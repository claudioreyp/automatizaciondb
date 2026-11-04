// Local transport pairing only. No .env, n8n client calls, POS relay or sink.
import { createServer } from "node:http";
import { mkdirSync, writeFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { ImpulsaGateway } from "./gateway.js";
import type { GatewayConfig } from "./types.js";
import { sessionSafeProviderClass } from "./session-safe-wppconnect-provider.js";
import { qrPreviewPage } from "./qr-preview-page.js";

const directory = process.argv[2];
const browserPath = process.argv[3];
const visible = process.argv[4] === "--visible";
if (!directory || !isAbsolute(directory) || !browserPath || !isAbsolute(browserPath)) {
  throw new Error("Usage: qr-preview <absolute isolated directory> <absolute browser executable>");
}
mkdirSync(directory, { recursive: true });
process.chdir(directory);
// Separate official connector install: ordinary gateway dependencies stay pinned.
const connectorRequire = createRequire(fileURLToPath(new URL("../qr-preview-runtime/package.json", import.meta.url)));
const connector = connectorRequire("@wppconnect-team/wppconnect") as { create: (args: Record<string, unknown>) => Promise<any> };
const config: GatewayConfig = {
  pairingOnly: true, port: 3008, defaultTenantId: "impulsa",
  sessionsDir: resolve(directory, "sessions"), wppExecutablePath: browserPath,
  n8nWebhookUrl: "http://127.0.0.1:1/disabled-qr-preview",
  n8nTimeoutMs: 1000, messageBatchWindowMs: 3500,
  defaultFallbackMessage: "", posEventPollMs: 5000,
};
const gateway = new ImpulsaGateway(config, sessionSafeProviderClass(connector.create));
const startedUtc = new Date().toISOString();
const observe = (stage: string) => writeFileSync(resolve(directory, "provider-status.json"),
  JSON.stringify({ pid: process.pid, startedUtc, updatedUtc: new Date().toISOString(), stage }), { mode: 0o600 });
observe("starting");
const server = createServer((request, response) => {
  const path = new URL(request.url || "/", "http://127.0.0.1:3008").pathname;
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("X-Content-Type-Options", "nosniff");
  const host = request.headers.host;
  if (!["localhost:3008", "127.0.0.1:3008"].includes(host || "") || request.method !== "GET") {
    response.writeHead(403); response.end("QR preview is read only"); return;
  }
  const state = gateway.getTenant("impulsa");
  if (path === "/api/health" || path === "/api/bots/impulsa/qr-state") {
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify({ mode: "pairing_only", revision: 7, connectorVersion: "2.3.6", pid: process.pid, startedUtc, consumerMessaging: "paused",
      n8nForwarding: false, posRelay: false, tenantId: "impulsa",
      status: state?.effectiveStatus || state?.status || "initializing",
      qr: path.endsWith("qr-state") ? state?.qr || null : undefined,
      qrGeneration: state?.qrGeneration || 0 })); return;
  }
  if (path !== "/" && path !== "/api/bots/impulsa/qr") {
    response.writeHead(404); response.end("Not found"); return;
  }
  response.setHeader("Content-Type", "text/html; charset=utf-8");
  response.setHeader("Content-Security-Policy", "default-src 'none'; img-src data:; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'");
  response.end(qrPreviewPage);
});
let closing = false;
const close = async () => {
  if (closing) return; closing = true;
  server.close(); await gateway.shutdown(); process.exit(0);
};
process.on("SIGINT", () => { void close(); });
process.on("SIGTERM", () => { void close(); });
server.once("error", error => { console.error("QR preview HTTP startup failed:", error.message); void close(); });
server.listen(3008, "127.0.0.1", () => console.log("QR preview: http://localhost:3008/api/bots/impulsa/qr · consumer messaging paused"));
void gateway.createTenant({ tenantId: "impulsa", name: "Impulsa · respuestas pausadas", providerOptions: {
  logQR: false,
  headless: !visible,
  puppeteerOptions: { headless: !visible, executablePath: browserPath, args: ["--no-sandbox", "--disable-setuid-sandbox"] },
  statusFind: (status: unknown) => observe(typeof status === "string" ? status.slice(0,80) : "status_changed"),
  onLoadingScreen: (percent: unknown) => observe(typeof percent === "number" ? "loading_" + percent : "loading"),
} }).then(() => observe("vendor_started")).catch(error => {
  observe("startup_failed");
  console.error("QR provider startup failed:", error instanceof Error ? error.message : "Unknown error");
});
