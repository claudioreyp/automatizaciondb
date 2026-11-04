import { createNativeUnsupportedContract } from './native-unsupported-contract.mjs';

const PATH = '/webhook/escalar-native-baileys-c647/private-unsupported';
const need = (ok, code) => { if (!ok) throw Error(code); };
// Metadata transport only. The request adapter owns the HTTPS wire and its
// shutdown; this client owns the original caller deadline and durable receipt.
export function createNativeUnsupportedClient(options = {}) {
  if (options.enabled !== true) return Object.freeze({
    recordUnsupported: async () => ({ status: 'OFF', accepted: false, durable: false }), close: async () => {},
  });
  const { config, readCredential, requestHTTPS } = options;
  const contract = createNativeUnsupportedContract(config), own = contract.own;
  const cfg = own(config);
  need(Object.keys(cfg).sort().join('|') === 'account_jid|https_origin|tenant'
    && cfg.https_origin === 'https://n8n.srv1547104.hstgr.cloud'
    && typeof readCredential === 'function' && typeof requestHTTPS === 'function', 'UNSUPPORTED_CLIENT_CONFIGURATION');
  const timeoutMs = options.timeoutMs ?? 10000;
  need(Number.isSafeInteger(timeoutMs) && timeoutMs >= 1 && timeoutMs <= 60000, 'UNSUPPORTED_CLIENT_LIMIT');
  const url = cfg.https_origin + PATH, active = new Set();
  let closed = false, closePromise;
  const now = () => { const t = Date.now(); need(Number.isSafeInteger(t) && t >= 0, 'UNSUPPORTED_CLIENT_CLOCK'); return t; };
  async function recordUnsupported(raw) {
    need(!closed, 'UNSUPPORTED_CLIENT_CLOSED');
    const body = contract.check(raw);
    need(Buffer.byteLength(contract.canonical(body)) <= 65536, 'UNSUPPORTED_CLIENT_BODY_LIMIT');
    const started = now(), deadline = started + timeoutMs, controller = new AbortController();
    let expired = false, rejectCancel, timer;
    const cancelled = new Promise((_, reject) => { rejectCancel = reject; });
    const op = { cancel() { expired = true; controller.abort(); rejectCancel(Error('UNSUPPORTED_CLIENT_CANCELLED')); } };
    active.add(op);
    const fence = () => { const t = now(); need(!closed && !expired && t >= started && t < deadline, 'UNSUPPORTED_CLIENT_ORIGINAL_DEADLINE'); return deadline - t; };
    try {
      timer = setTimeout(() => op.cancel(), timeoutMs);
      const response = await Promise.race([Promise.resolve().then(async () => {
        fence(); const credential = own(await readCredential());
        need(Object.keys(credential).sort().join('|') === 'header_name|header_value'
          && credential.header_name === 'X-Escalar-Native-Gateway' && typeof credential.header_value === 'string'
          && credential.header_value.length >= 1 && credential.header_value.length <= 4096 && !/[\r\n\0]/.test(credential.header_value), 'UNSUPPORTED_CLIENT_CREDENTIAL');
        return requestHTTPS({ url, method: 'POST', body, credential, timeoutMs: fence(), maxResponseBytes: 65536, redirect: 'error', signal: controller.signal });
      }), cancelled]);
      fence(); const r = own(response);
      need(Object.keys(r).sort().join('|') === 'body|redirected|statusCode|url' && r.statusCode === 200 && r.redirected === false
        && r.url === url && Buffer.byteLength(contract.canonical(r.body)) <= 65536, 'UNSUPPORTED_CLIENT_HTTP_RECEIPT');
      return contract.checkReceipt(r.body, body);
    } catch { throw Error('UNSUPPORTED_CLIENT_UNCONFIRMED_NO_RETRY'); }
    finally { expired = true; clearTimeout(timer); controller.abort(); active.delete(op); }
  }
  function close() {
    if (closePromise) return closePromise;
    // Publish the latch before abort listeners can call close again.
    closePromise = Promise.resolve(); closed = true;
    for (const op of [...active]) op.cancel();
    return closePromise;
  }
  return Object.freeze({ recordUnsupported, close });
}
