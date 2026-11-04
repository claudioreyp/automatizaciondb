// Receipt for API queue admission. A technical-custody receipt is insufficient.
// No file content, merchant instruction, model output or commercial authority.
export function createNativeQueuedUnsupportedContract({ tenant, account_jid, scope, sha256 } = {}) {
  const source = createNativeUnsupportedContract({ tenant, account_jid });
  const need = (ok, code) => { if (!ok) throw Error(code); };
  const exact = (v, keys) => v && typeof v === 'object' && !Array.isArray(v)
    && Object.keys(v).sort().join('|') === [...keys].sort().join('|');
  need(exact(scope, ['business_id', 'branch_id']) && Object.values(scope).every(n => Number.isSafeInteger(n) && n > 0)
    && typeof sha256 === 'function', 'UNSUPPORTED_QUEUE_FIXED_SCOPE');
  const fixed = source.own(scope);
  function metadataHash(raw) {
    const value = source.check(raw); delete value.record.index;
    const hash = sha256(source.canonical(value));
    need(typeof hash === 'string' && /^[a-f0-9]{64}$/.test(hash), 'UNSUPPORTED_QUEUE_METADATA_HASH');
    return hash;
  }
  function checkReceipt(raw, request) {
    const r = source.own(raw), s = source.check(request).source, q = r.queue;
    need(exact(r, ['schema', 'accepted', 'durable', 'tenantId', 'account_jid', 'sender', 'message_id', 'source_key', 'queue'])
      && r.schema === 'escalarai.unsupported-api-queue.v1' && r.accepted === true && r.durable === true
      && r.tenantId === s.tenantId && r.account_jid === s.account_jid && r.sender === s.sender && r.message_id === s.message_id
      && source.canonical(r.source_key) === source.canonical(s.key)
      && exact(q, ['business_id', 'branch_id', 'conversation_id', 'status', 'metadata_sha256'])
      && q.business_id === fixed.business_id && q.branch_id === fixed.branch_id
      && typeof q.conversation_id === 'string' && /^[A-Za-z0-9-]{1,64}$/.test(q.conversation_id)
      && ['queued', 'completed'].includes(q.status) && q.metadata_sha256 === metadataHash(request), 'UNSUPPORTED_API_QUEUE_RECEIPT');
    return r;
  }
  function receipt(request, response) {
    const s = source.check(request).source, r = response;
    need(r && typeof r === 'object' && !Array.isArray(r)
      && (r.status === 'queued' && Number.isSafeInteger(r.pending_count) && r.pending_count >= 0 && r.pending_count <= 30
        || r.status === 'completed' && r.duplicate === true && Number.isSafeInteger(r.version) && r.version > 0), 'UNSUPPORTED_API_QUEUE_NOT_CONFIRMED');
    return checkReceipt({ schema: 'escalarai.unsupported-api-queue.v1', accepted: true, durable: true,
      tenantId: s.tenantId, account_jid: s.account_jid, sender: s.sender, message_id: s.message_id, source_key: s.key,
      queue: { ...fixed, conversation_id: r.conversation_id, status: r.status, metadata_sha256: metadataHash(request) } }, request);
  }
  return Object.freeze({ check: source.check, own: source.own, canonical: source.canonical, metadataHash, receipt, checkReceipt });
}
import { createNativeUnsupportedContract } from './native-unsupported-contract.mjs';
