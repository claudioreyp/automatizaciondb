import { mkdirSync } from "fs";
import { resolve } from "path";
import { config as loadEnv } from "dotenv";

import type { GatewayConfig } from "./types.js";

loadEnv();

const parseNumber = (value: string | undefined, fallback: number): number => {
  if (!value) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
};

export const loadConfig = (): GatewayConfig => {
  const sessionsDir = resolve(process.cwd(), process.env.SESSIONS_DIR || "./sessions");
  mkdirSync(sessionsDir, { recursive: true });

  return {
    port: parseNumber(process.env.PORT, 3008),
    defaultTenantId: process.env.DEFAULT_TENANT_ID || "impulsa",
    n8nWebhookUrl: process.env.N8N_WEBHOOK_URL || "http://localhost:5678/webhook/impulsa-whatsapp-bridge",
    n8nWebhookSecret: process.env.N8N_WEBHOOK_SECRET || undefined,
    apiKey: process.env.API_KEY || undefined,
    sessionsDir,
    n8nTimeoutMs: parseNumber(process.env.N8N_TIMEOUT_MS, 90000),
    messageBatchWindowMs: parseNumber(process.env.MESSAGE_BATCH_WINDOW_MS, 3500),
    defaultFallbackMessage:
      process.env.DEFAULT_FALLBACK_MESSAGE ||
      "No pude confirmar cómo quedó tu solicitud. Escríbeme de nuevo en unos segundos para revisarla. 🙏",
    wppExecutablePath: process.env.WPP_EXECUTABLE_PATH || undefined,
    posEventApiBase: process.env.ESCALAR_POS_API_BASE?.replace(/\/$/, "") || undefined,
    posEventToken: process.env.ESCALAR_POS_EVENTS_TOKEN || undefined,
    posEventPollMs: parseNumber(process.env.ESCALAR_POS_EVENTS_POLL_MS, 5000),
    posBusinessId: Number(process.env.ESCALAR_POS_BUSINESS_ID) || undefined,
    posBranchId: Number(process.env.ESCALAR_POS_BRANCH_ID) || undefined,
  };
};
