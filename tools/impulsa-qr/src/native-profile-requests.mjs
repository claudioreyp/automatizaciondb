import { createNativeProfileFetch } from './native-profile-fetch.mjs';

// HTTPS transport only. Inject through the existing clients' requestHTTPS seam.
// Their source/receipt/CAS checks remain authoritative. No POS/model/provider,
// filesystem, environment lookup, automatic retry or message-level decision.
const ORIGIN = 'https://n8n.srv1547104.hstgr.cloud';
const PATHS = {
  unsupported: '/webhook/escalar-native-baileys-c647/private-unsupported',
  text: '/webhook/escalar-native-baileys-c450/private-text',
  pull: '/webhook/escalar-native-baileys-c450/mailbox/pull',
  authorize: '/webhook/escalar-native-baileys-c450/mailbox/action-authorize',
  observe: '/webhook/escalar-native-baileys-c450/mailbox/ack-observe',
  reserve: '/webhook/escalar-native-baileys-c459/private-media/reserve',
  validate: '/webhook/escalar-native-baileys-c460/private-media/validate',
  complete: '/webhook/escalar-native-baileys-c459/private-media/complete',
};
const ROLES = ['text', 'mailbox', 'reserve', 'validate', 'complete', 'unsupported'];
const need = (ok, code) => { if (!ok) throw Error(code); };
const integer = n => Number.isSafeInteger(n) && n >= 0;
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const exact = (v, fields) => object(v) && Reflect.ownKeys(v).length === fields.length
  && fields.every(k => Object.hasOwn(v, k));
