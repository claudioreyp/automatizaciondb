// Composition only. Default OFF; no provider, legacy gateway or secret imports.
// The owner supplies verified private callbacks and the existing Baileys socket.
import { isAbsolute } from "node:path";

export interface DirectNativeSocket {
  user?: { id?: unknown };
  ev: { on(event: string, listener: (...args: any[]) => void): unknown; off(event: string, listener: (...args: any[]) => void): unknown };
  sendMessage?: (...args: any[]) => unknown;
}

export interface DirectNativeConfig {
  https_origin: string;
  tenant: string;
  scope: { business_id: number; branch_id: number };
  gateway_id: string;
  account_jid: string;
  main_workflow_id: string;
  main_manifest: string;
  mailbox_workflow_id: string;
  mailbox_manifest: string;
  mailbox_table_id: string;
}

type PrivateCallback = (input: any) => unknown | Promise<unknown>;
export interface DirectNativeInboundCallbacks {
  enqueue: PrivateCallback;
  reserveMedia: PrivateCallback;
  downloadOriginal: PrivateCallback;
  validateReservation: PrivateCallback;
  completeMedia: PrivateCallback;
  recordUnsupported: PrivateCallback;
}

export interface DirectNativeInboundHost {
  attachSocket(socket: DirectNativeSocket): unknown;
  detachSocket(): void;
  close(): Promise<unknown>;
  getStatus(): { open?: boolean };
}

export interface DirectNativeMailboxClient {
  bindSocket(socket: DirectNativeSocket): unknown;
  retryPendingAcks(): Promise<{ status: string }>;
  runOnce(): Promise<{ status: string }>;
  // Existing C448 close invalidates closed/epoch synchronously before awaiting.
  close(): Promise<unknown>;
}

export interface DirectNativePrivateBindings {
  config: DirectNativeConfig;
  directory: string;
  inboundCallbacks: DirectNativeInboundCallbacks;
  readCredential(): { header_name: string; header_value: string } | Promise<{ header_name: string; header_value: string }>;
  createMessageId(): string;
  createInboundHost(options: { enabled: true; tenantId: string; expectedAccountJid: string; callbacks: DirectNativeInboundCallbacks }): DirectNativeInboundHost;
  createMailboxClient(options: { enabled: true; config: DirectNativeConfig; directory: string; readCredential: DirectNativePrivateBindings["readCredential"];
    createMessageId: DirectNativePrivateBindings["createMessageId"]; requestHTTPS?: PrivateCallback }): DirectNativeMailboxClient;
  requestHTTPS?: PrivateCallback;
}

export interface DirectNativeTransport {
  observeSocket(socket: DirectNativeSocket): void;
  observeConnection(socket: DirectNativeSocket, update: unknown): void;
  close(): Promise<void>;
  inspectMetadata(): { enabled: boolean; status: string; generation: number; socketObserved: boolean; clientBound: boolean; operationPending: boolean; timerPending: boolean };
}

export type DirectNativeTransportOptions = { enabled?: false } | {
  enabled: true;
  loadPrivateBindings(): DirectNativePrivateBindings;
  isCurrentSocket(socket: DirectNativeSocket): boolean;
  pollIntervalMs?: number;
};

function need(ok: unknown, code: string): asserts ok { if (!ok) throw new Error(code); }
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const ownAccount = (value: unknown) => {
  if (typeof value !== "string") return null;
  const found = /^(\d{8,15})(?::\d{1,5})?@s\.whatsapp\.net$/.exec(value);
  return found ? found[1] + "@s.whatsapp.net" : null;
};

