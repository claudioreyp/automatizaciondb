// Transport-only private text/location callback. No socket, QR, env, journal, model or
// business imports. Default OFF; construction and import perform no network IO.
import { request as httpsRequest } from "node:https";

const PATH = "/webhook/escalar-native-baileys-c450/private-text";
const HEADER = "X-Escalar-Native-Gateway";
const MAX_REQUEST_BYTES = 64 * 1024;
type JsonObject = Record<string, any>;

export interface NativeTextIngressConfig {
  https_origin: string;
  tenant: string;
  gateway_id: string;
  account_jid: string;
}
export interface NativeTextIngressCredential { header_name: string; header_value: string }
export interface NativeTextIngressRequest {
  envelope: JsonObject;
  source: { tenantId: string; account_jid: string; sender: string; message_id: string; key: JsonObject };
}
export interface NativeTextIngressReceipt {
  accepted: true;
  durable: true;
  tenantId: string;
  account_jid: string;
  sender: string;
  message_id: string;
  source_key: JsonObject;
  duplicate: boolean;
}
export interface NativeTextIngressHttpRequest {
  url: string;
  method: "POST";
  body: NativeTextIngressRequest;
  credential: NativeTextIngressCredential;
  timeoutMs: number;
  maxResponseBytes: number;
  redirect: "error";
  signal: AbortSignal;
}
export interface NativeTextIngressHttpResponse { statusCode: number; body: unknown; redirected: boolean; url: string }
export type NativeTextIngressHTTPS = (request: NativeTextIngressHttpRequest) => Promise<NativeTextIngressHttpResponse>;
export type NativeTextIngressClientOptions = { enabled?: false } | {
  enabled: true;
  config: NativeTextIngressConfig;
  readCredential(): NativeTextIngressCredential | Promise<NativeTextIngressCredential>;
  requestHTTPS?: NativeTextIngressHTTPS;
  readRealClock?: () => number;
  timeoutMs?: number;
  maxResponseBytes?: number;
};
export interface NativeTextIngressClient {
  enqueue(request: NativeTextIngressRequest): Promise<NativeTextIngressReceipt | { status: "OFF"; accepted: false; durable: false }>;
  close(): Promise<void>;
}

function need(ok: unknown, code: string): asserts ok { if (!ok) throw new Error(code); }
const object = (v: unknown): v is JsonObject => v !== null && typeof v === "object" && !Array.isArray(v);
const exact = (v: unknown, keys: string[]): v is JsonObject => object(v) && Object.keys(v).sort().join("|") === [...keys].sort().join("|");
const integer = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 0;
const identifier = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_.:-]{1,160}$/.test(v) && !v.includes("__UNBOUND");
function canonical(v: unknown): string {
  const ancestors = new Set<object>();
  const visit = (item: unknown): string => {
    if (item === null || typeof item === "string" || typeof item === "boolean") return JSON.stringify(item);
    if (typeof item === "number" && Number.isFinite(item)) return JSON.stringify(item);
    need(item && typeof item === "object" && !ancestors.has(item) && !Object.getOwnPropertySymbols(item).length, "NATIVE_TEXT_JSON");
    const ds = Object.getOwnPropertyDescriptors(item), names = Object.keys(ds);
    need(names.every(name => Object.hasOwn(ds[name], "value")), "NATIVE_TEXT_JSON");
    ancestors.add(item);
    let out: string;
    if (Array.isArray(item)) {
      need(names.length === item.length + 1 && names.includes("length"), "NATIVE_TEXT_JSON");
      out = "[" + Array.from({ length: item.length }, (_, i) => { need(ds[String(i)], "NATIVE_TEXT_JSON"); return visit(ds[String(i)].value); }).join(",") + "]";
    } else {
      need(names.every(name => ds[name].enumerable), "NATIVE_TEXT_JSON");
      out = "{" + names.sort().map(name => JSON.stringify(name) + ":" + visit(ds[name].value)).join(",") + "}";
    }
    ancestors.delete(item); return out;
  };
  return visit(v);
}
const own = <T>(v: T): T => JSON.parse(canonical(v));
const same = (a: unknown, b: unknown) => canonical(a) === canonical(b);
function jid(v: unknown) {
  if (typeof v !== "string") return null;
  const m = /^(\d+)(?::\d{1,5})?@(s\.whatsapp\.net|lid)$/.exec(v);
  if (!m || m[2] === "s.whatsapp.net" && !/^\d{8,15}$/.test(m[1]) || m[2] === "lid" && !/^\d{6,20}$/.test(m[1])) return null;
  return { account: m[1] + "@" + m[2], sender: m[1] + (m[2] === "lid" ? "@lid" : ""), kind: m[2] };
}
function originOf(raw: string) {
  let url: URL; try { url = new URL(raw); } catch { throw new Error("NATIVE_TEXT_HTTPS_ORIGIN"); }
  need(url.protocol === "https:" && url.origin === raw && url.pathname === "/" && !url.username && !url.password && !url.search && !url.hash, "NATIVE_TEXT_HTTPS_ORIGIN");
  return url.origin;
}
function credentialOf(value: NativeTextIngressCredential) {
  const c = own(value);
  need(exact(c, ["header_name", "header_value"]) && c.header_name === HEADER && typeof c.header_value === "string"
    && c.header_value.length >= 1 && c.header_value.length <= 4096 && !/[\r\n\0]/.test(c.header_value), "NATIVE_TEXT_PRIVATE_CREDENTIAL");
  return c;
}

