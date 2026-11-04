import type { IncomingMessage, ServerResponse } from "http";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "fs";
import { resolve } from "path";
import polka from "polka";
import { z } from "zod";

import { ImpulsaGateway } from "./gateway.js";
import type { TenantState } from "./types.js";

interface Request extends IncomingMessage {
  body?: unknown;
  params?: Record<string, string | string[]>;
}

const createTenantSchema = z.object({
  tenantId: z.string().min(1),
  name: z.string().min(1).optional(),
  providerOptions: z.record(z.string(), z.unknown()).optional(),
});

const restartTenantSchema = z.object({
  name: z.string().min(1).optional(),
  providerOptions: z.record(z.string(), z.unknown()).optional(),
});

const sendMessageSchema = z.object({
  tenantId: z.string().min(1),
  number: z.string().min(1),
  message: z.string().min(1),
  media: z.string().url().optional(),
});

export class GatewayApi {
  private readonly app = polka();
  private readonly instanceId = randomUUID();

  constructor(private readonly gateway: ImpulsaGateway, private readonly apiKey?: string) {}

  start(port: number): void {
    this.app.use(this.cors());
    this.app.use(this.jsonParser());
    this.app.use(this.auth());

    this.app.get("/", (_req, res) => {
      this.send(res, 200, {
        name: "impulsa-gateway",
        endpoints: [
          "GET /api/health",
          "GET /api/bots",
          "POST /api/bots",
          "GET /api/bots/:tenantId",
          "GET /api/bots/:tenantId/qr",
          "GET /api/payments/:tenantId/yape-qr.png",
          "POST /api/bots/:tenantId/disconnect",
          "POST /api/bots/:tenantId/restart",
          "DELETE /api/bots/:tenantId",
          "POST /api/messages/send",
        ],
      });
    });

    this.app.get("/api/health", (_req, res) => {
      this.send(res, 200, this.gateway.getHealth());
    });

    this.app.get("/api/payments/:tenantId/yape-qr.png", (req, res) => {
      const tenantId = this.getParam(req, "tenantId").replace(/[^a-zA-Z0-9_-]/g, "");
      const assetPath = resolve(process.cwd(), "assets", `yape-${tenantId}.png`);
      if (!tenantId || !existsSync(assetPath)) {
        return this.send(res, 404, { error: "Yape QR not configured" });
      }

      const image = readFileSync(assetPath);
      res.writeHead(200, {
        "content-type": "image/png",
        "cache-control": "public, max-age=300",
        "content-length": image.length,
      });
      res.end(image);
    });

    this.app.get("/api/bots", (_req, res) => {
      const bots = this.gateway.listTenants();
      this.send(res, 200, { count: bots.length, bots });
    });

    this.app.post("/api/bots", async (req, res) => {
      const parsed = createTenantSchema.safeParse(req.body);
      if (!parsed.success) {
        return this.send(res, 400, { error: "Validation failed", details: parsed.error.flatten() });
      }

      try {
        const state = await this.gateway.createTenant(parsed.data);
        this.send(res, 201, state);
      } catch (error) {
        this.send(res, 400, { error: this.errorMessage(error) });
      }
    });

    this.app.get("/api/bots/:tenantId", (req, res) => {
      const tenantId = this.getParam(req, "tenantId");
      const state = this.gateway.getTenant(tenantId);
      if (!state) {
        return this.send(res, 404, { error: "Bot not found" });
      }
      this.send(res, 200, state);
    });

    this.app.get("/api/bots/:tenantId/qr", (req, res) => {
      const tenantId = this.getParam(req, "tenantId");
      const state = this.gateway.getTenant(tenantId);
      if (!state) {
        return this.send(res, 404, { error: "Bot not found" });
      }

      const format = this.getQueryParam(req, "format");
      if (format === "image") {
        return this.sendQrImage(res, state.qr);
      }

      if (format === "view" || this.prefersHtml(req)) {
        return this.sendQrHtml(res, tenantId, state);
      }

      this.send(res, 200, this.toQrResponse(tenantId, state));
    });

    this.app.post("/api/bots/:tenantId/disconnect", async (req, res) => {
      const tenantId = this.getParam(req, "tenantId");

      try {
        const state = await this.gateway.disconnectTenant(tenantId);
        this.send(res, 200, { success: true, tenantId, state: { ...state, gatewayInstanceId: this.instanceId } });
      } catch (error) {
        this.send(res, 400, { error: this.errorMessage(error) });
      }
    });

    this.app.post("/api/bots/:tenantId/restart", async (req, res) => {
      const tenantId = this.getParam(req, "tenantId");
      const parsed = restartTenantSchema.safeParse(req.body ?? {});
      if (!parsed.success) {
        return this.send(res, 400, { error: "Validation failed", details: parsed.error.flatten() });
      }

      try {
        const state = await this.gateway.restartTenant(tenantId, parsed.data);
        this.send(res, 200, state);
      } catch (error) {
        this.send(res, 400, { error: this.errorMessage(error) });
      }
    });

    this.app.delete("/api/bots/:tenantId", async (req, res) => {
      const tenantId = this.getParam(req, "tenantId");
      const removed = await this.gateway.removeTenant(tenantId);
      if (!removed) {
        return this.send(res, 404, { error: "Bot not found" });
      }
      this.send(res, 200, { success: true, tenantId });
    });

    this.app.post("/api/messages/send", async (req, res) => {
      const parsed = sendMessageSchema.safeParse(req.body);
      if (!parsed.success) {
        return this.send(res, 400, { error: "Validation failed", details: parsed.error.flatten() });
      }

      try {
        await this.gateway.sendMessage(parsed.data);
        this.send(res, 200, { success: true });
      } catch (error) {
        this.send(res, 400, { error: this.errorMessage(error) });
      }
    });

    this.app.listen(port, "127.0.0.1", () => {
      console.log(`Impulsa gateway listening on http://localhost:${port}`);
    });
  }

