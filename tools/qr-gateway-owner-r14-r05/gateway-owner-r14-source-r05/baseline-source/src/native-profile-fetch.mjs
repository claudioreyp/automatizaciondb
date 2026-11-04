// Transport composition only. Closed clients keep their reviewed request
// contracts; this private adapter binds their seven fixed paths to ONE trusted
// restaurant profile. It never parses message bodies or supplies authority.
const ORIGIN = 'https://n8n.srv1547104.hstgr.cloud';
const KEYS = ['origin', 'text', 'pull', 'authorize', 'observe', 'reserve', 'validate', 'complete'];
const LEGACY = {
  unsupported: '/webhook/escalar-native-baileys-c647/private-unsupported',
  text: '/webhook/escalar-native-baileys-c450/private-text',
  pull: '/webhook/escalar-native-baileys-c450/mailbox/pull',
  authorize: '/webhook/escalar-native-baileys-c450/mailbox/action-authorize',
  observe: '/webhook/escalar-native-baileys-c450/mailbox/ack-observe',
  reserve: '/webhook/escalar-native-baileys-c459/private-media/reserve',
  validate: '/webhook/escalar-native-baileys-c460/private-media/validate',
  complete: '/webhook/escalar-native-baileys-c459/private-media/complete',
};
const SUFFIX = { text: '/private-text', pull: '/mailbox/pull', authorize: '/mailbox/action-authorize', observe: '/mailbox/ack-observe',
  reserve: '/private-media/reserve', validate: '/private-media/validate', complete: '/private-media/complete' };
const need = (ok, code) => { if (!ok) throw Error(code); };
export function createNativeProfileFetch(options = {}) {
  // OFF must not inspect a private binding, callback or credential dependency.
  if (options.enabled !== true) return async () => { throw Error('NATIVE_PROFILE_TRANSPORT_OFF'); };
  const raw = options.endpoints;
  need(raw && typeof raw === 'object' && !Array.isArray(raw), 'NATIVE_PROFILE_ENDPOINTS_REQUIRED');
  const descriptors = Object.getOwnPropertyDescriptors(raw);
  const keys = [...KEYS, ...(Object.hasOwn(descriptors, 'unsupported') ? ['unsupported'] : [])];
  need(Reflect.ownKeys(descriptors).length === keys.length && keys.every(k => descriptors[k]?.enumerable === true
    && Object.hasOwn(descriptors[k], 'value') && typeof descriptors[k].value === 'string'), 'NATIVE_PROFILE_ENDPOINT_FIELDS');
  const endpoints = Object.fromEntries(keys.map(k => [k, descriptors[k].value]));
  need(endpoints.origin === ORIGIN, 'NATIVE_PROFILE_ORIGIN');
  const prefix = /^\/webhook\/([a-z][a-z0-9-]{7,99})\/private-text$/.exec(endpoints.text)?.[1];
  need(prefix && Object.entries(SUFFIX).every(([k, suffix]) => endpoints[k] === '/webhook/' + prefix + suffix), 'NATIVE_PROFILE_SINGLE_PREFIX');
  need(!Object.hasOwn(endpoints, 'unsupported') || ['/private-unsupported', '/private-unsupported-queue'].some(suffix => endpoints.unsupported === '/webhook/' + prefix + suffix), 'NATIVE_PROFILE_UNSUPPORTED_PREFIX');
  const fetchImpl = options.fetchImpl;
  need(typeof fetchImpl === 'function', 'NATIVE_PROFILE_FETCH_REQUIRED');
  const targets = new Map(Object.entries(LEGACY).filter(([k]) => Object.hasOwn(endpoints, k)).map(([k, path]) => [ORIGIN + path, ORIGIN + endpoints[k]]));
  return async (url, init) => {
    need(typeof url === 'string' && targets.has(url), 'NATIVE_PROFILE_FIXED_SOURCE_URL');
    need(init && init.method === 'POST' && init.redirect === 'error' && init.body !== undefined, 'NATIVE_PROFILE_ORIGINAL_POST_REQUIRED');
    // One call; all body bytes, headers, original signal, deadlines and response
    // ownership stay with the closed client. No clock reads, retries or renewal.
    return await fetchImpl(targets.get(url), init);
  };
}