// Adapted from frozen C448 createFixedHttpsMailboxRequest (source pin in the
// C450 review memo). Single fixed path, bounded bytes/deadline, TLS validation,
// no redirects. An already-started request may have committed before abort.
export function createFixedHttpsTextIngressRequest({ httpsOrigin }: { httpsOrigin: string }): NativeTextIngressHTTPS {
  const origin = originOf(httpsOrigin);
  return async input => {
    need(input.url === origin + PATH && input.method === "POST" && input.redirect === "error"
      && integer(input.timeoutMs) && input.timeoutMs >= 1 && input.timeoutMs <= 60_000
      && integer(input.maxResponseBytes) && input.maxResponseBytes >= 1024 && input.maxResponseBytes <= 64 * 1024
      && input.signal && !input.signal.aborted, "NATIVE_TEXT_FIXED_ENDPOINT");
    const auth = credentialOf(input.credential), data = Buffer.from(canonical(input.body));
    need(data.length <= MAX_REQUEST_BYTES && exact(input.body, ["envelope", "source"]), "NATIVE_TEXT_REQUEST_BYTES");
    return new Promise((resolve, reject) => {
      let settled = false, timer: ReturnType<typeof setTimeout> | undefined;
      const clear = () => { if (timer !== undefined) clearTimeout(timer); input.signal.removeEventListener("abort", abort); };
      const fail = () => { if (!settled) { settled = true; clear(); reject(new Error("NATIVE_TEXT_HTTPS_UNKNOWN")); } };
      const abort = () => { req.destroy(); fail(); };
      const req = httpsRequest(input.url, { method: "POST", rejectUnauthorized: true,
        headers: { "content-type": "application/json", "content-length": data.length, [auth.header_name]: auth.header_value } }, res => {
        if ((res.statusCode ?? 0) >= 300 && (res.statusCode ?? 0) < 400) { res.destroy(); req.destroy(); fail(); return; }
        let bytes = 0; const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => { bytes += chunk.length; if (bytes > input.maxResponseBytes) { res.destroy(); req.destroy(); fail(); } else chunks.push(chunk); });
        res.on("error", fail); res.on("end", () => {
          if (settled) return;
          try { const body: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8")); settled = true; clear(); resolve({ statusCode: res.statusCode ?? 0, body, redirected: false, url: input.url }); }
          catch { fail(); }
        });
      });
      req.on("error", fail); input.signal.addEventListener("abort", abort, { once: true });
      if (input.signal.aborted) { abort(); return; }
      timer = setTimeout(abort, input.timeoutMs); req.end(data);
    });
  };
}

