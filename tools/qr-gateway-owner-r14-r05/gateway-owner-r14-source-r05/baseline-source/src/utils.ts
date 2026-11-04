import qr from "qr-image";
import { validateBinaryMedia } from "./binary-media.js";

import type { GatewayAction, N8nBridgeResponse, ParsedInboundMessage, RawBotContext } from "./types.js";

const EVENT_TYPE_MAP: Record<string, ParsedInboundMessage["type"]> = {
  "_event_media_": "media",
  "_event_document_": "document",
  "_event_voice_note_": "voice_note",
  "_event_location_": "location",
};

const resolveEventMessageType = (body: string): ParsedInboundMessage["type"] | undefined => {
  for (const [marker, type] of Object.entries(EVENT_TYPE_MAP)) {
    // BuilderBot appends a unique suffix to these markers (for example,
    // _event_media__<uuid>). Exact matching misclassifies them as text.
    if (body === marker || body.startsWith(marker)) {
      return type;
    }
  }

  return undefined;
};

export const toQrDataUrl = (qrText: string): string => {
  const png = qr.imageSync(qrText, { type: "png", margin: 4 });
  return `data:image/png;base64,${png.toString("base64")}`;
};

export const normalizeQrImage = (value: string): string => {
  const trimmed = value.trim();
  if (!trimmed) return "";
  if (trimmed.startsWith("data:image/")) {
    return trimmed;
  }
  return toQrDataUrl(trimmed);
};

export const parseInboundMessage = (ctx: RawBotContext): ParsedInboundMessage => {
  const body = typeof ctx.body === "string" ? ctx.body : "";
  const mappedType = resolveEventMessageType(body);

  if (mappedType) {
    return {
      type: mappedType,
      text: "",
      hasMedia: mappedType !== "location",
    };
  }

  return {
    type: "text",
    text: body,
    hasMedia: false,
  };
};

// WPPConnect identifies stickers by message type. A sticker can use image/webp,
// but ordinary customer photos may also be WebP, so MIME alone is insufficient.
export const isStickerInbound = (ctx: RawBotContext): boolean => {
  const raw = ctx as Record<string, unknown>;
  const nested = [raw._data, raw.data, raw.message]
    .filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === "object");
  const hasStickerSignal = (value: Record<string, unknown>): boolean =>
    value.isSticker === true || [value.type, value.mtype, value.messageType]
      .some(type => typeof type === "string" && type.trim().toLowerCase() === "sticker");

  if (hasStickerSignal(raw)) return true;
  // Some provider wrappers omit the outer type. Do not let a quoted sticker
  // override an explicit outer text/image type.
  if ([raw.type, raw.mtype, raw.messageType].some(type => typeof type === "string" && type.trim())) {
    return false;
  }
  return nested.some(hasStickerSignal);
};

export const isPlaceholderInbound = (message: ParsedInboundMessage): boolean => {
  return message.type !== "text" && !message.text.trim();
};

export const isEmptyTextInbound = (message: ParsedInboundMessage): boolean => {
  return message.type === "text" && !message.text.trim();
};

export type InboundClassification =
  | "sync_notification_event"
  | "empty_text_sync_event"
  | "historical_media_placeholder"
  | "ambiguous_sync_event"
  | "fresh_text_candidate"
  | "fresh_media_candidate";

export const extractMessageId = (ctx: RawBotContext): string => {
  const key = ctx.key && typeof ctx.key === "object" ? ctx.key as Record<string, unknown> : null;
  const id = ctx.id && typeof ctx.id === "object" ? ctx.id as Record<string, unknown> : null;
  const raw = [key?.id, id?.id, ctx.id, key?._serialized, id?._serialized]
    .find(value => typeof value === "string" && value.trim()) as string | undefined;
  if (!raw) return "";
  const value = raw.trim();
  // MsgKey._serialized is fromMe_remote_messageId. Use the same provider ID
  // exposed by key.id/id.id so duplicate listener and poll copies converge.
  return /^(?:true|false)_[^_]+_(.+)$/i.exec(value)?.[1] || value;
};