  stop(): void {
    this.app.server?.close();
  }

  private jsonParser() {
    return async (req: any, _res: ServerResponse, next: () => void) => {
      if (req.method === "POST" || req.method === "PUT" || req.method === "PATCH") {
        try {
          const chunks: Buffer[] = [];
          for await (const chunk of req) {
            chunks.push(chunk as Buffer);
          }
          const raw = Buffer.concat(chunks).toString();
          req.body = raw ? JSON.parse(raw) : {};
        } catch {
          req.body = {};
        }
      }
      next();
    };
  }

  private cors() {
    return (req: IncomingMessage, res: ServerResponse, next: () => void) => {
      const host = req.headers.host || "";
      const local = /^(?:127\.0\.0\.1|localhost):\d+$/.test(host);
      const origin = req.headers.origin;
      if (!local || (origin && origin !== `http://${host}`)
          || req.headers["sec-fetch-site"] === "cross-site") {
        this.send(res, 403, { error: "Local access only" });
        return;
      }
      if (origin) res.setHeader("Access-Control-Allow-Origin", origin);
      res.setHeader("Vary", "Origin");
      res.setHeader("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
      res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-API-Key, Authorization");
      next();
    };
  }

  private auth() {
    return (req: any, res: ServerResponse, next: () => void) => {
      if (req.method === "OPTIONS") {
        res.writeHead(204);
        res.end();
        return;
      }

      if (req.method === "GET" && /^\/api\/payments\/[^/]+\/yape-qr\.png(?:\?|$)/.test(req.url || "")) {
        next();
        return;
      }

      if (!this.apiKey) {
        next();
        return;
      }

      const provided = req.headers["x-api-key"] || req.headers.authorization;
      if (provided === this.apiKey || provided === `Bearer ${this.apiKey}`) {
        next();
        return;
      }

      this.send(res, 401, { error: "Unauthorized" });
    };
  }

  private send(res: ServerResponse, status: number, body: unknown): void {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  }

  private sendQrImage(res: ServerResponse, qr: string | null | undefined): void {
    if (!qr?.startsWith("data:image/png;base64,")) {
      res.writeHead(404, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "QR not available" }));
      return;
    }

