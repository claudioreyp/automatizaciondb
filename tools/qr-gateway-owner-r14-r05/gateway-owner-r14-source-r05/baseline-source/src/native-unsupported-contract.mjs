// Pure metadata contract, shared with the native n8n Code adapter. An unsupported
// file contributes no instructions, bytes, URL, payment evidence or authority.
export function createNativeUnsupportedContract({ tenant, account_jid } = {}) {
  const need = (ok, code) => { if (!ok) throw Error(code); };
  const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
  const exact = (v, keys) => object(v) && Reflect.ownKeys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k));
  const identifier = v => typeof v === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(v) && !v.includes('__UNBOUND');
  need(typeof tenant === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(tenant) && !tenant.includes('__UNBOUND')
    && typeof account_jid === 'string' && /^\d{8,15}@s\.whatsapp\.net$/.test(account_jid), 'UNSUPPORTED_FIXED_CONTEXT');
  function canonical(value) {
    const ancestors = new Set();
    function visit(v, depth = 0) {
      need(depth <= 16, 'UNSUPPORTED_JSON_DEPTH');
      if (v === null || typeof v === 'string' || typeof v === 'boolean') return JSON.stringify(v);
      if (typeof v === 'number') { need(Number.isFinite(v), 'UNSUPPORTED_JSON_NUMBER'); return JSON.stringify(v); }
      need(object(v) && !ancestors.has(v) && !Object.getOwnPropertySymbols(v).length, 'UNSUPPORTED_JSON_DATA');
      const ds = Object.getOwnPropertyDescriptors(v), keys = Object.keys(ds);
      need(keys.every(k => ds[k].enumerable && Object.hasOwn(ds[k], 'value')), 'UNSUPPORTED_JSON_ACCESSOR');
      ancestors.add(v); const result = '{' + keys.sort().map(k => JSON.stringify(k) + ':' + visit(ds[k].value, depth + 1)).join(',') + '}';
      ancestors.delete(v); return result;
    }
    return visit(value);
  }
  const own = value => JSON.parse(canonical(value)), same = (a, b) => canonical(a) === canonical(b);
  function jid(v) {
    if (typeof v !== 'string') return null;
    const m = /^(\d+)(?::\d{1,5})?@(s\.whatsapp\.net|lid)$/.exec(v);
    if (!m || !(m[2] === 'lid' ? /^\d{6,20}$/ : /^\d{8,15}$/).test(m[1])) return null;
    return { kind: m[2], sender: m[1] + (m[2] === 'lid' ? '@lid' : '') };
  }
  function check(raw) {
    const body = own(raw); need(exact(body, ['record', 'source']), 'UNSUPPORTED_BODY');
    const { record: r, source: s } = body;
    need(exact(s, ['tenantId', 'account_jid', 'sender', 'message_id', 'key']) && s.tenantId === tenant && s.account_jid === account_jid
      && identifier(s.message_id), 'UNSUPPORTED_SOURCE');
    const key = s.key, allowed = ['remoteJid', 'fromMe', 'id', 'participant', 'remoteJidAlt', 'participantAlt', 'addressingMode', 'isViewOnce'];
    const p = jid(key?.remoteJid), alt = key?.remoteJidAlt == null ? null : jid(key.remoteJidAlt);
    need(object(key) && canonical(key).length <= 8192 && Object.keys(key).every(k => allowed.includes(k)) && p && p.sender === s.sender
      && key.id === s.message_id && (key.fromMe == null || key.fromMe === false) && (key.remoteJidAlt == null || alt)
      && !(alt && p.kind === alt.kind && p.sender !== alt.sender) && (key.participant == null || key.participant === '')
      && (key.participantAlt == null || key.participantAlt === '') && (key.addressingMode == null || ['pn', 'lid'].includes(key.addressingMode))
      && (key.isViewOnce == null || typeof key.isViewOnce === 'boolean'), 'UNSUPPORTED_SOURCE_KEY');
    const fields = ['index', 'message_id', 'sender', 'kind', ...(Object.hasOwn(r ?? {}, 'mime_type') ? ['mime_type'] : [])];
    need(exact(r, fields) && Number.isSafeInteger(r.index) && r.index >= 0 && r.index < 100 && r.message_id === s.message_id && r.sender === s.sender
      && ['audioMessage', 'videoMessage', 'documentMessage', 'imageMessage', 'stickerMessage'].includes(r.kind)
      && (!Object.hasOwn(r, 'mime_type') || typeof r.mime_type === 'string' && r.mime_type.length <= 100 && !/[\x00-\x1f\x7f]/.test(r.mime_type)), 'UNSUPPORTED_RECORD');
    need(!['imageMessage', 'stickerMessage'].includes(r.kind) || !['image/png', 'image/jpeg', 'image/webp'].includes(r.mime_type), 'UNSUPPORTED_SUPPORTED_IMAGE');
    return body;
  }
  function checkReceipt(raw, request) {
    const r = own(raw), s = check(request).source;
    need(exact(r, ['accepted', 'durable', 'tenantId', 'account_jid', 'sender', 'message_id', 'source_key', 'duplicate'])
      && r.accepted === true && r.durable === true && typeof r.duplicate === 'boolean'
      && r.tenantId === s.tenantId && r.account_jid === s.account_jid && r.sender === s.sender && r.message_id === s.message_id
      && same(r.source_key, s.key), 'UNSUPPORTED_DURABLE_RECEIPT');
    return r;
  }
  return Object.freeze({ check, checkReceipt, canonical, own });
}
