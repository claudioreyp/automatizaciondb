import type { GatewayConfig, N8nExecutionResult, N8nInboundPayload } from "./types.js";
import { isStickerInbound, normalizeN8nResponse } from "./utils.js";

const SAFE_FALLBACK = "No pude confirmar cómo quedó tu solicitud. Escríbeme de nuevo en unos segundos para revisarla. 🙏";

type ExecutionContext = { acceptedInbound?: boolean };

const canRespondToEmptyWebhook = (payload: N8nInboundPayload, context: ExecutionContext): boolean => {
  // Only the gateway can mark a turn accepted after freshness, audience, sticker,
  // materialized-media and message-ID deduplication checks. Keep the remaining
  // checks here so a direct caller cannot turn a group or sticker into a reply.
  if (context.acceptedInbound !== true || payload?.channel !== "whatsapp"
      || payload?.context?.isGroup !== false) return false;
  const from = String(payload.from || "").trim().toLowerCase();
  if (!/^\d{6,20}(?:@(c\.us|lid|s\.whatsapp\.net))?$/.test(from)) return false;
  if (!String(payload.message?.id || "").trim()) return false;
  if (payload.raw && isStickerInbound(payload.raw as { body: string; from: string })) return false;
  if (payload.message.type === "text") return Boolean(payload.message.text?.trim());
  if (payload.message.type === "location") return true;
  if (["media", "document", "voice_note"].includes(payload.message.type))
    return Boolean(payload.message.media?.data);
  return false;
};

export class N8nClient {
  constructor(private readonly config: GatewayConfig) {}

  async execute(payload: N8nInboundPayload, context: ExecutionContext = {}): Promise<N8nExecutionResult> {
    try {
      const host = new URL(this.config.n8nWebhookUrl).hostname;
      if (!["localhost", "127.0.0.1", "[::1]"].includes(host) && !this.config.n8nWebhookSecret) {
        return this.buildFallback("request_error", "Webhook authentication is not configured");
      }
      const response = await fetch(this.config.n8nWebhookUrl, {
        method: "POST",
        redirect: "error",
        headers: {
          "content-type": "application/json",
          ...(this.config.n8nWebhookSecret ? { "X-Escalar-Bridge-Key": this.config.n8nWebhookSecret } : {}),
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(this.config.n8nTimeoutMs),
      });

      if (!response.ok) {
        return this.buildFallback("http_error", `n8n returned status ${response.status}`);
      }

      const text = await response.text();
      if (!text.trim()) {
        if (canRespondToEmptyWebhook(payload, context)) {
          // n8n's explicit `{"actions":[]}` remains silent. A truly empty body
          // after an accepted turn means its branch ended without a result; the
          // POS outcome is unknown, so never claim that an order or payment exists.
          return this.buildFallback("empty_response", "n8n returned an empty response");
        }
        return {
          response: { actions: [] },
          meta: {
            status: "empty",
            reason: null,
          },
        };
      }

      let parsed: unknown;
      try { parsed = JSON.parse(text); }
      catch {
        // Never turn malformed structured output (including image data) into a chat message.
        if (/^[\[{]/.test(text.trim())) return this.buildFallback("invalid_response", "Invalid structured response");
        parsed = text;
      }
      try {
        return {
          response: normalizeN8nResponse(parsed),
          meta: {
            status: "success",
            reason: null,
          },
        };
      } catch {
        return this.buildFallback("invalid_response", "Invalid outbound action");
      }
    } catch {
      return this.buildFallback("request_error", "Webhook request unavailable");
    }
  }

  private buildFallback(status: "http_error" | "request_error" | "invalid_response" | "empty_response", reason: string): N8nExecutionResult {
    console.error(`[n8n] ${reason}`);

    const fallback = this.config.defaultFallbackMessage.trim() || SAFE_FALLBACK;

    return {
      response: {
        actions: [{ type: "text", text: fallback }],
      },
      meta: {
        status: "fallback",
        reason,
      },
    };
  }
}