function dataJSON(value) {
  const ancestors = new Set();
  function visit(v, depth = 0) {
    need(depth <= 32, 'PROFILE_REQUEST_JSON_DEPTH');
    if (v === null || typeof v === 'string' || typeof v === 'boolean') return JSON.stringify(v);
    if (typeof v === 'number') { need(Number.isFinite(v), 'PROFILE_REQUEST_JSON_NUMBER'); return JSON.stringify(v); }
    need(v && typeof v === 'object' && !ancestors.has(v) && !Object.getOwnPropertySymbols(v).length, 'PROFILE_REQUEST_JSON_DATA');
    const d = Object.getOwnPropertyDescriptors(v), keys = Object.keys(d);
    need(keys.every(k => Object.hasOwn(d[k], 'value')), 'PROFILE_REQUEST_JSON_ACCESSOR');
    ancestors.add(v); let result;
    if (Array.isArray(v)) {
      const length = d.length.value;
      need(integer(length) && length <= 10000 && keys.length === length + 1, 'PROFILE_REQUEST_JSON_ARRAY');
      result = '[' + Array.from({ length }, (_, i) => { need(d[i]?.enumerable, 'PROFILE_REQUEST_JSON_ARRAY'); return visit(d[i].value, depth + 1); }).join(',') + ']';
    } else {
      need(keys.every(k => d[k].enumerable), 'PROFILE_REQUEST_JSON_ENUMERABLE');
      result = '{' + keys.sort().map(k => JSON.stringify(k) + ':' + visit(d[k].value, depth + 1)).join(',') + '}';
    }
    ancestors.delete(v); return result;
  }
  return visit(value);
}
function authOf(raw) {
  const a = JSON.parse(dataJSON(raw));
  need(exact(a, ['header_name', 'header_value']) && a.header_name === 'X-Escalar-Native-Gateway'
    && typeof a.header_value === 'string' && a.header_value.length >= 1 && a.header_value.length <= 4096
    && !/[\r\n\0]/.test(a.header_value), 'PROFILE_REQUEST_CREDENTIAL');
  return a;
}
function requestBody(role, body) {
  if (role !== 'complete') {
    const json = dataJSON(body), value = JSON.parse(json);
    need(object(value) && Buffer.byteLength(json) <= (role === 'mailbox' ? 128 * 1024 * 1024 : 65536), 'PROFILE_REQUEST_BODY_LIMIT');
    if (role === 'text' || role === 'reserve') need(exact(value, ['envelope', 'source']), 'PROFILE_REQUEST_BODY_FIELDS');
    if (role === 'unsupported') need(exact(value, ['record', 'source']), 'PROFILE_REQUEST_BODY_FIELDS');
    if (role === 'validate') need(exact(value, ['envelope', 'source', 'reservation']), 'PROFILE_REQUEST_BODY_FIELDS');
    return { body: json, json: true };
  }
  need(body instanceof FormData, 'PROFILE_REQUEST_FORM');
  const p = [...body.entries()];
  need(p.length === 2 && p[0][0] === 'metadata' && typeof p[0][1] === 'string' && Buffer.byteLength(p[0][1]) <= 65536
    && p[1][0] === 'file' && p[1][1] instanceof Blob && p[1][1].size >= 1 && p[1][1].size <= 8388608
    && ['image/png', 'image/jpeg', 'image/webp'].includes(p[1][1].type), 'PROFILE_REQUEST_FORM_BOUNDS');
  const metadata = JSON.parse(p[0][1]);
  need(exact(metadata, ['envelope', 'source', 'reservation']) && metadata.envelope?.media_reservation?.mime_type === p[1][1].type, 'PROFILE_REQUEST_FORM_IDENTITY');
  return { body, json: false };
}
export function createNativeProfileRequests(options = {}) {
  if (options.enabled !== true) return Object.freeze({
    ...Object.fromEntries(ROLES.map(k => [k, async () => { throw Error('PROFILE_REQUESTS_OFF'); }])),
    close: async () => {}, inspectMetadata: () => ({ enabled: false, status: 'OFF', active: 0, wire_pending: 0 }),
  });
  const rawEndpoints = options.endpoints;
  const mappedFetch = createNativeProfileFetch({ enabled: true, endpoints: rawEndpoints, fetchImpl: options.fetchImpl ?? globalThis.fetch });
  const endpoints = Object.fromEntries(Object.entries(Object.getOwnPropertyDescriptors(rawEndpoints)).map(([k, d]) => [k, d.value]));
  const cleanupMs = options.cleanupTimeoutMs ?? 1000;
  need(integer(cleanupMs) && cleanupMs >= 1 && cleanupMs <= 1000, 'PROFILE_REQUEST_CLEANUP_LIMIT');
  const active = new Set(), wires = new Set(), consumed = new WeakSet();
  let closed = false, closeUnconfirmed = false, closePromise;
  const now = () => { const n = Date.now(); need(integer(n), 'PROFILE_REQUEST_CLOCK'); return n; };
  async function request(role, input) {
    need(!closed, 'PROFILE_REQUESTS_CLOSED');
    const fields = ['url', 'method', 'body', 'credential', 'timeoutMs', 'maxResponseBytes', 'redirect'];
    if (role !== 'mailbox' || Object.hasOwn(input ?? {}, 'signal')) fields.push('signal');
    need(exact(input, fields) && Object.values(Object.getOwnPropertyDescriptors(input)).every(d => Object.hasOwn(d, 'value'))
      && !consumed.has(input), 'PROFILE_REQUEST_INPUT');
    consumed.add(input);
    const kind = role === 'mailbox' ? ['pull', 'authorize', 'observe'].find(k => input.url === ORIGIN + PATHS[k]) : role;
    need(kind && input.url === ORIGIN + PATHS[kind] && input.method === 'POST' && input.redirect === 'error', 'PROFILE_REQUEST_FIXED_ROUTE');
    need(typeof endpoints[kind] === 'string', 'PROFILE_REQUEST_ROUTE_NOT_CONFIGURED');
    need(integer(input.timeoutMs) && input.timeoutMs >= 1 && input.timeoutMs <= 60000 && integer(input.maxResponseBytes)
      && input.maxResponseBytes >= 1024 && input.maxResponseBytes <= (role === 'mailbox' ? 128 * 1024 * 1024 : 65536), 'PROFILE_REQUEST_LIMITS');
    const signal = input.signal;
    need((role === 'mailbox' && signal === undefined) || signal && typeof signal.addEventListener === 'function'
      && typeof signal.removeEventListener === 'function' && signal.aborted === false, 'PROFILE_REQUEST_ORIGINAL_SIGNAL');
    const auth = authOf(input.credential), payload = requestBody(role, input.body), started = now(), deadline = started + input.timeoutMs;
    need(integer(deadline), 'PROFILE_REQUEST_DEADLINE');
    const controller = new AbortController();
    let stopped = false, timer, reader, response, cancelBodyPromise, rejectCancelled;
    const cancelled = new Promise((_, reject) => { rejectCancelled = reject; });
    const cancelBody = () => {
      if (cancelBodyPromise) return cancelBodyPromise;
      const target = reader ?? response?.body;
      if (target && typeof target.cancel === 'function') cancelBodyPromise = Promise.resolve().then(() => target.cancel()).catch(() => { closeUnconfirmed = true; });
      return cancelBodyPromise;
    };
    const abort = () => {
      if (stopped) return;
      stopped = true; controller.abort(); void cancelBody(); rejectCancelled(Error('PROFILE_REQUEST_UNCONFIRMED_NO_RETRY'));
    };
    const op = { abort }; active.add(op);
    const fence = () => { const t = now(); need(!closed && !stopped && !signal?.aborted && t >= started && t < deadline, 'PROFILE_REQUEST_ORIGINAL_DEADLINE'); };
    signal?.addEventListener('abort', abort, { once: true });
    timer = setTimeout(abort, input.timeoutMs);
    const wire = Promise.resolve().then(async () => {
      try {
        fence();
        response = await mappedFetch(input.url, { method: 'POST', redirect: 'error', credentials: 'omit', cache: 'no-store',
          headers: { [auth.header_name]: auth.header_value, ...(payload.json ? { 'content-type': 'application/json' } : {}) },
          body: payload.body, signal: controller.signal });
        fence();
        need(response && Number.isInteger(response.status) && response.status >= 100 && response.status <= 599
          && (response.status < 300 || response.status >= 400), 'PROFILE_REQUEST_HTTP_STATUS');
        need(response.redirected === false && response.url === endpoints.origin + endpoints[kind]
          && response.body && typeof response.body.getReader === 'function', 'PROFILE_REQUEST_RESPONSE_TARGET');
        reader = response.body.getReader();
        let size = 0; const chunks = [];
        while (true) {
          fence(); const part = await reader.read(); fence(); if (part.done) break;
          need(part.value instanceof Uint8Array, 'PROFILE_REQUEST_RESPONSE_BYTES'); size += part.value.byteLength;
          need(size <= input.maxResponseBytes, 'PROFILE_REQUEST_RESPONSE_LIMIT'); chunks.push(part.value);
        }
        const bytes = new Uint8Array(size); let offset = 0;
        for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
        const body = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
        fence(); return { statusCode: response.status, body, redirected: false, url: input.url };
      } finally { await cancelBody(); }
    });
    wires.add(wire); void wire.then(() => wires.delete(wire), () => wires.delete(wire));
    try { if (signal?.aborted) abort(); return await Promise.race([wire, cancelled]); }
    catch { throw Error('PROFILE_REQUEST_UNCONFIRMED_NO_RETRY'); }
    finally { stopped = true; clearTimeout(timer); signal?.removeEventListener('abort', abort); controller.abort(); void cancelBody(); active.delete(op); }
  }
  function close() {
    if (closePromise) return closePromise;
    let resolveClose, rejectClose;
    closePromise = new Promise((resolve, reject) => { resolveClose = resolve; rejectClose = reject; });
    closed = true; // Publish the promise/latch before abort callbacks can re-enter.
    for (const op of [...active]) op.abort();
    void (async () => {
      let timer;
      try {
        await Promise.race([Promise.allSettled([...wires]), new Promise((_, reject) => { timer = setTimeout(() => reject(Error('PROFILE_REQUEST_CLOSE_UNCONFIRMED')), cleanupMs); })]);
        need(wires.size === 0 && !closeUnconfirmed, 'PROFILE_REQUEST_CLOSE_UNCONFIRMED'); resolveClose();
      } catch { closeUnconfirmed = true; rejectClose(Error('PROFILE_REQUEST_CLOSE_UNCONFIRMED')); }
      finally { clearTimeout(timer); }
    })();
    return closePromise;
  }
  return Object.freeze({ ...Object.fromEntries(ROLES.map(role => [role, input => request(role, input)])), close,
    inspectMetadata: () => ({ enabled: true, status: closeUnconfirmed ? 'CLOSE_UNCONFIRMED' : closed ? 'CLOSED' : 'OPEN', active: active.size, wire_pending: wires.size }),
  });
}