export function createNativeTextIngressClient(options: NativeTextIngressClientOptions = {}): NativeTextIngressClient {
  // Do not inspect any other option (including getters) while OFF.
  if (options.enabled !== true) return { enqueue: async () => ({ status: "OFF", accepted: false, durable: false }), close: async () => {} };
  const cfg = own(options.config), timeoutMs = options.timeoutMs ?? 30_000, maxResponseBytes = options.maxResponseBytes ?? 64 * 1024;
  need(exact(cfg, ["https_origin", "tenant", "gateway_id", "account_jid"]) && identifier(cfg.tenant) && identifier(cfg.gateway_id)
    && jid(cfg.account_jid)?.account === cfg.account_jid && typeof options.readCredential === "function"
    && (options.requestHTTPS === undefined || typeof options.requestHTTPS === "function")
    && (options.readRealClock === undefined || typeof options.readRealClock === "function")
    && integer(timeoutMs) && timeoutMs >= 1 && timeoutMs <= 60_000
    && integer(maxResponseBytes) && maxResponseBytes >= 1024 && maxResponseBytes <= 64 * 1024, "NATIVE_TEXT_FIXED_CONFIGURATION");
  const url = originOf(cfg.https_origin) + PATH, readCredential = options.readCredential;
  const requestHTTPS = options.requestHTTPS ?? createFixedHttpsTextIngressRequest({ httpsOrigin: cfg.https_origin });
  const readClock = options.readRealClock ?? Date.now;
  const now = () => { const n = readClock(); need(integer(n), "NATIVE_TEXT_CLOCK"); return n; };
  let closed = false, epoch = 0;
  const active = new Set<{ cancel(): void }>();
  function checkedRequest(raw: NativeTextIngressRequest) {
    const body = own(raw); need(exact(body, ["envelope", "source"]) && Buffer.byteLength(canonical(body)) <= MAX_REQUEST_BYTES, "NATIVE_TEXT_BODY");
    const { source: s, envelope: e } = body;
    need(exact(s, ["tenantId", "account_jid", "sender", "message_id", "key"]) && s.tenantId === cfg.tenant && s.account_jid === cfg.account_jid
      && identifier(s.message_id) && s.sender === e?.from && s.message_id === e?.message?.id, "NATIVE_TEXT_SOURCE");
    const primary = jid(s.key?.remoteJid), alt = s.key?.remoteJidAlt == null ? null : jid(s.key.remoteJidAlt);
    const keyFields = ["remoteJid", "fromMe", "id", "participant", "remoteJidAlt", "participantAlt", "addressingMode", "isViewOnce"];
    need(object(s.key) && Buffer.byteLength(canonical(s.key)) <= 8192 && Object.keys(s.key).every(k => keyFields.includes(k)) && s.key.id === s.message_id && s.key.fromMe !== true
      && primary?.sender === s.sender && (s.key.remoteJidAlt == null || alt !== null)
      && !(alt && primary.kind === alt.kind && primary.sender !== alt.sender)
      && (s.key.participant == null || s.key.participant === "") && (s.key.participantAlt == null || s.key.participantAlt === ""), "NATIVE_TEXT_SOURCE_KEY");
    const phone = primary.kind === "s.whatsapp.net" ? primary.sender : alt?.kind === "s.whatsapp.net" ? alt.sender : null;
    const envelopeKeys = ["tenantId", "channel", "provider", "from", "name", "contact_display_name", "verified_phone", "message", "context", "raw", "location"];
    const textOnly = e?.message?.type === "text" && typeof e.message.text === "string" && !!e.message.text.trim() && e.message.text.length <= 4000 && !Object.hasOwn(e, "location");
    const locationOnly = e?.message?.type === "location" && e.message.text === "" && exact(e.location, ["latitude", "longitude"])
      && Number.isFinite(e.location.latitude) && e.location.latitude >= -90 && e.location.latitude <= 90
      && Number.isFinite(e.location.longitude) && e.location.longitude >= -180 && e.location.longitude <= 180;
    need(object(e) && Object.keys(e).every(k => envelopeKeys.includes(k)) && e.tenantId === cfg.tenant && e.provider === "baileys" && e.channel === "whatsapp"
      && exact(e.message, ["id", "type", "text", "timestamp"]) && (textOnly || locationOnly)
      && integer(e.message.timestamp) && exact(e.context, ["isGroup", "hasMedia"]) && e.context.isGroup === false && e.context.hasMedia === false
      && exact(e.raw, []) && (e.verified_phone || null) === phone
      && typeof e.name === "string" && e.name.length <= 120 && (e.contact_display_name === null || typeof e.contact_display_name === "string" && e.contact_display_name.length <= 120), "NATIVE_TEXT_TEXT_OR_LOCATION_ONLY");
    return body;
  }
  return {
    async enqueue(raw) {
      need(!closed, "NATIVE_TEXT_CLOSED");
      const body = checkedRequest(raw), expectedEpoch = epoch, started = now(), deadline = started + timeoutMs;
      need(Number.isSafeInteger(deadline), "NATIVE_TEXT_CLOCK");
      const controller = new AbortController(); let expired = false, timer: ReturnType<typeof setTimeout> | undefined;
      let rejectCancellation!: (error: Error) => void;
      const cancelled = new Promise<never>((_, reject) => { rejectCancellation = reject; });
      const operation = { cancel() { expired = true; controller.abort(); rejectCancellation(new Error("NATIVE_TEXT_CLOSED")); } };
      active.add(operation);
      const fence = () => { const at = now(); need(!closed && epoch === expectedEpoch && !expired && at >= started && at < deadline, "NATIVE_TEXT_DEADLINE_OR_CLOSED"); return deadline - at; };
      try {
        timer = setTimeout(() => { expired = true; controller.abort(); rejectCancellation(new Error("NATIVE_TEXT_DEADLINE")); }, timeoutMs);
        const response = await Promise.race([Promise.resolve().then(async () => {
          fence(); const credential = credentialOf(await readCredential()); const remaining = fence();
          const value = await requestHTTPS({ url, method: "POST", body: own(body), credential, timeoutMs: remaining,
            maxResponseBytes, redirect: "error", signal: controller.signal });
          fence(); return own(value);
        }), cancelled]);
        fence(); need(exact(response, ["statusCode", "body", "redirected", "url"]) && response.statusCode === 200 && response.redirected === false
          && response.url === url && Buffer.byteLength(canonical(response.body)) <= maxResponseBytes, "NATIVE_TEXT_HTTP_RECEIPT");
        const receipt = response.body;
        need(exact(receipt, ["accepted", "durable", "tenantId", "account_jid", "sender", "message_id", "source_key", "duplicate"])
          && receipt.accepted === true && receipt.durable === true && typeof receipt.duplicate === "boolean"
          && receipt.tenantId === body.source.tenantId && receipt.account_jid === body.source.account_jid && receipt.sender === body.source.sender
          && receipt.message_id === body.source.message_id && same(receipt.source_key, body.source.key), "NATIVE_TEXT_DURABLE_RECEIPT_REQUIRED");
        // Return the backend object intact after validation. No public result
        // flag is promoted to a durable receipt by the client.
        return receipt as NativeTextIngressReceipt;
      } catch { throw new Error("NATIVE_TEXT_INGRESS_UNCONFIRMED_NO_RETRY"); }
      finally { expired = true; if (timer !== undefined) clearTimeout(timer); controller.abort(); active.delete(operation); }
    },
    async close() { closed = true; epoch++; for (const operation of active) operation.cancel(); },
  };
}
