// Standalone BuilderBot socket transport. Never imports the legacy gateway/.env.
import { createServer } from "node:http";
import { mkdirSync, writeFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { DirectQrSession, bindDirectPairing } from "./direct-qr-session.js";
import { createNativeProfileBootstrap, loadInstalledQrBindings } from "./native-profile-bootstrap.js";
import { qrPreviewPage } from "./qr-preview-page.js";
import { loadNativeRuntimeProfile } from "./native-runtime-profile.js";
import { loadDirectQrDependencies, resolveQrListenHost } from "./qr-runtime-dependencies.js";
import { qrRuntimeState } from "./qr-runtime-state.js";
import { qrImageReply } from "./qr-image-reply.js";

if (process.argv[2] === "--check-runtime") {
  if (process.argv.length !== 3) throw new Error("QR_RUNTIME_CHECK_ARGUMENTS");
  await Promise.all([loadDirectQrDependencies(), loadInstalledQrBindings()]);
  console.log(JSON.stringify({ status: "QR_RUNTIME_IMPORTS_OK", providerInstances: 0, businessEnabled: false }));
} else {
const args = process.argv.slice(2);
const activationRequested = args.at(-1) === "--activate-native";
if (activationRequested) args.pop();
if (args.length < 1 || args.length > 2 || args.some(arg => arg.startsWith("--"))) throw new Error("QR_RUNTIME_ARGUMENTS");
const directory = args[0];
if (!directory || !isAbsolute(directory)) throw new Error("An absolute private session directory is required");
const listenHost = resolveQrListenHost(process.env.IMPULSA_QR_LISTEN_HOST);
mkdirSync(directory, { recursive: true });
process.chdir(directory);
const stagedProfile = loadNativeRuntimeProfile(args[1], directory, activationRequested);
const { BaileysProvider, qrImage } = await loadDirectQrDependencies();
const session = new DirectQrSession(raw => qrImage.imageSync(raw, { type: "png", size: 8, margin: 4, ec_level: "M" }));
// The QR page is read-only. An enabled private profile and explicit CLI flag
// supply the existing account/scope/manifest-fenced native integration.
let provider: any;
const nativeTransport = await createNativeProfileBootstrap({
  profile: stagedProfile?.profile ?? null,
  sessionDirectory: directory,
  getProvider: () => provider,
  loadInstalled: loadInstalledQrBindings,
});
const startedUtc = new Date().toISOString();
let closing = false;
const observe = () => writeFileSync(resolve(directory, "provider-status.json"), JSON.stringify({
  pid: process.pid, startedUtc, updatedUtc: new Date().toISOString(), ...session.snapshot(),
}), { mode: 0o600 });

const server = createServer((request, response) => {
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("X-Content-Type-Options", "nosniff");
  if (!["localhost:3008", "127.0.0.1:3008"].includes(request.headers.host || "") || request.method !== "GET") {
    response.writeHead(403); response.end("Local read-only QR page"); return;
  }
  const requestUrl = new URL(request.url || "/", "http://127.0.0.1:3008");
  const path = requestUrl.pathname;
  if (path === "/api/bots/impulsa/qr-image") {
    const snapshot = session.snapshot(true);
    const result = qrImageReply(snapshot, requestUrl.searchParams.get("generation"));
    response.statusCode = result.status;
    response.setHeader("X-Impulsa-QR-Generation", String(snapshot.qrGeneration));
    response.setHeader("Content-Type", result.bytes ? "image/png" : "text/plain; charset=utf-8");
    response.end(result.bytes ?? "QR unavailable"); return;
  }
  if (path === "/api/health" || path === "/api/bots/impulsa/qr-state") {
    response.setHeader("Content-Type", "application/json");
    const transport = nativeTransport.inspectMetadata();
    const pairing = session.snapshot(path.endsWith("qr-state"));
    response.end(JSON.stringify({ revision: 5, connectorVersion: "1.4.4", pid: process.pid, startedUtc,
      browserProcess: false, posRelay: false, tenantId: "impulsa",
      ...qrRuntimeState(pairing.status, transport),
      nativeTransport: transport.status,
      nativeScope: stagedProfile?.profile.native.scope ?? null,
      nativeProfile: stagedProfile ? { status: stagedProfile.status, fingerprint: stagedProfile.fingerprint } : null,
      ...pairing })); return;
  }
  if (path !== "/" && path !== "/api/bots/impulsa/qr") {
    response.writeHead(404); response.end("Not found"); return;
  }
  response.setHeader("Content-Type", "text/html; charset=utf-8");
  response.setHeader("Content-Security-Policy", "default-src 'none'; img-src data:; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'");
  response.end(qrPreviewPage);
});
const close = async () => {
  if (closing) return;
  closing = true;
  session.stopped(); observe();
  server.close();
  await nativeTransport.close();
  await provider?.destroy();
  process.exit(0);
};
process.on("SIGINT", () => { void close(); });
process.on("SIGTERM", () => { void close(); });
server.once("error", () => { console.error("Local QR port is unavailable"); void close(); });
server.listen(3008, listenHost, () => {
  provider = new BaileysProvider({
    name: "impulsa-direct", port: 0, usePairingCode: false,
    browser: ["Impulsa", "Desktop", "1.0.0"],
    baileysLogLevel: "silent", captureProcessSignals: false, clearAuthOnLogout: false,
    lidCache: "memory", useBaileysStore: false,
    syncFullHistory: false, shouldSyncHistoryMessage: () => false,
    markOnlineOnConnect: false, fireInitQueries: false,
    shouldIgnoreJid: () => !nativeTransport.inspectMetadata().enabled,
    enableAutoSessionRecreation: false, enableRecentMessageCache: false,
    maxMsgRetryCount: 0,
  });
  bindDirectPairing(provider, session, nativeTransport);
  provider.on("require_action", observe);
  provider.on("ready", observe);
  provider.on("auth_failure", observe);
  observe();
  void provider.initVendor().catch(() => { session.failed(); observe(); });
  console.log("Impulsa direct QR: http://127.0.0.1:3008/api/bots/impulsa/qr?format=view · " + (activationRequested ? "native profile enabled; waiting for verified connection" : "responses paused"));
});
}
