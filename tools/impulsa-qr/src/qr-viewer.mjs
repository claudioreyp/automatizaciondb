// Local display proxy only. No Baileys/BuilderBot imports, sessions or provider.
import { createServer } from 'node:http';
import { dirname } from 'node:path';
import { realpathSync } from 'node:fs';
import { qrPreviewPage } from './qr-preview-page.js';
import { loadQrViewerProfile, readQrViewerCredential, createQrViewerClient } from './qr-viewer-client.mjs';
const file = process.argv[2];
if (process.argv.length !== 3) throw Error('QR_VIEWER_PROFILE_ARGUMENT_REQUIRED');
const profile = loadQrViewerProfile(file), privateDirectory = realpathSync(dirname(file));
const client = createQrViewerClient({ profile, readCredential: () => readQrViewerCredential(profile, privateDirectory) });
let active = 0;
const server = createServer(async (request, response) => {
  response.setHeader('Cache-Control', 'no-store'); response.setHeader('X-Content-Type-Options', 'nosniff');
  if (request.method !== 'GET' || !['localhost:3008','127.0.0.1:3008'].includes(request.headers.host ?? '')) { response.writeHead(403); response.end('Local read-only QR viewer'); return; }
  const path = new URL(request.url ?? '/', 'http://127.0.0.1:3008').pathname;
  if (path === '/' || path === '/api/bots/impulsa/qr') {
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    response.setHeader('Content-Security-Policy', "default-src 'none'; img-src data:; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'");
    response.end(qrPreviewPage); return;
  }
  if (!['/api/health','/api/bots/impulsa/qr-state'].includes(path)) { response.writeHead(404); response.end('Not found'); return; }
  if (active >= 4) { response.writeHead(503); response.end('QR viewer busy'); return; }
  active++;
  try { const state = await client.readState(path.endsWith('qr-state')); response.setHeader('Content-Type','application/json'); response.end(JSON.stringify(state)); }
  catch { response.writeHead(503); response.end('QR service unavailable'); }
  finally { active--; }
});
server.listen(3008, '127.0.0.1', () => console.log('Impulsa QR viewer: http://127.0.0.1:3008/api/bots/impulsa/qr?format=view'));
server.on('error', () => { console.error('Local QR viewer port unavailable'); process.exitCode = 1; });
process.on('SIGINT', () => server.close()); process.on('SIGTERM', () => server.close());