export const extractTimestamp = (ctx: RawBotContext): number | null => {
  const timestampMs = extractProviderTimestampMs(ctx);
  return typeof timestampMs === "number" ? Math.floor(timestampMs / 1000) : null;
};

const toFiniteNumber = (value: unknown): number | null => {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }

  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  if (typeof value === "object" && value !== null) {
    const low = (value as Record<string, unknown>).low;
    if (typeof low === "number" && Number.isFinite(low)) {
      return low;
    }
  }

  return null;
};

const normalizeTimestampMs = (value: number | null): number | null => {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }

  return value >= 1_000_000_000_000 ? value : value * 1000;
};

export type ProviderTimestampSource = "timestamp" | "messageTimestamp" | "t" | "clientReceivedTsMillis" | null;

export const extractProviderTimestampInfo = (
  ctx: RawBotContext
): { timestampMs: number | null; source: ProviderTimestampSource } => {
  const candidates: Array<[ProviderTimestampSource, unknown]> = [
    ["timestamp", ctx.timestamp],
    ["messageTimestamp", ctx.messageTimestamp],
    ["t", ctx.t],
    ["clientReceivedTsMillis", ctx.clientReceivedTsMillis],
  ];

  for (const [source, candidate] of candidates) {
    const parsed = normalizeTimestampMs(toFiniteNumber(candidate));
    if (parsed !== null) {
      return { timestampMs: parsed, source };
    }
  }

  return { timestampMs: null, source: null };
};

export const extractProviderTimestampMs = (ctx: RawBotContext): number | null => {
  return extractProviderTimestampInfo(ctx).timestampMs;
};

export const hasServerMessageTimestamp = (ctx: RawBotContext): boolean => {
  const { timestampMs, source } = extractProviderTimestampInfo(ctx);
  return timestampMs !== null && source !== null && source !== "clientReceivedTsMillis";
};

export const extractIsNewMsg = (ctx: RawBotContext): boolean | null => {
  const isNewMsg = ctx.isNewMsg;
  return typeof isNewMsg === "boolean" ? isNewMsg : null;
};

export const extractRecvFresh = (ctx: RawBotContext): boolean | null => {
  const recvFresh = ctx.recvFresh;
  return typeof recvFresh === "boolean" ? recvFresh : null;
};

export const extractIsNewMessage = (ctx: RawBotContext): boolean | null => {
  const isNewMsg = extractIsNewMsg(ctx);
  if (isNewMsg !== null) {
    return isNewMsg;
  }

  const recvFresh = extractRecvFresh(ctx);
  if (recvFresh !== null) {
    return recvFresh;
  }

  return null;
};

const SYNC_NOTIFICATION_TYPES = new Set(["notification_template"]);
const SYNC_NOTIFICATION_SUBTYPES = new Set(["biz_account_type_changed_to_hosted"]);

export const extractProviderEventType = (ctx: RawBotContext): string | null => {
  return typeof ctx.type === "string" && ctx.type.trim() ? ctx.type.trim() : null;
};

export const extractProviderSubtype = (ctx: RawBotContext): string | null => {
  return typeof ctx.subtype === "string" && ctx.subtype.trim() ? ctx.subtype.trim() : null;
};

export const isSyncNotificationEvent = (ctx: RawBotContext): boolean => {
  const eventType = extractProviderEventType(ctx);
  const subtype = extractProviderSubtype(ctx);
  return (
    (eventType !== null && SYNC_NOTIFICATION_TYPES.has(eventType)) ||
    (subtype !== null && SYNC_NOTIFICATION_SUBTYPES.has(subtype))
  );
};

export const classifyInboundEvent = (
  ctx: RawBotContext,
  message: ParsedInboundMessage
): InboundClassification => {
  if (isSyncNotificationEvent(ctx)) {
    return "sync_notification_event";
  }

  if (isEmptyTextInbound(message)) {
    return "empty_text_sync_event";
  }

  if (isPlaceholderInbound(message)) {
    return "historical_media_placeholder";
  }

  const { source } = extractProviderTimestampInfo(ctx);
  if (source === "clientReceivedTsMillis") {
    return "ambiguous_sync_event";
  }

  if (message.type === "text") {
    return "fresh_text_candidate";
  }

  return "fresh_media_candidate";
};

