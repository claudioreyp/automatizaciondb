import { request } from 'node:https';
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
const ORIGIN = 'https://n8n.srv1547104.hstgr.cloud';
const need = (ok, code) => { if (!ok) throw Error(code); };
const inside = (path, root) => { const r = relative(root, path); return r && !isAbsolute(r) && r !== '..' && !r.startsWith('..' + sep); };

export function validateQrViewerProfile(input, profileDirectory) {
  const p = structuredClone(input);
  need(p && typeof p === 'object' && Object.keys(p).sort().join('|') === ['schema','https_origin','health_path','image_path','credential_file','scope'].sort().join('|'), 'QR_VIEWER_PROFILE_FIELDS');
  need(p.schema === 'escalarai.impulsa-qr-viewer.v1' && p.https_origin === ORIGIN, 'QR_VIEWER_PROFILE_ORIGIN');
  const prefix = /^\/webhook\/([a-z][a-z0-9-]{7,99})\/qr\/health$/.exec(p.health_path)?.[1];
  need(prefix && p.image_path === '/webhook/' + prefix + '/qr/image', 'QR_VIEWER_PROFILE_ROUTE_PAIR');
  need(p.scope && Object.keys(p.scope).sort().join('|') === 'branch_id|business_id' && Object.values(p.scope).every(v => Number.isSafeInteger(v) && v > 0), 'QR_VIEWER_PROFILE_SCOPE');
  need(isAbsolute(profileDirectory) && typeof p.credential_file === 'string' && isAbsolute(p.credential_file) && inside(resolve(p.credential_file), resolve(profileDirectory)), 'QR_VIEWER_CREDENTIAL_REFERENCE');
  Object.freeze(p.scope); return Object.freeze(p);
}
export function loadQrViewerProfile(file) {
  need(typeof file === 'string' && isAbsolute(file), 'QR_VIEWER_ABSOLUTE_PROFILE');
  const stat = lstatSync(file);
  need(stat.isFile() && !stat.isSymbolicLink() && stat.size > 0 && stat.size <= 65536, 'QR_VIEWER_BOUNDED_PROFILE');
  const parent = realpathSync(dirname(file));
  need(inside(realpathSync(file), parent), 'QR_VIEWER_PROFILE_LINK_ESCAPE');
  return validateQrViewerProfile(JSON.parse(readFileSync(file, 'utf8')), parent);
}
export function readQrViewerCredential(profile, privateDirectory) {
  const file = profile.credential_file, stat = lstatSync(file);
  need(stat.isFile() && !stat.isSymbolicLink() && stat.size > 0 && stat.size <= 65536 && inside(realpathSync(file), realpathSync(privateDirectory)), 'QR_VIEWER_PRIVATE_CREDENTIAL_FILE');
  const value = JSON.parse(readFileSync(file, 'utf8'));
  need(value?.header_name === 'X-Escalar-Native-Gateway' && typeof value.header_value === 'string' && value.header_value.length >= 1 && value.header_value.length <= 4096 && !/[\r\n\0]/.test(value.header_value), 'QR_VIEWER_PRIVATE_CREDENTIAL_FIELDS');
  return { header_name: value.header_name, header_value: value.header_value };
}

/** GET-only, bounded HTTPS. Never redirects or opens a WhatsApp connection. */
export function requestQrViewerHttps(url, { credential, maxBytes, timeoutMs = 8000 }) {
  return new Promise((resolveResult, reject) => {
    let done = false, timer;
    const fail = () => { if (!done) { done = true; clearTimeout(timer); reject(Error('QR_VIEWER_UPSTREAM_UNAVAILABLE')); } };
    const req = request(url, { method: 'GET', rejectUnauthorized: true, headers: { [credential.header_name]: credential.header_value } }, res => {
      if (res.statusCode >= 300 && res.statusCode < 400) { res.destroy(); req.destroy(); fail(); return; }
      const chunks = []; let size = 0;
      res.on('data', chunk => { size += chunk.length; if (size > maxBytes) { res.destroy(); req.destroy(); fail(); } else chunks.push(chunk); });
      res.on('error', fail);
      res.on('end', () => { if (done) return; done = true; clearTimeout(timer); resolveResult({ statusCode: res.statusCode, contentType: res.headers['content-type'], body: Buffer.concat(chunks) }); });
    });
    req.on('error', fail); timer = setTimeout(() => { req.destroy(); fail(); }, timeoutMs); req.end();
  });
}
export function createQrViewerClient({ profile, readCredential, requestHTTPS = requestQrViewerHttps }) {
  need(profile?.https_origin === ORIGIN && typeof readCredential === 'function' && typeof requestHTTPS === 'function', 'QR_VIEWER_CLIENT_BINDINGS');
  profile = validateQrViewerProfile(profile, dirname(profile.credential_file));
  return { async readState(includeQr = false) {
    const credential = readCredential();
    need(credential?.header_name === 'X-Escalar-Native-Gateway' && typeof credential.header_value === 'string' && credential.header_value.length > 0 && credential.header_value.length <= 4096 && !/[\r\n\0]/.test(credential.header_value), 'QR_VIEWER_CREDENTIAL');
    const response = await requestHTTPS(ORIGIN + profile.health_path, { credential, maxBytes: 65536 });
    need(response.statusCode === 200 && response.body instanceof Uint8Array && response.body.length <= 65536, 'QR_VIEWER_HEALTH_UNAVAILABLE');
    const raw = JSON.parse(Buffer.from(response.body).toString('utf8'));
    need(raw.browserProcess === false && ['initializing','awaiting_qr','connected','disconnected','error'].includes(raw.status)
      && ['active','paused','attention_required'].includes(raw.consumerMessaging) && typeof raw.nativeTransport === 'string'
      && Number.isSafeInteger(raw.qrGeneration) && raw.qrGeneration >= 0
      && raw.nativeScope?.business_id === profile.scope.business_id && raw.nativeScope?.branch_id === profile.scope.branch_id, 'QR_VIEWER_SCOPE_OR_HEALTH');
    need(raw.consumerMessaging !== 'active' || (raw.status === 'connected' && ['BOUND','IDLE','ACK_DURABLE'].includes(raw.nativeTransport)), 'QR_VIEWER_INCONSISTENT_ACTIVE_STATE');
    const state = { status: raw.status, qrGeneration: raw.qrGeneration, consumerMessaging: raw.consumerMessaging,
      nativeTransport: raw.nativeTransport, connected: raw.status === 'connected', browserProcess: false,
      localProvider: false, viewerMode: 'remote_qr_viewer', n8nForwarding: raw.consumerMessaging === 'active' };
    if (includeQr) {
      state.qr = null;
      if (raw.status === 'awaiting_qr') {
        const image = await requestHTTPS(ORIGIN + profile.image_path + '?generation=' + raw.qrGeneration, { credential, maxBytes: 512 * 1024 });
        if (![404,409].includes(image.statusCode)) {
          need(image.statusCode === 200 && /^image\/png(?:;|$)/.test(image.contentType ?? '') && image.body instanceof Uint8Array && image.body.length <= 512 * 1024
            && Buffer.from(image.body).subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])), 'QR_VIEWER_IMAGE_INVALID');
          state.qr = 'data:image/png;base64,' + Buffer.from(image.body).toString('base64');
        }
      }
    }
    return state;
  } };
}
