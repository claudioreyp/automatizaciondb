export type TenantStatus = "initializing" | "awaiting_qr" | "connected" | "degraded" | "disconnected" | "error";
export type ProviderStatus =
  | "initializing"
  | "require_action"
  | "ready"
  | "host"
  | "message"
  | "state_change"
  | "stream_change"
  | "disconnected"
  | "restarting"
  | "disconnecting"
  | "auth_failure"
  | "notice"
  | "error"
  | "unknown";
export type StartupVerificationStatus = "idle" | "pending" | "success" | "failed" | "recovering";

export interface GatewayConfig {
  /** Explicit local QR preview: never process or send consumer messages. */
  pairingOnly?: boolean;
  port: number;
  defaultTenantId: string;
  n8nWebhookUrl: string;
  n8nWebhookSecret?: string;
  apiKey?: string;
  sessionsDir: string;
  n8nTimeoutMs: number;
  messageBatchWindowMs: number;
  defaultFallbackMessage: string;
  wppExecutablePath?: string;
  posEventApiBase?: string;
  posEventToken?: string;
  posEventPollMs: number;
  posBusinessId?: number;
  posBranchId?: number;
}

export interface N8nInboundPayload {
  tenantId: string;
  channel: "whatsapp";
  provider: "wppconnect";
  from: string;
  name: string;
  /** Provider profile display value; never inferred from a message or agenda title. */
  contact_display_name?: string | null;
  /** Trusted provider metadata; never extracted from consumer text. */
  location?: { latitude: number; longitude: number };
  verified_phone?: string;
  message: {
    id: string;
    type: "text" | "media" | "document" | "voice_note" | "location" | "unknown";
    text: string;
    timestamp: number | null;
    media?: {
      data: string;
      mimeType: string;
      fileName: string;
      size: number;
    };
  };
  context: {
    isGroup: boolean;
    hasMedia: boolean;
    batched?: boolean;
    messageCount?: number;
    messageIds?: string[];
    turnId?: string;
    messages?: { id: string; text: string; type: N8nInboundPayload['message']['type'] }[];
  };
  raw: Record<string, unknown>;
}

export interface TextAction {
  type: "text";
  text: string;
}

export interface MediaAction {
  type: "media";
  url: string;
  caption?: string;
}

export interface BinaryMediaAction {
  type: "media_binary";
  data: string;
  mimeType: "image/png" | "image/jpeg" | "image/webp";
  caption?: string;
}

export type GatewayAction = TextAction | MediaAction | BinaryMediaAction | { type: "pos_menu"; caption?: never };

export interface N8nBridgeResponse {
  actions: GatewayAction[];
  turn?: { id: string; outcome: 'none' | 'committed' | 'uncertain' };
}

export type N8nExecutionStatus =
  | "success"
  | "empty"
  | "invalid_response"
  | "http_error"
  | "request_error"
  | "fallback";

export interface N8nExecutionResult {
  response: N8nBridgeResponse;
  meta: {
    status: N8nExecutionStatus;
    reason?: string | null;
  };
}

export interface TenantSnapshot {
  tenantId: string;
  name: string;
  status: TenantStatus;
  effectiveStatus?: TenantStatus;
  providerStatus?: ProviderStatus;
  hasLiveSessionEvidence?: boolean;
  isOperational?: boolean;
  providerType: string;
  port?: number;
  createdAt: string;
  updatedAt: string;
  host?: Record<string, unknown>;
  qr?: string | null;
  lastError?: string | null;
  lastInboundAt?: string | null;
  lastInboundType?: N8nInboundPayload["message"]["type"] | null;
  lastInboundTextPreview?: string | null;
  lastN8nAt?: string | null;
  lastN8nStatus?: N8nExecutionStatus | null;
  lastOutboundAt?: string | null;
  lastOutboundPreview?: string | null;
  lastTransportError?: string | null;
  sessionReady?: boolean;
  messageBusReady?: boolean;
  messageBusListenerCount?: number | null;
  fallbackMessageBusReady?: boolean;
  fallbackMessageBusListenerCount?: number | null;
  pollingMessageBusReady?: boolean;
  pollingMessageBusLastRunAt?: string | null;
  pollingMessageBusCursorAt?: string | null;
  pollingMessageBusError?: string | null;
  pollingMessageBusConsecutiveErrors?: number;
  pollingMessageBusRecoveryAt?: string | null;
  pollingMessageBusRecoveryReason?: string | null;
  listenerBoundAt?: string | null;
  startupVerifiedAt?: string | null;
  startupVerificationStatus?: StartupVerificationStatus | null;
  startupVerificationError?: string | null;
  lastMessageEventAt?: string | null;
  lastProviderEventAt?: string | null;
  lastProviderEventSource?: "builderbot_message" | "vendor_any_message" | "vendor_message_poll" | null;
  lastAcceptedInboundAt?: string | null;
  pendingInboundBatchCount?: number;
  lastInboundBatchSize?: number | null;
  lastInboundBatchAt?: string | null;
  acceptMessagesAfter?: string | null;
  syncBarrierStartedAt?: string | null;
  syncLastNoiseAt?: string | null;
  syncSettledAt?: string | null;
  syncBarrierActive?: boolean;
  lastIgnoredInboundAt?: string | null;
  lastIgnoredReason?: string | null;
  ignoredInboundCount?: number;
  recoveryAttemptCount?: number;
  lastRecoveryAt?: string | null;
  lastDisconnectAt?: string | null;
  lastDisconnectReason?: string | null;
  lastVendorState?: string | null;
  lastVendorStream?: string | null;
  autoRestartReason?: string | null;
  disconnectPendingQr?: boolean;
  disconnectGeneration?: number;
  connectionInvalidatedAt?: string | null;
  disconnectInProgress?: boolean;
  restartInProgress?: boolean;
  qrUpdatedAt?: string | null;
  qrGeneration?: number;
  qrInvalidatedAt?: string | null;
  qrRecoveryInProgress?: boolean;
  lastAuthFailureAt?: string | null;
  lastAuthFailureReason?: string | null;
}

export interface TenantState extends TenantSnapshot {
  qr?: string | null;
  lastError?: string | null;
}

export interface CreateTenantInput {
  tenantId: string;
  name?: string;
  providerOptions?: Record<string, unknown>;
}

export interface RestartTenantInput {
  name?: string;
  providerOptions?: Record<string, unknown>;
}

export interface SendMessageInput {
  tenantId: string;
  number: string;
  message: string;
  media?: string;
}

export interface ParsedInboundMessage {
  type: N8nInboundPayload["message"]["type"];
  text: string;
  hasMedia: boolean;
}

export interface RawBotContext extends Record<string, unknown> {
  name?: string;
  body: string;
  from: string;
}