export function createDirectNativeTransport(options: DirectNativeTransportOptions = {}): DirectNativeTransport {
  // Do not access private bindings, factories, callbacks, sockets or timers OFF.
  if (options.enabled !== true) return {
    observeSocket() {}, observeConnection() {}, async close() {},
    inspectMetadata: () => ({ enabled: false, status: "OFF", generation: 0, socketObserved: false, clientBound: false, operationPending: false, timerPending: false }),
  };
  const interval = options.pollIntervalMs ?? 1000;
  need(typeof options.loadPrivateBindings === "function" && typeof options.isCurrentSocket === "function"
    && Number.isSafeInteger(interval) && interval >= 10 && interval <= 60_000, "DIRECT_NATIVE_PRIVATE_OPTIONS");
  const bindings = options.loadPrivateBindings();
  need(bindings && typeof bindings.createInboundHost === "function" && typeof bindings.createMailboxClient === "function"
    && typeof bindings.readCredential === "function" && typeof bindings.createMessageId === "function"
    && typeof bindings.directory === "string" && isAbsolute(bindings.directory)
    && (bindings.requestHTTPS === undefined || typeof bindings.requestHTTPS === "function"), "DIRECT_NATIVE_PRIVATE_BINDINGS");
  const cfg = bindings.config;
  const keys = ["https_origin", "tenant", "scope", "gateway_id", "account_jid", "main_workflow_id", "main_manifest", "mailbox_workflow_id", "mailbox_manifest", "mailbox_table_id"];
  need(record(cfg) && Object.keys(cfg).sort().join("|") === keys.sort().join("|") && ownAccount(cfg.account_jid) === cfg.account_jid
    && /^[A-Za-z0-9_-]{1,80}$/.test(cfg.tenant) && !cfg.tenant.includes("__UNBOUND") && record(cfg.scope)
    && Object.keys(cfg.scope).sort().join("|") === "branch_id|business_id"
    && [cfg.scope.business_id, cfg.scope.branch_id].every(x => Number.isSafeInteger(x) && x > 0), "DIRECT_NATIVE_FIXED_CONFIG");
  const origin = new URL(cfg.https_origin);
  need(origin.protocol === "https:" && origin.origin === cfg.https_origin && !origin.username && !origin.password && !origin.search && !origin.hash
    && [cfg.gateway_id, cfg.main_workflow_id, cfg.mailbox_workflow_id, cfg.mailbox_table_id].every(value => typeof value === "string"
      && /^[A-Za-z0-9_.:-]{1,200}$/.test(value) && !value.includes("__UNBOUND"))
    && [cfg.main_manifest, cfg.mailbox_manifest].every(value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value)), "DIRECT_NATIVE_PRIVATE_CONFIG_UNBOUND");
  const callbacks = bindings.inboundCallbacks;
  const callbackNames = ["enqueue", "reserveMedia", "downloadOriginal", "validateReservation", "completeMedia", "recordUnsupported"] as const;
  need(callbacks && callbackNames.every(name => typeof callbacks[name] === "function"), "DIRECT_NATIVE_PRIVATE_INGRESS_REQUIRED");
  // Fixed copies; no consumer payload can select a factory, callback or account.
  const config: DirectNativeConfig = { ...cfg, scope: { ...cfg.scope } };
  const fixedCallbacks = Object.fromEntries(callbackNames.map(name => [name, callbacks[name]])) as unknown as DirectNativeInboundCallbacks;
  const host = bindings.createInboundHost({ enabled: true, tenantId: config.tenant, expectedAccountJid: config.account_jid, callbacks: fixedCallbacks });
  need(host && typeof host.attachSocket === "function" && typeof host.detachSocket === "function" && typeof host.close === "function" && typeof host.getStatus === "function", "DIRECT_NATIVE_HOST_INTERFACE");
  const createClient = bindings.createMailboxClient;
  const clientOptions = { enabled: true as const, config, directory: bindings.directory, readCredential: bindings.readCredential,
    createMessageId: bindings.createMessageId, ...(bindings.requestHTTPS ? { requestHTTPS: bindings.requestHTTPS } : {}) };
  let socket: DirectNativeSocket | null = null, client: DirectNativeMailboxClient | null = null;
  let generation = 0, opened = false, closed = false, review = false, closeUnconfirmed = false;
  let hostClosing: Promise<unknown> | null = null, finalClosing: Promise<void> | null = null;
  let status = "WAITING_SOCKET", timer: ReturnType<typeof setTimeout> | undefined;
  let operation: Promise<void> | null = null, draining: Promise<void> = Promise.resolve(), lifecycle: Promise<void> = Promise.resolve();

  const cancelTimer = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined; };
  const closeHostNow = () => {
    if (hostClosing) return hostClosing;
    let resolve!: (value: unknown) => void, reject!: (reason: unknown) => void;
    // Publish before close can synchronously reenter the connection owner.
    hostClosing = new Promise((done, fail) => { resolve = done; reject = fail; });
    void hostClosing.catch(() => { closeUnconfirmed = true; });
    try { Promise.resolve(host.close()).then(resolve, reject); } catch (error) { reject(error); }
    return hostClosing;
  };
  const closeClientNow = () => {
    const previous = client, pending = operation;
    client = null;
    let closing: Promise<unknown> = Promise.resolve();
    // Invoke now: C448 invalidates epochs synchronously, before any await here.
    if (previous) {
      try { closing = Promise.resolve(previous.close()); }
      catch { closeUnconfirmed = true; review = true; status = "REVIEW_REQUIRED"; }
    }
    draining = Promise.allSettled([draining, closing, pending ?? Promise.resolve()]).then(results => {
      if (results.some(result => result.status === "rejected")) { closeUnconfirmed = true; review = true; status = "REVIEW_REQUIRED"; }
    });
  };
  const failClosed = () => {
    review = true; opened = false; generation++; status = "REVIEW_REQUIRED"; cancelTimer();
    // A host failure must not prevent immediate outbound epoch invalidation.
    closeClientNow();
    try { host.detachSocket(); } catch { closeUnconfirmed = true; }
    void closeHostNow().catch(() => {});
  };
  const current = (expected: DirectNativeSocket, epoch: number) => !closed && !review && opened && socket === expected && generation === epoch
    && options.isCurrentSocket(expected) && ownAccount(expected.user?.id) === config.account_jid && host.getStatus().open === true;

  const tick = (expected: DirectNativeSocket, epoch: number, active: DirectNativeMailboxClient) => {
    if (operation) { failClosed(); return; }
    const work = Promise.resolve().then(async () => {
      if (!current(expected, epoch) || client !== active) return;
      const recovered = await active.retryPendingAcks();
      if (!current(expected, epoch) || client !== active) return;
      if (recovered.status !== "ACK_ONLY_DURABLE") { failClosed(); return; }
      const result = await active.runOnce();
      if (!current(expected, epoch) || client !== active) return;
      if (!["idle", "mailbox_ack_durable"].includes(result.status)) { failClosed(); return; }
      status = result.status === "idle" ? "IDLE" : "ACK_DURABLE";
    }).catch(() => { failClosed(); });
    operation = work;
    void work.then(() => {
      if (operation === work) operation = null;
      try {
        if (!current(expected, epoch) || client !== active) return;
        timer = setTimeout(() => { timer = undefined; tick(expected, epoch, active); }, interval);
        timer.unref?.();
      } catch { failClosed(); }
    });
  };
  const beginOpen = (expected: DirectNativeSocket, epoch: number) => {
    lifecycle = lifecycle.then(async () => {
      await draining;
      if (!current(expected, epoch)) return;
      const created = createClient(clientOptions);
      // Retain before validating/binding, so an interface/bind failure closes it.
      client = created;
      need(created && ["bindSocket", "retryPendingAcks", "runOnce", "close"].every(name => typeof created[name as keyof DirectNativeMailboxClient] === "function"), "DIRECT_NATIVE_CLIENT_INTERFACE");
      if (!current(expected, epoch)) { closeClientNow(); return; }
      created.bindSocket(expected);
      if (!current(expected, epoch)) { closeClientNow(); return; }
      status = "BOUND"; tick(expected, epoch, created);
    }).catch(() => { failClosed(); });
  };

  return {
    observeSocket(next) {
      try {
        if (closed || review) return;
        cancelTimer(); opened = false; generation++; closeClientNow(); host.detachSocket();
        need(next && typeof next.ev?.on === "function" && typeof next.ev?.off === "function" && options.isCurrentSocket(next), "DIRECT_NATIVE_SOCKET_OWNER");
        socket = next;
        // Owner must call this before registering its connection observer.
        host.attachSocket(next); status = "WAITING_OPEN";
      } catch { failClosed(); }
    },
    observeConnection(observed, update) {
      try {
        if (closed || review || observed !== socket) return;
        if (!record(update)) { failClosed(); return; }
        if (update.connection === "connecting" || update.connection === "close") {
          opened = false; generation++; status = "DISCONNECTED"; cancelTimer(); closeClientNow(); return;
        }
        if (update.connection !== "open" || opened) return;
        need(options.isCurrentSocket(observed) && ownAccount(observed.user?.id) === config.account_jid && host.getStatus().open === true,
          "DIRECT_NATIVE_OPEN_ACCOUNT_OR_HOST_FENCE");
        opened = true; generation++; beginOpen(observed, generation);
      } catch { failClosed(); }
    },
    close() {
      if (finalClosing) return finalClosing;
      let resolve!: () => void, reject!: (reason: unknown) => void;
      finalClosing = new Promise<void>((done, fail) => { resolve = done; reject = fail; });
      void finalClosing.catch(() => {});
      closed = true; opened = false; generation++; status = "CLOSING"; cancelTimer(); closeClientNow();
      try { host.detachSocket(); } catch { closeUnconfirmed = true; }
      socket = null;
      const hostWork = closeHostNow();
      void Promise.allSettled([draining, lifecycle, hostWork]).then(results => {
        if (closeUnconfirmed || results.some(result => result.status === "rejected")) {
          status = "CLOSE_UNCONFIRMED"; reject(new Error("DIRECT_NATIVE_CLOSE_UNCONFIRMED"));
        } else { status = "CLOSED"; resolve(); }
      });
      return finalClosing;
    },
    inspectMetadata: () => ({ enabled: true, status, generation, socketObserved: socket !== null, clientBound: client !== null,
      operationPending: operation !== null, timerPending: timer !== undefined }),
  };
}