    const base64 = qr.replace("data:image/png;base64,", "");
    const buffer = Buffer.from(base64, "base64");
    res.writeHead(200, {
      "content-type": "image/png",
      "cache-control": "no-store",
      "content-length": buffer.length,
    });
    res.end(buffer);
  }

  private sendQrHtml(res: ServerResponse, tenantId: string, state: TenantState): void {
    const qrState = this.toQrResponse(tenantId, state);
    const body = `<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>QR ${this.escapeHtml(tenantId)}</title>
    <style>
      body { font-family: Arial, sans-serif; background: #111827; color: #f9fafb; margin: 0; min-height: 100vh; display: grid; place-items: center; }
      .card { width: min(92vw, 560px); background: #1f2937; border: 1px solid #374151; border-radius: 18px; padding: 24px; box-sizing: border-box; text-align: center; }
      img { width: min(100%, 360px); height: auto; background: white; border-radius: 12px; padding: 12px; box-sizing: border-box; }
      .hidden { display: none; }
      h1 { margin: 0 0 12px; font-size: 24px; }
      p { margin: 8px 0; color: #d1d5db; }
      a { color: #93c5fd; }
      code { background: #111827; padding: 2px 6px; border-radius: 6px; }
      .status-connected { color: #86efac; }
      .status-degraded { color: #fcd34d; }
      .status-disconnected { color: #fdba74; }
      .status-error { color: #fca5a5; }
      .status-awaiting_qr { color: #93c5fd; }
      .status-initializing { color: #d1d5db; }
      .actions { margin-top: 18px; display: flex; justify-content: center; gap: 12px; flex-wrap: wrap; }
      button { border: 0; border-radius: 10px; padding: 12px 16px; background: #2563eb; color: #fff; font-weight: 600; cursor: pointer; }
      button:disabled { opacity: 0.55; cursor: wait; }
      .meta { font-size: 12px; color: #9ca3af; min-height: 18px; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>QR de ${this.escapeHtml(tenantId)}</h1>
      <p>Estado actual: <strong id="effective-status" class="status-${this.escapeHtml(qrState.effectiveStatus)}">${this.escapeHtml(qrState.effectiveStatus)}</strong></p>
      <p>Provider: <code id="provider-status">${this.escapeHtml(qrState.providerStatus)}</code></p>
      <p id="hint">${this.escapeHtml(this.buildQrHint(qrState.effectiveStatus, qrState.hasLiveSessionEvidence))}</p>
      <img id="qr-image" class="${qrState.qr ? "" : "hidden"}" src="${qrState.qr ?? ""}" alt="QR de WhatsApp para ${this.escapeHtml(tenantId)}" />
      <p id="image-link-row" class="${qrState.qr ? "" : "hidden"}"><a href="/api/bots/${encodeURIComponent(tenantId)}/qr?format=image">Abrir solo la imagen</a></p>
      <div class="actions">
        <button id="disconnect-button" type="button" class="${qrState.effectiveStatus === "connected" || qrState.effectiveStatus === "degraded" || qrState.effectiveStatus === "error" ? "" : "hidden"}">Desconectar y generar nuevo QR</button>
      </div>
      <p id="action-status" class="meta"></p>
      <p><a href="/api/bots/${encodeURIComponent(tenantId)}/qr?format=json">Ver JSON</a></p>
      <p><a href="/api/bots/${encodeURIComponent(tenantId)}">Ver estado del tenant</a></p>
    </div>
    <script>
      const qrJsonUrl = "/api/bots/${encodeURIComponent(tenantId)}/qr?format=json";
      const disconnectUrl = "/api/bots/${encodeURIComponent(tenantId)}/disconnect";
      const statusEl = document.getElementById("effective-status");
      const providerEl = document.getElementById("provider-status");
      const hintEl = document.getElementById("hint");
      const qrImageEl = document.getElementById("qr-image");
      const imageLinkRowEl = document.getElementById("image-link-row");
      const disconnectButtonEl = document.getElementById("disconnect-button");
      const actionStatusEl = document.getElementById("action-status");
      const hints = {
        awaiting_qr: "Escanea este codigo con WhatsApp.",
        connected: "WhatsApp ya esta conectado y la sesion quedo activa.",
        degraded: "La sesion sigue enlazada, pero el provider reporto senales inestables. Puedes desconectarla desde aqui para generar un QR nuevo.",
        error: "No se pudo regenerar el QR automaticamente. Intenta desconectarlo otra vez en unos segundos.",
        initializing: "Inicializando la sesion. Esta pantalla se actualiza sola.",
        disconnected: "La sesion fue desvinculada. Espera unos segundos mientras se genera un QR nuevo."
      };
      let actionInFlight = false;
      let actionEpoch = 0;
      let lastState = null;
      let lastInstanceId = ${JSON.stringify(this.instanceId)};
      let lastDisconnectGeneration = ${Math.max(0, Number(qrState.disconnectGeneration) || 0)};
      let actionError = "";
      let actionErrorGeneration = 0;
      let pollInFlight = false;
      let pollAgain = false;

      const requestJson = async (url, options, timeoutMs) => {
        const controller = new AbortController();
        let timer;
        try {
          return await Promise.race([
            (async () => {
              const response = await fetch(url, { ...options, signal: controller.signal });
              const payload = await response.json().catch(() => null);
              if (!response.ok) {
                throw new Error(payload?.error || "No se pudo completar la accion.");
              }
              if (!payload || typeof payload !== "object") {
                throw new Error("No pude leer la respuesta del gateway.");
              }
              return payload;
            })(),
            new Promise((_, reject) => {
              timer = setTimeout(() => {
                const error = new Error("El gateway no respondio a tiempo.");
                error.name = "TimeoutError";
                reject(error);
                controller.abort();
              }, timeoutMs);
            })
          ]);
        } finally {
          clearTimeout(timer);
        }
      };

      const render = (data) => {
        const instanceId = data.gatewayInstanceId || lastInstanceId;
        if (instanceId !== lastInstanceId) {
          lastInstanceId = instanceId;
          lastDisconnectGeneration = 0;
          actionError = "";
          actionErrorGeneration = 0;
        }
        const generation = Number(data.disconnectGeneration || 0);
        if (generation < lastDisconnectGeneration) return;
        if (generation > actionErrorGeneration) actionError = "";
        lastDisconnectGeneration = generation;
        lastState = data;
        const effectiveStatus = data.effectiveStatus || data.status || "initializing";
        const providerStatus = data.providerStatus || "unknown";
        const hasLiveSessionEvidence = !!data.hasLiveSessionEvidence;
        const qrRecoveryInProgress = !!data.qrRecoveryInProgress;
        const shouldShowQr = !!data.qr && effectiveStatus === "awaiting_qr" && !qrRecoveryInProgress;
        const busy = !!data.disconnectInProgress || !!data.restartInProgress || actionInFlight;
        const canDisconnect = effectiveStatus === "connected" || effectiveStatus === "degraded" || effectiveStatus === "error";

        statusEl.textContent = effectiveStatus;
        statusEl.className = "status-" + effectiveStatus;
        providerEl.textContent = providerStatus;
        hintEl.textContent = qrRecoveryInProgress
          ? "Generando un nuevo QR..."
          : (hints[effectiveStatus] || "Esperando estado del bot.");

        if (shouldShowQr) {
          qrImageEl.src = data.qr;
          qrImageEl.classList.remove("hidden");
          imageLinkRowEl.classList.remove("hidden");
        } else {
          qrImageEl.classList.add("hidden");
          imageLinkRowEl.classList.add("hidden");
        }

        if (canDisconnect) {
          disconnectButtonEl.classList.remove("hidden");
        } else {
          disconnectButtonEl.classList.add("hidden");
        }

        disconnectButtonEl.disabled = busy;

        if (!actionInFlight) {
          if (actionError) {
            actionStatusEl.textContent = actionError;
          } else if (qrRecoveryInProgress) {
            actionStatusEl.textContent = "Generando un nuevo QR...";
          } else if (busy) {
            actionStatusEl.textContent = "Cerrando la sesion actual y generando un nuevo QR...";
          } else if ((effectiveStatus === "awaiting_qr" && shouldShowQr)
              || ((effectiveStatus === "connected" || effectiveStatus === "degraded") && hasLiveSessionEvidence)) {
            actionStatusEl.textContent = "";
          }
        }
      };

      const runAction = async (url, label) => {
        if (actionInFlight) return;
        actionInFlight = true;
        actionEpoch++;
        actionError = "";
        actionErrorGeneration = lastDisconnectGeneration;
        actionStatusEl.textContent = label;
        disconnectButtonEl.disabled = true;

        try {
          const payload = await requestJson(url, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: "{}"
          }, 12000);
          if (payload.state && typeof payload.state === "object") {
            render(payload.state);
          }
          actionStatusEl.textContent = "Accion iniciada. Esta pantalla se actualizara sola.";
        } catch (error) {
          actionError = error?.name === "TimeoutError"
            ? "No pude confirmar la respuesta. Consultare el estado; no repitas la accion mientras se actualiza."
            : (error instanceof Error ? error.message : "No se pudo completar la accion.");
          actionStatusEl.textContent = actionError;
        } finally {
          actionInFlight = false;
          if (lastState) render(lastState);
          else disconnectButtonEl.disabled = false;
          await poll();
        }
      };

      disconnectButtonEl.addEventListener("click", () => {
        void runAction(disconnectUrl, "Cerrando la sesion actual y generando un nuevo QR...");
      });

      const poll = async () => {
        if (actionInFlight) return;
        if (pollInFlight) {
          pollAgain = true;
          return;
        }
        pollInFlight = true;
        const epoch = actionEpoch;
        try {
          const data = await requestJson(qrJsonUrl, { cache: "no-store" }, 8000);
          if (!data.effectiveStatus && !data.status) throw new Error("Estado del gateway no disponible.");
          if (epoch === actionEpoch) render(data);
        } catch {
          if (epoch === actionEpoch && !actionInFlight && !actionError) {
            actionStatusEl.textContent = "No pude actualizar el estado. Se volvera a consultar automaticamente.";
          }
        } finally {
          pollInFlight = false;
          if (pollAgain) {
            pollAgain = false;
            if (!actionInFlight) void poll();
          }
        }
      };

      setInterval(poll, 2500);
      poll();
    </script>
  </body>
</html>`;

    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    res.end(body);
  }

  private toQrResponse(tenantId: string, state: TenantState) {
    return {
      tenantId,
      gatewayInstanceId: this.instanceId,
      status: state.effectiveStatus ?? state.status,
      effectiveStatus: state.effectiveStatus ?? state.status,
      providerStatus: state.providerStatus ?? "unknown",
      hasLiveSessionEvidence: state.hasLiveSessionEvidence ?? false,
      isOperational: state.isOperational ?? false,
      disconnectPendingQr: state.disconnectPendingQr ?? false,
      disconnectGeneration: state.disconnectGeneration ?? 0,
      connectionInvalidatedAt: state.connectionInvalidatedAt ?? null,
      disconnectInProgress: state.disconnectInProgress ?? false,
      restartInProgress: state.restartInProgress ?? false,
      qrUpdatedAt: state.qrUpdatedAt ?? null,
      qrGeneration: state.qrGeneration ?? 0,
      qrInvalidatedAt: state.qrInvalidatedAt ?? null,
      qrRecoveryInProgress: state.qrRecoveryInProgress ?? false,
      lastAuthFailureAt: state.lastAuthFailureAt ?? null,
      lastAuthFailureReason: state.lastAuthFailureReason ?? null,
      qr: state.qr ?? null,
      lastDisconnectAt: state.lastDisconnectAt ?? null,
      lastDisconnectReason: state.lastDisconnectReason ?? null,
      lastVendorState: state.lastVendorState ?? null,
      lastVendorStream: state.lastVendorStream ?? null,
      autoRestartReason: state.autoRestartReason ?? null,
      message: state.qr ? "Scan QR to connect" : "QR not available",
    };
  }

  private getParam(req: Request, key: string): string {
    const value = req.params?.[key];
    return Array.isArray(value) ? value[0] || "" : value || "";
  }

  private getQueryParam(req: Request, key: string): string | null {
    const url = new URL(req.url || "/", "http://localhost");
    return url.searchParams.get(key);
  }

  private prefersHtml(req: Request): boolean {
    const format = this.getQueryParam(req, "format");
    if (format === "json") {
      return false;
    }

    const accept = req.headers.accept || "";
    return typeof accept === "string" && accept.includes("text/html");
  }

  private escapeHtml(value: string): string {
    return value
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  private errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : "Unknown error";
  }

  private buildQrHint(status: string, hasLiveSessionEvidence: boolean): string {
    if ((status === "connected" || status === "degraded") && hasLiveSessionEvidence) {
      return "WhatsApp ya quedo enlazado. Si quieres cambiar el dispositivo, desconectalo desde esta pagina.";
    }

    if (status === "awaiting_qr") {
      return "Escanea este codigo con WhatsApp.";
    }

    if (status === "error") {
      return "No se pudo regenerar el QR automaticamente. Intenta desconectarlo otra vez en unos segundos.";
    }

    if (status === "initializing") {
      return "Inicializando la sesion. Esta pantalla se actualiza sola.";
    }

    if (status === "disconnected") {
      return "La sesion fue desvinculada. Espera unos segundos mientras se genera un nuevo QR.";
    }

    return "Esperando estado del bot.";
  }
}