const GROUP_FLAG_KEYS = ["isGroup", "isGroupMsg", "isGroupMessage"] as const;
const GROUP_ID_KEYS = ["from", "chatId", "remote", "remoteJid", "groupId", "_serialized"] as const;
const GROUP_METADATA_KEYS = ["key", "id", "chat", "_data", "data", "message", "msg"] as const;

const isEnabledGroupFlag = (value: unknown): boolean => {
  return value === true || value === 1 || (typeof value === "string" && value.trim().toLowerCase() === "true");
};

const isGroupIdentifier = (value: unknown, seen: WeakSet<object>, depth = 0): boolean => {
  if (typeof value === "string") {
    return value.trim().toLowerCase().includes("@g.us");
  }

  if (!value || typeof value !== "object" || depth > 3 || seen.has(value)) {
    return false;
  }

  seen.add(value);
  const record = value as Record<string, unknown>;
  if (typeof record.server === "string" && record.server.trim().toLowerCase() === "g.us") {
    return true;
  }

  return GROUP_ID_KEYS.some((key) => isGroupIdentifier(record[key], seen, depth + 1));
};

const hasGroupMetadata = (value: unknown, seen: WeakSet<object>, depth = 0): boolean => {
  if (!value || typeof value !== "object" || depth > 3 || seen.has(value)) {
    return false;
  }

  seen.add(value);
  const record = value as Record<string, unknown>;
  if (GROUP_FLAG_KEYS.some((key) => isEnabledGroupFlag(record[key]))) {
    return true;
  }

  if (GROUP_ID_KEYS.some((key) => isGroupIdentifier(record[key], new WeakSet<object>()))) {
    return true;
  }

  return GROUP_METADATA_KEYS.some((key) => hasGroupMetadata(record[key], seen, depth + 1));
};

export const isGroupMessage = (ctx: RawBotContext): boolean => {
  return hasGroupMetadata(ctx, new WeakSet<object>());
};

const normalizeActionsFromUnknown = (value: unknown): GatewayAction[] => {
  if (!value) return [];

  if (typeof value === "string") {
    const text = value.trim();
    return text ? [{ type: "text", text }] : [];
  }

  if (Array.isArray(value)) {
    return value.flatMap((item) => normalizeActionsFromUnknown(item));
  }

  if (typeof value !== "object") return [];

  const record = value as Record<string, unknown>;

  if (Array.isArray(record.actions)) {
    return record.actions.flatMap((item) => normalizeActionsFromUnknown(item));
  }

  if (record.type === "text" && typeof record.text === "string" && record.text.trim()) {
    return [{ type: "text", text: record.text }];
  }

  if (record.type === "media_binary") return [validateBinaryMedia(record)];
  if (record.type === "pos_menu") return [{ type: "pos_menu" }];

  if (record.type === "media" && typeof record.url === "string" && record.url.trim()) {
    return [
      {
        type: "media",
        url: record.url,
        caption: typeof record.caption === "string" ? record.caption : undefined,
      },
    ];
  }

  if ("json" in record) return normalizeActionsFromUnknown(record.json);
  if ("body" in record) return normalizeActionsFromUnknown(record.body);
  if ("data" in record) return normalizeActionsFromUnknown(record.data);
  if ("result" in record) return normalizeActionsFromUnknown(record.result);

  return [];
};

export const normalizeN8nResponse = (value: unknown): N8nBridgeResponse => {
  const record = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {};
  const turn = record.turn;
  return {
    actions: normalizeActionsFromUnknown(value),
    ...(turn && typeof turn.id === 'string' && ['none', 'committed', 'uncertain'].includes(turn.outcome)
      ? { turn: { id: turn.id, outcome: turn.outcome } } : {}),
  };
};
