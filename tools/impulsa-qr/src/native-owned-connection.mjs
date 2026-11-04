// APP POS/entregas/agente-20261028/native-n8n/native-control-c464/preparation/native-owned-connection-r01.mjs
import { isAbsolute as isAbsolute4 } from "node:path";

// APP POS/entregas/agente-20261028/native-n8n/native-control-c464/preparation/direct-native-transport-r02.mjs
import { isAbsolute } from "node:path";
function need(ok, code) {
  if (!ok)
    throw new Error(code);
}
var record = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
var ownAccount = (value) => {
  if (typeof value !== "string")
    return null;
  const found = /^(\d{8,15})(?::\d{1,5})?@s\.whatsapp\.net$/.exec(value);
  return found ? found[1] + "@s.whatsapp.net" : null;
};
function createDirectNativeTransport(options = {}) {
  if (options.enabled !== true)
    return {
      observeSocket() {
      },
      observeConnection() {
      },
      async close() {
      },
      inspectMetadata: () => ({ enabled: false, status: "OFF", generation: 0, socketObserved: false, clientBound: false, operationPending: false, timerPending: false })
    };
  const interval = options.pollIntervalMs ?? 1e3;
  need(typeof options.loadPrivateBindings === "function" && typeof options.isCurrentSocket === "function" && Number.isSafeInteger(interval) && interval >= 10 && interval <= 6e4, "DIRECT_NATIVE_PRIVATE_OPTIONS");
  const bindings = options.loadPrivateBindings();
  need(bindings && typeof bindings.createInboundHost === "function" && typeof bindings.createMailboxClient === "function" && typeof bindings.readCredential === "function" && typeof bindings.createMessageId === "function" && typeof bindings.directory === "string" && isAbsolute(bindings.directory) && (bindings.requestHTTPS === void 0 || typeof bindings.requestHTTPS === "function"), "DIRECT_NATIVE_PRIVATE_BINDINGS");
  const cfg = bindings.config;
  const keys = ["https_origin", "tenant", "scope", "gateway_id", "account_jid", "main_workflow_id", "main_manifest", "mailbox_workflow_id", "mailbox_manifest", "mailbox_table_id"];
  need(record(cfg) && Object.keys(cfg).sort().join("|") === keys.sort().join("|") && ownAccount(cfg.account_jid) === cfg.account_jid && /^[A-Za-z0-9_-]{1,80}$/.test(cfg.tenant) && !cfg.tenant.includes("__UNBOUND") && record(cfg.scope) && Object.keys(cfg.scope).sort().join("|") === "branch_id|business_id" && [cfg.scope.business_id, cfg.scope.branch_id].every((x) => Number.isSafeInteger(x) && x > 0), "DIRECT_NATIVE_FIXED_CONFIG");
  const origin = new URL(cfg.https_origin);
  need(origin.protocol === "https:" && origin.origin === cfg.https_origin && !origin.username && !origin.password && !origin.search && !origin.hash && [cfg.gateway_id, cfg.main_workflow_id, cfg.mailbox_workflow_id, cfg.mailbox_table_id].every((value) => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,200}$/.test(value) && !value.includes("__UNBOUND")) && [cfg.main_manifest, cfg.mailbox_manifest].every((value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value)), "DIRECT_NATIVE_PRIVATE_CONFIG_UNBOUND");
  const callbacks = bindings.inboundCallbacks;
  const callbackNames = ["enqueue", "reserveMedia", "downloadOriginal", "validateReservation", "completeMedia", "recordUnsupported"];
  need(callbacks && callbackNames.every((name) => typeof callbacks[name] === "function"), "DIRECT_NATIVE_PRIVATE_INGRESS_REQUIRED");
  const config = { ...cfg, scope: { ...cfg.scope } };
  const fixedCallbacks = Object.fromEntries(callbackNames.map((name) => [name, callbacks[name]]));
  const host = bindings.createInboundHost({ enabled: true, tenantId: config.tenant, expectedAccountJid: config.account_jid, callbacks: fixedCallbacks });
  need(host && typeof host.attachSocket === "function" && typeof host.detachSocket === "function" && typeof host.close === "function" && typeof host.getStatus === "function", "DIRECT_NATIVE_HOST_INTERFACE");
  const createClient = bindings.createMailboxClient;
  const clientOptions = {
    enabled: true,
    config,
    directory: bindings.directory,
    readCredential: bindings.readCredential,
    createMessageId: bindings.createMessageId,
    ...bindings.requestHTTPS ? { requestHTTPS: bindings.requestHTTPS } : {}
  };
  let socket = null, client = null;
  let generation = 0, opened = false, closed = false, review = false, closeUnconfirmed = false;
  let hostClosing = null, finalClosing = null;
  let status = "WAITING_SOCKET", timer;
  let operation = null, draining = Promise.resolve(), lifecycle = Promise.resolve();
  const cancelTimer = () => {
    if (timer !== void 0)
      clearTimeout(timer);
    timer = void 0;
  };
  const closeHostNow = () => {
    if (hostClosing)
      return hostClosing;
    let resolve, reject;
    hostClosing = new Promise((done, fail) => {
      resolve = done;
      reject = fail;
    });
    void hostClosing.catch(() => {
      closeUnconfirmed = true;
    });
    try {
      Promise.resolve(host.close()).then(resolve, reject);
    } catch (error2) {
      reject(error2);
    }
    return hostClosing;
  };
  const closeClientNow = () => {
    const previous = client, pending = operation;
    client = null;
    let closing = Promise.resolve();
    if (previous) {
      try {
        closing = Promise.resolve(previous.close());
      } catch {
        closeUnconfirmed = true;
        review = true;
        status = "REVIEW_REQUIRED";
      }
    }
    draining = Promise.allSettled([draining, closing, pending ?? Promise.resolve()]).then((results) => {
      if (results.some((result) => result.status === "rejected")) {
        closeUnconfirmed = true;
        review = true;
        status = "REVIEW_REQUIRED";
      }
    });
  };
  const failClosed = () => {
    review = true;
    opened = false;
    generation++;
    status = "REVIEW_REQUIRED";
    cancelTimer();
    closeClientNow();
    try {
      host.detachSocket();
    } catch {
      closeUnconfirmed = true;
    }
    void closeHostNow().catch(() => {
    });
  };
  const current = (expected, epoch) => !closed && !review && opened && socket === expected && generation === epoch && options.isCurrentSocket(expected) && ownAccount(expected.user?.id) === config.account_jid && host.getStatus().open === true;
  const tick = (expected, epoch, active) => {
    if (operation) {
      failClosed();
      return;
    }
    const work = Promise.resolve().then(async () => {
      if (!current(expected, epoch) || client !== active)
        return;
      const recovered = await active.retryPendingAcks();
      if (!current(expected, epoch) || client !== active)
        return;
      if (recovered.status !== "ACK_ONLY_DURABLE") {
        failClosed();
        return;
      }
      const result = await active.runOnce();
      if (!current(expected, epoch) || client !== active)
        return;
      if (!["idle", "mailbox_ack_durable"].includes(result.status)) {
        failClosed();
        return;
      }
      status = result.status === "idle" ? "IDLE" : "ACK_DURABLE";
    }).catch(() => {
      failClosed();
    });
    operation = work;
    void work.then(() => {
      if (operation === work)
        operation = null;
      try {
        if (!current(expected, epoch) || client !== active)
          return;
        timer = setTimeout(() => {
          timer = void 0;
          tick(expected, epoch, active);
        }, interval);
        timer.unref?.();
      } catch {
        failClosed();
      }
    });
  };
  const beginOpen = (expected, epoch) => {
    lifecycle = lifecycle.then(async () => {
      await draining;
      if (!current(expected, epoch))
        return;
      const created = createClient(clientOptions);
      client = created;
      need(created && ["bindSocket", "retryPendingAcks", "runOnce", "close"].every((name) => typeof created[name] === "function"), "DIRECT_NATIVE_CLIENT_INTERFACE");
      if (!current(expected, epoch)) {
        closeClientNow();
        return;
      }
      created.bindSocket(expected);
      if (!current(expected, epoch)) {
        closeClientNow();
        return;
      }
      status = "BOUND";
      tick(expected, epoch, created);
    }).catch(() => {
      failClosed();
    });
  };
  return {
    observeSocket(next) {
      try {
        if (closed || review)
          return;
        cancelTimer();
        opened = false;
        generation++;
        closeClientNow();
        host.detachSocket();
        need(next && typeof next.ev?.on === "function" && typeof next.ev?.off === "function" && options.isCurrentSocket(next), "DIRECT_NATIVE_SOCKET_OWNER");
        socket = next;
        host.attachSocket(next);
        status = "WAITING_OPEN";
      } catch {
        failClosed();
      }
    },
    observeConnection(observed, update) {
      try {
        if (closed || review || observed !== socket)
          return;
        if (!record(update)) {
          failClosed();
          return;
        }
        if (update.connection === "connecting" || update.connection === "close") {
          opened = false;
          generation++;
          status = "DISCONNECTED";
          cancelTimer();
          closeClientNow();
          return;
        }
        if (update.connection !== "open" || opened)
          return;
        need(options.isCurrentSocket(observed) && ownAccount(observed.user?.id) === config.account_jid && host.getStatus().open === true, "DIRECT_NATIVE_OPEN_ACCOUNT_OR_HOST_FENCE");
        opened = true;
        generation++;
        beginOpen(observed, generation);
      } catch {
        failClosed();
      }
    },
    close() {
      if (finalClosing)
        return finalClosing;
      let resolve, reject;
      finalClosing = new Promise((done, fail) => {
        resolve = done;
        reject = fail;
      });
      void finalClosing.catch(() => {
      });
      closed = true;
      opened = false;
      generation++;
      status = "CLOSING";
      cancelTimer();
      closeClientNow();
      try {
        host.detachSocket();
      } catch {
        closeUnconfirmed = true;
      }
      socket = null;
      const hostWork = closeHostNow();
      void Promise.allSettled([draining, lifecycle, hostWork]).then((results) => {
        if (closeUnconfirmed || results.some((result) => result.status === "rejected")) {
          status = "CLOSE_UNCONFIRMED";
          reject(new Error("DIRECT_NATIVE_CLOSE_UNCONFIRMED"));
        } else {
          status = "CLOSED";
          resolve();
        }
      });
      return finalClosing;
    },
    inspectMetadata: () => ({
      enabled: true,
      status,
      generation,
      socketObserved: socket !== null,
      clientBound: client !== null,
      operationPending: operation !== null,
      timerPending: timer !== void 0
    })
  };
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c462/preparation/native-media-host-owned-downloads-r01.mjs
import * as nodeCrypto from "node:crypto";

// APP POS/entregas/agente-20261028/native-n8n/native-control-c454/original-lease-preparation/private-original-media-lease-r01.mjs
var object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
var need2 = (condition, code) => {
  if (!condition) throw new Error(code);
};
var account = (value) => {
  if (typeof value !== "string") return null;
  const found = /^([0-9]{6,20})(?::[0-9]{1,5})?@(s\.whatsapp\.net|lid)$/.exec(value);
  return found && (found[2] !== "s.whatsapp.net" || /^[0-9]{8,15}$/.test(found[1])) ? found[1] + "@" + found[2] : null;
};
function createPrivateOriginalMediaLeaseRegistry(options = {}) {
  const enabled = options?.enabled;
  if (enabled !== true) return Object.freeze({
    issuer: Object.freeze({
      registerOriginal() {
        throw new Error("C454_ORIGINAL_LEASE_OFF");
      },
      invalidateAll() {
      }
    }),
    acquireOriginalLease() {
      throw new Error("C454_ORIGINAL_LEASE_OFF");
    },
    inspectMetadata: () => ({
      enabled: false,
      active_leases: 0,
      blocked: false,
      original_custody_accredited: false
    })
  });
  const { tenantId, expectedAccountJid, maxActiveLeases = 20 } = options;
  const expected = account(expectedAccountJid);
  need2(
    typeof tenantId === "string" && /^[A-Za-z0-9_-]{1,80}$/.test(tenantId) && !tenantId.includes("__UNBOUND") && expected && expected === expectedAccountJid,
    "C454_ORIGINAL_LEASE_IDENTITY"
  );
  need2(
    Number.isSafeInteger(maxActiveLeases) && maxActiveLeases >= 1 && maxActiveLeases <= 20,
    "C454_ORIGINAL_LEASE_CAP"
  );
  const lookup = /* @__PURE__ */ new WeakMap(), active = /* @__PURE__ */ new Set();
  let blocked = false;
  function markRevoked(record2) {
    if (!record2.valid) return false;
    record2.valid = false;
    if (lookup.get(record2.source) === record2) lookup.delete(record2.source);
    active.delete(record2);
    return true;
  }
  function notifyRevoked(record2) {
    const listeners = [...record2.listeners];
    record2.listeners.clear();
    for (const subscription of listeners) {
      if (!subscription.active) continue;
      subscription.active = false;
      try {
        subscription.callback();
      } catch {
        notificationFailed();
      }
    }
  }
  function revoke(record2) {
    if (markRevoked(record2)) notifyRevoked(record2);
  }
  function invalidateAll() {
    const records = [...active];
    for (const record2 of records) markRevoked(record2);
    for (const record2 of records) notifyRevoked(record2);
  }
  function notificationFailed() {
    blocked = true;
    invalidateAll();
  }
  function recordCurrent(record2) {
    if (blocked || !record2.valid || lookup.get(record2.source) !== record2 || !active.has(record2)) return false;
    try {
      const valid = record2.privateCurrent() === true;
      return valid && !blocked && record2.valid && lookup.get(record2.source) === record2 && active.has(record2) && account(record2.socket.user?.id) === expected && record2.source.tenantId === tenantId && record2.source.account_jid === expected && record2.originalMessage.key === record2.source.key && record2.source.message_id === record2.originalMessage.key.id;
    } catch {
      return false;
    }
  }
  function isCurrent(record2) {
    const valid = recordCurrent(record2);
    if (!valid) revoke(record2);
    return valid;
  }
  function onInvalidate(record2, callback) {
    need2(typeof callback === "function", "C454_ORIGINAL_LEASE_SUBSCRIBER");
    if (!isCurrent(record2)) {
      try {
        callback();
      } catch {
        notificationFailed();
      }
      return () => {
      };
    }
    need2(record2.listeners.size === 0, "C454_ORIGINAL_LEASE_SUBSCRIBER_CAP");
    const subscription = { callback, active: true };
    record2.listeners.add(subscription);
    return () => {
      subscription.active = false;
      record2.listeners.delete(subscription);
    };
  }
  function registerOriginal(descriptor = {}) {
    need2(!blocked, "C454_ORIGINAL_LEASE_BLOCKED");
    const { originalMessage, source, reservation, socket, socketEpoch, isCurrent: privateCurrent } = descriptor;
    need2(
      object(originalMessage) && object(originalMessage.key) && object(source) && originalMessage.key === source.key && object(reservation) && object(socket) && source.tenantId === tenantId && source.account_jid === expected && typeof source.message_id === "string" && source.message_id.length > 0 && source.message_id === originalMessage.key.id && account(socket.user?.id) === expected && Number.isSafeInteger(socketEpoch) && socketEpoch > 0 && typeof privateCurrent === "function",
      "C454_ORIGINAL_LEASE_BINDING"
    );
    need2(
      !blocked && !lookup.has(source) && active.size < maxActiveLeases,
      "C454_ORIGINAL_LEASE_UNAVAILABLE"
    );
    const record2 = {
      originalMessage,
      source,
      reservation,
      socket,
      socketEpoch,
      privateCurrent,
      valid: true,
      listeners: /* @__PURE__ */ new Set(),
      lease: null
    };
    record2.lease = Object.freeze({
      originalMessage,
      source,
      reservation,
      socket,
      account_jid: expected,
      socketEpoch,
      isCurrent: () => isCurrent(record2),
      onInvalidate: (callback) => onInvalidate(record2, callback)
    });
    lookup.set(source, record2);
    active.add(record2);
    if (!isCurrent(record2)) throw new Error("C454_ORIGINAL_LEASE_NOT_CURRENT");
    return () => revoke(record2);
  }
  function acquireOriginalLease(descriptor = {}) {
    const { originalMessage, source, reservation } = descriptor;
    need2(object(source), "C454_ORIGINAL_LEASE_NOT_REGISTERED");
    const record2 = lookup.get(source);
    need2(record2 && record2.originalMessage === originalMessage && record2.source === source && record2.reservation === reservation, "C454_ORIGINAL_LEASE_NOT_REGISTERED");
    need2(isCurrent(record2), "C454_ORIGINAL_LEASE_NOT_CURRENT");
    return record2.lease;
  }
  return Object.freeze({
    issuer: Object.freeze({ registerOriginal, invalidateAll }),
    acquireOriginalLease,
    inspectMetadata: () => ({
      enabled: true,
      active_leases: active.size,
      blocked,
      max_active_leases: maxActiveLeases,
      original_custody_accredited: false
    })
  });
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c454/bounded-web-media-preparation/baileys-original-image-web-download-r01.mjs
var MAX_BYTES = 8 * 1024 * 1024;
var MAX_WIRE_BYTES = MAX_BYTES + 16 + 10;
var ORIGIN = "https://mmg.whatsapp.net";
var WRAPPERS = [
  "ephemeralMessage",
  "viewOnceMessage",
  "documentWithCaptionMessage",
  "viewOnceMessageV2",
  "viewOnceMessageV2Extension",
  "associatedChildMessage"
];
var object2 = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
var error = (code) => new Error(code);
var need3 = (condition, code) => {
  if (!condition) throw error(code);
};
var account2 = (value) => {
  if (typeof value !== "string") return null;
  const found = /^([0-9]{6,20})(?::[0-9]{1,5})?@(s\.whatsapp\.net|lid)$/.exec(value);
  if (!found || found[2] === "s.whatsapp.net" && !/^[0-9]{8,15}$/.test(found[1])) return null;
  return `${found[1]}@${found[2]}`;
};
var equalBytes = (left, right) => left instanceof Uint8Array && right instanceof Uint8Array && left.byteLength === right.byteLength && left.every((value, index) => value === right[index]);
function imageOf(original) {
  let content = original?.message;
  for (let depth = 0; depth < 5; depth++) {
    need3(object2(content) && !content.editedMessage && !content.groupStatusMessage && !content.groupStatusMessageV2, "C454_ORIGINAL_WRAPPER");
    const found = WRAPPERS.filter((key) => content[key] != null);
    need3(found.length <= 1, "C454_ORIGINAL_WRAPPER");
    if (!found.length) break;
    content = content[found[0]]?.message;
  }
  need3(object2(content) && !WRAPPERS.some((key) => content[key] != null) && object2(content.imageMessage), "C454_IMAGE_MESSAGE_REQUIRED");
  need3(![
    "conversation",
    "extendedTextMessage",
    "stickerMessage",
    "audioMessage",
    "videoMessage",
    "documentMessage",
    "locationMessage",
    "liveLocationMessage"
  ].some((key) => content[key] != null), "C454_AMBIGUOUS_CONTENT");
  return content.imageMessage;
}
function fileLength(value) {
  if (value == null) return null;
  if (Number.isSafeInteger(value) && value >= 0) return value;
  if (typeof value === "bigint" && value >= 0n && value <= BigInt(Number.MAX_SAFE_INTEGER)) return Number(value);
  if (typeof value === "string" && /^[0-9]{1,16}$/.test(value)) {
    const number = Number(value);
    if (Number.isSafeInteger(number)) return number;
  }
  if (object2(value) && Number.isInteger(value.low) && Number.isInteger(value.high) && value.low >= -2147483648 && value.low <= 4294967295 && value.high >= 0 && value.high <= 2097151) {
    const number = value.high * 4294967296 + (value.low >>> 0);
    if (Number.isSafeInteger(number)) return number;
  }
  throw error("C454_FILE_LENGTH");
}
function signatureMatches(mime, bytes) {
  if (mime === "image/png") return bytes.length >= 8 && [137, 80, 78, 71, 13, 10, 26, 10].every((value, index) => bytes[index] === value);
  if (mime === "image/jpeg") return bytes.length >= 3 && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  return bytes.length >= 12 && [82, 73, 70, 70].every((value, index) => bytes[index] === value) && [87, 69, 66, 80].every((value, index) => bytes[index + 8] === value);
}
function allowedMediaLocation(media) {
  if (media.url !== void 0 && media.url !== null) {
    need3(typeof media.url === "string" && media.url.length <= 4096 && !/[\\\r\n]/.test(media.url), "C454_MEDIA_LOCATION");
    let url;
    try {
      url = new URL(media.url);
    } catch {
      throw error("C454_MEDIA_LOCATION");
    }
    need3(url.origin === ORIGIN && !url.username && !url.password && !url.hash, "C454_MEDIA_LOCATION");
  }
  if (media.directPath !== void 0 && media.directPath !== null) {
    need3(typeof media.directPath === "string" && media.directPath.length <= 4096 && media.directPath.startsWith("/") && !media.directPath.startsWith("//") && !/[\\\r\n#]/.test(media.directPath) && new URL(media.directPath, ORIGIN).origin === ORIGIN, "C454_MEDIA_LOCATION");
  }
  need3(Boolean(media.url || media.directPath), "C454_MEDIA_LOCATION");
}
function createBaileysOriginalImageDownloader(options = {}) {
  const enabled = options?.enabled;
  if (enabled !== true) return Object.freeze({
    downloadOriginal: async () => {
      throw error("C454_DOWNLOAD_OFF");
    },
    close: async () => {
    },
    inspectMetadata: () => ({
      enabled: false,
      closed: false,
      active: false,
      blocked: false,
      status: "OFF",
      native_download_accredited: false,
      physical_close_accredited: false
    })
  });
  const {
    tenantId,
    expectedAccountJid,
    installed,
    privateBindings,
    timeoutMs = 15e3,
    cleanupTimeoutMs = 1e3
  } = options;
  const expected = account2(expectedAccountJid);
  need3(typeof tenantId === "string" && /^[A-Za-z0-9_-]{1,80}$/.test(tenantId) && !tenantId.includes("__UNBOUND") && expected && expected === expectedAccountJid, "C454_PRIVATE_IDENTITY");
  need3(object2(installed) && typeof installed.getMediaKeys === "function", "C454_INSTALLED_EXPORT_REQUIRED");
  need3(object2(privateBindings) && typeof privateBindings.acquireOriginalLease === "function" && typeof privateBindings.openOwnedDispatcher === "function" && typeof privateBindings.fetch === "function", "C454_PRIVATE_BINDING_REQUIRED");
  const crypto2 = privateBindings.crypto;
  need3(object2(crypto2) && ["createHash", "createHmac", "createDecipheriv", "timingSafeEqual"].every((name) => typeof crypto2[name] === "function"), "C454_NODE_CRYPTO_REQUIRED");
  need3(
    Number.isSafeInteger(timeoutMs) && timeoutMs >= 1 && timeoutMs <= 3e4 && Number.isSafeInteger(cleanupTimeoutMs) && cleanupTimeoutMs >= 1 && cleanupTimeoutMs <= 2e3,
    "C454_TIMEOUT_CONFIG"
  );
  const derive = installed.getMediaKeys;
  const acquireLease = privateBindings.acquireOriginalLease;
  const openDispatcher = privateBindings.openOwnedDispatcher;
  const privateFetch = privateBindings.fetch;
  const { createHash: createHash3, createHmac, createDecipheriv, timingSafeEqual } = crypto2;
  let closed = false, blocked = false, active = null, lastStatus = "READY_NOT_ACCREDITED";
  async function downloadOriginal(args = {}) {
    need3(!closed && !blocked && !active, "C454_DOWNLOAD_UNAVAILABLE");
    const startedAt = Date.now(), deadlineAt = startedAt + timeoutMs;
    const controller = new AbortController();
    let originalMessage, source, reservation, media, mime, peer, advertised;
    let originalDigest = null, originalEncDigest = null, mediaKey = null, location = null, keySnapshot;
    let lease = null, socket = null, epoch = null, dispatcher = null, responseBody = null, reader = null;
    let readerEOF = false, destroyPromise = null, readerCancelPromise = null, bodyCancelPromise = null;
    let unsubscribe = null, unsubscribed = false, timer = null, failure = null, returned = false;
    let wireStorage = null, plainStorage = null, keys = null, hmacDigest = null;
    let encDigest = null, plainDigest = null, part = null, tail = null, cleanupConfirmed = false;
    let cleanupDeadline = null;
    let rejectCancelled, resolveDone, rejectDone;
    const cancelled = new Promise((_, reject) => {
      rejectCancelled = reject;
    });
    void cancelled.catch(() => {
    });
    const done = new Promise((resolve, reject) => {
      resolveDone = resolve;
      rejectDone = reject;
    });
    void done.catch(() => {
    });
    const operation = { cancel, done };
    active = operation;
    function timeCurrent() {
      const now = Date.now();
      return Number.isSafeInteger(now) && now >= startedAt && now < deadlineAt;
    }
    function current() {
      try {
        return !closed && !blocked && active === operation && lease !== null && lease.socket === socket && lease.socketEpoch === epoch && account2(socket.user?.id) === expected && lease.account_jid === expected && lease.originalMessage === originalMessage && lease.source === source && lease.reservation === reservation && lease.isCurrent() === true && originalMessage.key === source.key && JSON.stringify(source.key) === keySnapshot && source.tenantId === tenantId && source.account_jid === expected && source.message_id === originalMessage.key.id && (peer.endsWith("@lid") ? peer : peer.split("@")[0]) === source.sender && imageOf(originalMessage) === media && media.mimetype === mime && media.url === location.url && media.directPath === location.directPath && equalBytes(media.mediaKey, mediaKey) && fileLength(media.fileLength) === advertised && equalBytes(media.fileSha256, originalDigest) && (originalEncDigest === null ? media.fileEncSha256 == null : equalBytes(media.fileEncSha256, originalEncDigest)) && lease.socket === socket && lease.socketEpoch === epoch && !failure && !closed && !blocked && active === operation;
      } catch {
        return false;
      }
    }
    function checkStartup() {
      if (failure) throw error(failure);
      if (!timeCurrent()) {
        cancel("C454_DOWNLOAD_TIMEOUT");
        throw error(failure);
      }
      if (closed || blocked || active !== operation) {
        cancel("C454_DOWNLOAD_CLOSED");
        throw error(failure);
      }
    }
    function check() {
      checkStartup();
      const valid = current();
      checkStartup();
      if (!valid) {
        cancel("C454_ORIGINAL_INVALIDATED");
        throw error(failure);
      }
    }
    function settlePrivate(call) {
      let resolveSettled;
      const pending = new Promise((resolve) => {
        resolveSettled = resolve;
      });
      return { pending, start() {
        try {
          Promise.resolve(call()).then(() => resolveSettled(true), () => resolveSettled(false));
        } catch {
          resolveSettled(false);
        }
      } };
    }
    function startCleanup() {
      if (cleanupDeadline === null) cleanupDeadline = Date.now() + cleanupTimeoutMs;
      if (reader && !readerCancelPromise) {
        const task = settlePrivate(() => reader.cancel(error("C454_READER_CANCELLED")));
        readerCancelPromise = task.pending;
        task.start();
      } else if (!reader && responseBody && !bodyCancelPromise) {
        const task = settlePrivate(() => {
          need3(responseBody.locked !== true && typeof responseBody.cancel === "function", "C454_BODY_CANCEL_UNAVAILABLE");
          return responseBody.cancel(error("C454_BODY_CANCELLED"));
        });
        bodyCancelPromise = task.pending;
        task.start();
      }
      if (dispatcher && !destroyPromise) {
        const task = settlePrivate(() => dispatcher.destroy());
        destroyPromise = task.pending;
        task.start();
      }
    }
    function cancel(code) {
      if (failure) return;
      failure = code;
      blocked = true;
      lastStatus = code;
      try {
        controller.abort(error(code));
      } catch {
      }
      startCleanup();
      rejectCancelled(error(code));
    }
    async function boundedCleanup() {
      startCleanup();
      const waits = [readerCancelPromise, bodyCancelPromise, destroyPromise].filter(Boolean);
      if (!waits.length) return true;
      let cleanupTimer;
      try {
        const outcome = await Promise.race([
          Promise.all(waits).then((results) => results.every((value) => value === true)),
          new Promise((resolve) => {
            cleanupTimer = setTimeout(
              () => resolve(false),
              Math.max(0, cleanupDeadline - Date.now())
            );
          })
        ]);
        if (outcome !== true) return false;
        if (reader) {
          try {
            reader.releaseLock();
          } catch {
            return false;
          }
        }
        return true;
      } finally {
        clearTimeout(cleanupTimer);
      }
    }
    function unsubscribeOnce() {
      if (unsubscribed) return true;
      unsubscribed = true;
      try {
        unsubscribe?.();
        return true;
      } catch {
        blocked = true;
        lastStatus = "C454_UNSUBSCRIBE_FAILED";
        return false;
      }
    }
    function wipe() {
      for (const bytes of [
        wireStorage,
        mediaKey,
        originalDigest,
        originalEncDigest,
        keys?.iv,
        keys?.cipherKey,
        keys?.macKey,
        hmacDigest,
        encDigest,
        plainDigest,
        part,
        tail
      ]) {
        try {
          bytes?.fill(0);
        } catch {
        }
      }
      if (!returned) {
        try {
          plainStorage?.fill(0);
        } catch {
        }
      }
    }
    function digestSha256(bytes) {
      check();
      const hash4 = createHash3("sha256");
      need3(object2(hash4) && typeof hash4.update === "function" && typeof hash4.digest === "function", "C454_HASH_SHAPE");
      hash4.update(bytes);
      const result = hash4.digest();
      check();
      need3(result instanceof Uint8Array && result.byteLength === 32, "C454_HASH_RESULT");
      return result;
    }
    function compareDigest(left, right, code) {
      check();
      need3(left instanceof Uint8Array && right instanceof Uint8Array && left.byteLength === right.byteLength && timingSafeEqual(left, right) === true, code);
      check();
    }
    async function work() {
      try {
        checkStartup();
        ({ originalMessage, source, reservation } = args);
        need3(object2(originalMessage) && object2(originalMessage.key) && object2(source) && originalMessage.key === source.key && originalMessage.key.fromMe !== true && source.tenantId === tenantId && source.account_jid === expected && typeof source.message_id === "string" && /^[A-Za-z0-9_.:-]{1,160}$/.test(source.message_id) && source.message_id === originalMessage.key.id, "C454_ORIGINAL_SOURCE_BINDING");
        peer = account2(source.key.remoteJid);
        need3(
          peer && (peer.endsWith("@lid") ? peer : peer.split("@")[0]) === source.sender,
          "C454_ORIGINAL_PEER_BINDING"
        );
        media = imageOf(originalMessage);
        mime = media.mimetype;
        need3(["image/png", "image/jpeg", "image/webp"].includes(mime), "C454_ORIGINAL_MIME");
        need3(media.mediaKey instanceof Uint8Array && media.mediaKey.length === 32, "C454_ORIGINAL_MEDIA_KEY");
        need3(
          media.fileSha256 instanceof Uint8Array && media.fileSha256.byteLength === 32,
          "C454_ORIGINAL_DIGEST_REQUIRED"
        );
        originalDigest = Uint8Array.from(media.fileSha256);
        if (media.fileEncSha256 != null) {
          need3(
            media.fileEncSha256 instanceof Uint8Array && media.fileEncSha256.byteLength === 32,
            "C454_ORIGINAL_ENCRYPTED_DIGEST"
          );
          originalEncDigest = Uint8Array.from(media.fileEncSha256);
        }
        advertised = fileLength(media.fileLength);
        need3(advertised === null || advertised > 0 && advertised <= MAX_BYTES, "C454_IMAGE_LIMIT");
        allowedMediaLocation(media);
        mediaKey = Uint8Array.from(media.mediaKey);
        location = { directPath: media.directPath, url: media.url };
        const downloadUrl = new URL(location.directPath || location.url, ORIGIN).href;
        const expectedWire = advertised === null ? null : 16 * (Math.floor(advertised / 16) + 1) + 10;
        keySnapshot = JSON.stringify(source.key);
        checkStartup();
        lease = acquireLease({ originalMessage, source, reservation });
        need3(
          object2(lease) && lease.originalMessage === originalMessage && lease.source === source && lease.reservation === reservation && object2(reservation) && object2(lease.socket) && lease.account_jid === expected && Number.isSafeInteger(lease.socketEpoch) && lease.socketEpoch > 0 && typeof lease.isCurrent === "function" && typeof lease.onInvalidate === "function",
          "C454_PRIVATE_LEASE_REQUIRED"
        );
        socket = lease.socket;
        epoch = lease.socketEpoch;
        check();
        unsubscribe = lease.onInvalidate(() => cancel("C454_ORIGINAL_INVALIDATED"));
        need3(typeof unsubscribe === "function", "C454_INVALIDATION_SUBSCRIPTION_REQUIRED");
        check();
        const derived = await derive(mediaKey, "image");
        check();
        need3(object2(derived) && derived.iv instanceof Uint8Array && derived.iv.byteLength === 16 && derived.cipherKey instanceof Uint8Array && derived.cipherKey.byteLength === 32 && derived.macKey instanceof Uint8Array && derived.macKey.byteLength === 32, "C454_MEDIA_KEYS_RESULT");
        keys = {
          iv: Uint8Array.from(derived.iv),
          cipherKey: Uint8Array.from(derived.cipherKey),
          macKey: Uint8Array.from(derived.macKey)
        };
        check();
        dispatcher = openDispatcher({ origin: ORIGIN, signal: controller.signal });
        need3(object2(dispatcher) && typeof dispatcher.dispatch === "function" && typeof dispatcher.destroy === "function", "C454_OWNED_DISPATCHER_REQUIRED");
        if (failure) startCleanup();
        check();
        const response = await privateFetch(downloadUrl, {
          method: "GET",
          dispatcher,
          signal: controller.signal,
          redirect: "error",
          headers: { Origin: "https://web.whatsapp.com", "Accept-Encoding": "identity" }
        });
        responseBody = response?.body;
        if (failure || !current() || !timeCurrent()) startCleanup();
        check();
        need3(object2(response) && response.status === 200 && response.redirected === false && response.url === downloadUrl && object2(response.headers) && typeof response.headers.get === "function", "C454_HTTP_RESPONSE");
        const encoding = response.headers.get("content-encoding");
        need3(
          encoding === null || typeof encoding === "string" && encoding.trim().toLowerCase() === "identity",
          "C454_CONTENT_ENCODING"
        );
        const contentLength = response.headers.get("content-length");
        if (contentLength !== null) {
          need3(typeof contentLength === "string" && /^[0-9]{1,10}$/.test(contentLength), "C454_CONTENT_LENGTH");
          const length = Number(contentLength);
          need3(Number.isSafeInteger(length) && length >= 26 && length <= MAX_WIRE_BYTES && (expectedWire === null || length === expectedWire), "C454_CONTENT_LENGTH");
        }
        need3(object2(responseBody) && responseBody.locked !== true && typeof responseBody.getReader === "function", "C454_WEB_BODY_REQUIRED");
        check();
        reader = responseBody.getReader();
        need3(object2(reader) && typeof reader.read === "function" && typeof reader.cancel === "function" && typeof reader.releaseLock === "function", "C454_WEB_READER_REQUIRED");
        if (failure) startCleanup();
        check();
        wireStorage = new Uint8Array(MAX_WIRE_BYTES);
        let size = 0;
        for (; ; ) {
          check();
          const next = await reader.read();
          check();
          need3(object2(next) && typeof next.done === "boolean", "C454_WEB_READ_RESULT");
          if (next.done) {
            readerEOF = true;
            break;
          }
          need3(next.value instanceof Uint8Array, "C454_WEB_CHUNK");
          need3(next.value.byteLength <= MAX_WIRE_BYTES - size, "C454_ENCRYPTED_LIMIT");
          wireStorage.set(next.value, size);
          size += next.value.byteLength;
        }
        check();
        need3(size >= 26 && (size - 10) % 16 === 0 && (expectedWire === null || size === expectedWire), "C454_ENCRYPTED_LENGTH");
        const wire = wireStorage.subarray(0, size), ciphertext = wire.subarray(0, size - 10);
        const tag = wire.subarray(size - 10);
        const hmac = createHmac("sha256", keys.macKey);
        need3(object2(hmac) && typeof hmac.update === "function" && typeof hmac.digest === "function", "C454_HMAC_SHAPE");
        hmac.update(keys.iv);
        hmac.update(ciphertext);
        hmacDigest = hmac.digest();
        check();
        need3(hmacDigest instanceof Uint8Array && hmacDigest.byteLength === 32, "C454_HMAC_RESULT");
        compareDigest(hmacDigest.subarray(0, 10), tag, "C454_MEDIA_MAC_MISMATCH");
        if (originalEncDigest !== null) {
          encDigest = digestSha256(wire);
          compareDigest(encDigest, originalEncDigest, "C454_ENCRYPTED_DIGEST_MISMATCH");
        }
        check();
        const decipher = createDecipheriv("aes-256-cbc", keys.cipherKey, keys.iv);
        need3(
          object2(decipher) && typeof decipher.update === "function" && typeof decipher.final === "function",
          "C454_DECIPHER_SHAPE"
        );
        part = decipher.update(ciphertext);
        check();
        need3(part instanceof Uint8Array && part.byteLength <= MAX_BYTES, "C454_IMAGE_LIMIT");
        tail = decipher.final();
        check();
        need3(tail instanceof Uint8Array && tail.byteLength <= MAX_BYTES - part.byteLength, "C454_IMAGE_LIMIT");
        const plainSize = part.byteLength + tail.byteLength;
        need3(plainSize > 0 && (advertised === null || advertised === plainSize), "C454_IMAGE_LENGTH_MISMATCH");
        plainStorage = new Uint8Array(plainSize);
        plainStorage.set(part);
        plainStorage.set(tail, part.byteLength);
        need3(signatureMatches(mime, plainStorage), "C454_IMAGE_SIGNATURE_MISMATCH");
        plainDigest = digestSha256(plainStorage);
        compareDigest(plainDigest, originalDigest, "C454_IMAGE_DIGEST_MISMATCH");
        check();
        return { mime_type: mime, bytes: plainStorage };
      } finally {
        if (failure) {
          startCleanup();
          wipe();
        }
      }
    }
    timer = setTimeout(() => cancel("C454_DOWNLOAD_TIMEOUT"), timeoutMs);
    const pendingWork = work();
    void pendingWork.catch(() => {
    });
    void (async () => {
      try {
        checkStartup();
        const result = await Promise.race([pendingWork, cancelled]);
        check();
        need3(readerEOF, "C454_UPSTREAM_INCOMPLETE");
        const cleanupOk = await boundedCleanup();
        check();
        need3(cleanupOk === true, "C454_DISPATCHER_CLOSE_UNCONFIRMED");
        cleanupConfirmed = true;
        need3(unsubscribeOnce(), "C454_UNSUBSCRIBE_FAILED");
        check();
        returned = true;
        lastStatus = "IMAGE_RETURNED_BINDINGS_NOT_ACCREDITED";
        return result;
      } catch (caught) {
        const code = typeof caught?.message === "string" && /^C454_[A-Z0-9_]+$/.test(caught.message) ? caught.message : "C454_DOWNLOAD_FAILED";
        cancel(code);
        await boundedCleanup();
        throw error(failure || code);
      } finally {
        clearTimeout(timer);
        const unsubscribedOk = unsubscribeOnce();
        if (!unsubscribedOk) {
          returned = false;
          blocked = true;
        }
        wipe();
        if (!returned && !cleanupConfirmed) blocked = true;
        if (active === operation) active = null;
        if (!unsubscribedOk) throw error("C454_UNSUBSCRIBE_FAILED");
      }
    })().then(resolveDone, rejectDone);
    return done;
  }
  return Object.freeze({
    downloadOriginal,
    close: async () => {
      closed = true;
      lastStatus = "CLOSED";
      const pending = active;
      if (pending) {
        pending.cancel("C454_DOWNLOAD_CLOSED");
        await pending.done.catch(() => {
        });
      }
    },
    inspectMetadata: () => ({
      enabled: true,
      closed,
      active: active !== null,
      blocked,
      status: lastStatus,
      max_bytes: MAX_BYTES,
      max_wire_bytes: MAX_WIRE_BYTES,
      timeout_ms: timeoutMs,
      native_download_accredited: false,
      physical_close_accredited: false,
      original_custody_accredited: false
    })
  });
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c453/owned-dispatcher-preparation/owned-undici-dispatcher-r02.mjs
var ORIGIN2 = "https://mmg.whatsapp.net";
var MAX_ENCRYPTED_BYTES = 8 * 1024 * 1024 + 32;
var object3 = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
var technical = (code) => new Error(code);
var need4 = (condition, code) => {
  if (!condition) throw technical(code);
};
function createOwnedMediaDispatcherFactory(options = {}) {
  if (options.enabled !== true) return Object.freeze({
    openOwnedDispatcher: () => {
      throw technical("C453_DISPATCHER_OFF");
    },
    close: async () => {
    },
    inspectMetadata: () => ({
      enabled: false,
      closed: false,
      blocked: false,
      active_dispatchers: 0,
      physical_close_accredited: false
    })
  });
  const { installed, cleanupTimeoutMs = 1e3 } = options;
  need4(object3(installed) && typeof installed.Agent === "function", "C453_PRIVATE_AGENT_REQUIRED");
  need4(
    Number.isSafeInteger(cleanupTimeoutMs) && cleanupTimeoutMs >= 1 && cleanupTimeoutMs <= 2e3,
    "C453_CLEANUP_TIMEOUT"
  );
  const Agent = installed.Agent;
  let factoryClosed = false, factoryBlocked = false, active = null;
  function openOwnedDispatcher({ origin, signal } = {}) {
    need4(!factoryClosed && !factoryBlocked && active === null, "C453_FACTORY_UNAVAILABLE");
    need4(origin === ORIGIN2, "C453_FIXED_ORIGIN_REQUIRED");
    need4(signal instanceof AbortSignal && !signal.aborted, "C453_PRIVATE_ABORT_SIGNAL_REQUIRED");
    const agent = new Agent({
      connections: 1,
      pipelining: 0,
      allowH2: false,
      maxRedirections: 0,
      interceptors: { Agent: [] },
      maxResponseSize: MAX_ENCRYPTED_BYTES,
      strictContentLength: true,
      headersTimeout: 15e3,
      bodyTimeout: 15e3,
      connect: { timeout: 1e4 }
    });
    need4(
      object3(agent) && typeof agent.dispatch === "function" && typeof agent.destroy === "function",
      "C453_AGENT_SHAPE"
    );
    let closed = false, used = false, terminal = false, connectCount = 0, byteCount = 0;
    let nativeDestroy = null, closePromise = null, nativeDestroyResolved = false;
    let requestHandler = null, abortRequest = null, timer = null, lastCode = "UNUSED";
    const record2 = { destroy: null };
    active = record2;
    function notifyError(code) {
      if (terminal) return;
      terminal = true;
      lastCode = code;
      try {
        requestHandler?.onError(technical(code));
      } catch {
        factoryBlocked = true;
      }
    }
    function beginDestroy() {
      if (nativeDestroy) return nativeDestroy;
      let resolveNative;
      nativeDestroy = new Promise((resolve) => {
        resolveNative = resolve;
      });
      closed = true;
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      if (abortRequest && !terminal) {
        try {
          abortRequest(technical("C453_OWNED_REQUEST_ABORTED"));
        } catch {
          factoryBlocked = true;
        }
      }
      if (used && !terminal) notifyError("C453_OWNED_REQUEST_ABORTED");
      try {
        Promise.resolve(agent.destroy()).then(() => {
          nativeDestroyResolved = true;
          resolveNative(true);
        }, () => resolveNative(false));
      } catch {
        resolveNative(false);
      }
      return nativeDestroy;
    }
    function destroy() {
      if (closePromise) return closePromise;
      let resolveClose, rejectClose;
      closePromise = new Promise((resolve, reject) => {
        resolveClose = resolve;
        rejectClose = reject;
      });
      void closePromise.catch(() => {
      });
      const pending = beginDestroy();
      void (async () => {
        let cleanupTimer;
        try {
          const confirmed = await Promise.race([pending, new Promise((resolve) => {
            cleanupTimer = setTimeout(() => resolve(false), cleanupTimeoutMs);
          })]);
          if (confirmed !== true) {
            factoryBlocked = true;
            lastCode = "C453_AGENT_CLOSE_UNCONFIRMED";
            throw technical(lastCode);
          }
          lastCode = "AGENT_DESTROY_RESOLVED_NOT_ACREDITED";
          if (active === record2) active = null;
          resolveClose();
        } catch {
          factoryBlocked = true;
          lastCode = "C453_AGENT_CLOSE_UNCONFIRMED";
          rejectClose(technical(lastCode));
        } finally {
          clearTimeout(cleanupTimer);
        }
      })();
      return closePromise;
    }
    record2.destroy = destroy;
    function stop(code) {
      notifyError(code);
      void destroy().catch(() => {
      });
    }
    function onAbort() {
      stop("C453_PRIVATE_SIGNAL_ABORTED");
    }
    signal.addEventListener("abort", onAbort, { once: true });
    if (signal.aborted) onAbort();
    function dispatch(request, handler) {
      need4(object3(handler) && ["onConnect", "onError", "onHeaders", "onData", "onComplete"].every((name) => typeof handler[name] === "function"), "C453_HANDLER_CONTRACT");
      if (closed || used || factoryClosed || factoryBlocked || signal.aborted) {
        try {
          handler.onError(technical("C453_ONE_REQUEST_ONLY"));
        } catch {
          factoryBlocked = true;
        }
        return false;
      }
      used = true;
      requestHandler = handler;
      try {
        need4(object3(request) && (typeof request.origin === "string" || request.origin instanceof URL), "C453_REQUEST_ORIGIN");
        const parsedOrigin = new URL(String(request.origin));
        need4(parsedOrigin.origin === ORIGIN2 && parsedOrigin.href === `${ORIGIN2}/` && !parsedOrigin.username && !parsedOrigin.password, "C453_REQUEST_ORIGIN");
        need4(request.method === "GET" && request.body == null && request.upgrade == null && request.query == null && request.expectContinue !== true, "C453_GET_ONLY");
        need4(typeof request.path === "string" && request.path.length >= 1 && request.path.length <= 4096 && request.path.startsWith("/") && !request.path.startsWith("//") && !/[\\\r\n#]/.test(request.path) && new URL(request.path, ORIGIN2).origin === ORIGIN2, "C453_REQUEST_PATH");
        timer = setTimeout(() => stop("C453_ABSOLUTE_REQUEST_TIMEOUT"), 15e3);
        lastCode = "ONE_DISPATCH_STARTED";
        const guardedHandler = {
          onConnect(abort, context) {
            connectCount++;
            if (connectCount !== 1 || closed || signal.aborted) {
              try {
                abort(technical("C453_SECOND_CONNECT_DENIED"));
              } catch {
              }
              stop("C453_SECOND_CONNECT_DENIED");
              return;
            }
            abortRequest = abort;
            return handler.onConnect(abort, context);
          },
          onError() {
            stop("C453_REQUEST_FAILED");
          },
          onHeaders(statusCode, headers, resume, statusText) {
            if (terminal || closed || signal.aborted) {
              stop("C453_REQUEST_INACTIVE");
              return false;
            }
            if (Number.isInteger(statusCode) && statusCode >= 100 && statusCode < 200) return true;
            if (statusCode !== 200) {
              stop(statusCode >= 300 && statusCode < 400 ? "C453_REDIRECT_DENIED" : "C453_HTTP_STATUS_DENIED");
              return false;
            }
            return handler.onHeaders(statusCode, headers, resume, statusText);
          },
          onData(chunk) {
            if (terminal || closed || signal.aborted) {
              stop("C453_REQUEST_INACTIVE");
              return false;
            }
            if (!(chunk instanceof Uint8Array) || chunk.byteLength > MAX_ENCRYPTED_BYTES - byteCount) {
              stop("C453_ENCRYPTED_RESPONSE_LIMIT");
              return false;
            }
            byteCount += chunk.byteLength;
            return handler.onData(chunk);
          },
          onComplete(trailers) {
            if (terminal || closed || signal.aborted) return;
            terminal = true;
            clearTimeout(timer);
            lastCode = "RESPONSE_COMPLETE";
            return handler.onComplete(trailers);
          },
          onUpgrade() {
            stop("C453_UPGRADE_DENIED");
          }
        };
        if (typeof handler.onResponseStarted === "function") guardedHandler.onResponseStarted = () => {
          if (!closed && !terminal) return handler.onResponseStarted();
        };
        if (typeof handler.onRequestSent === "function") guardedHandler.onRequestSent = () => {
          if (!closed && !terminal) return handler.onRequestSent();
        };
        return agent.dispatch({
          origin: ORIGIN2,
          path: request.path,
          method: "GET",
          headers: request.headers,
          body: null,
          idempotent: false,
          blocking: true,
          reset: true,
          maxRedirections: 0,
          headersTimeout: 15e3,
          bodyTimeout: 15e3
        }, guardedHandler);
      } catch {
        stop("C453_REQUEST_DENIED");
        return false;
      }
    }
    return Object.freeze({
      dispatch,
      destroy,
      inspectMetadata: () => ({
        closed,
        used,
        terminal,
        connect_count: connectCount,
        encrypted_bytes: byteCount,
        agent_destroy_resolved: nativeDestroyResolved,
        last_code: lastCode,
        physical_close_accredited: false
      })
    });
  }
  return Object.freeze({
    openOwnedDispatcher,
    close: async () => {
      factoryClosed = true;
      if (active) await active.destroy();
    },
    inspectMetadata: () => ({
      enabled: true,
      closed: factoryClosed,
      blocked: factoryBlocked,
      active_dispatchers: active === null ? 0 : 1,
      physical_close_accredited: false
    })
  });
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c460/preparation/native-media-host-callbacks-r01.mjs
import crypto from "node:crypto";

// APP POS/entregas/agente-20261028/native-n8n/native-control-c459/native-vps-preparation/native-media-reservation-client-c459-r01.mjs
import { request as httpsRequest } from "node:https";
var ORIGIN3 = "https://n8n.srv1547104.hstgr.cloud";
var PATH = "/webhook/escalar-native-baileys-c459/private-media/reserve";
var HEADER = "X-Escalar-Native-Gateway";
var MAX_REQUEST_BYTES = 64 * 1024;
var MAX_ENVELOPE_BYTES = 32 * 1024;
var RECEIPT_KEYS = [
  "accepted",
  "durable",
  "tenantId",
  "account_jid",
  "sender",
  "message_id",
  "source_key",
  "duplicate",
  "reservation_id",
  "reservation_run_id",
  "reservation_binding_sha256"
];
var need5 = (condition, code) => {
  if (!condition) throw Error(code);
};
var object4 = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
var exact = (value, keys) => object4(value) && Object.keys(value).sort().join("|") === [...keys].sort().join("|");
var integer = (value) => Number.isSafeInteger(value) && value >= 0;
var identifier = (value) => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,160}$/.test(value) && !value.includes("__UNBOUND");
function canonical(value) {
  const ancestors = /* @__PURE__ */ new Set();
  function visit(item, depth = 0) {
    need5(depth <= 32, "NATIVE_MEDIA_JSON");
    if (item === null || typeof item === "string" || typeof item === "boolean") return JSON.stringify(item);
    if (typeof item === "number" && Number.isFinite(item)) return JSON.stringify(item);
    need5(item && typeof item === "object" && !ancestors.has(item) && !Object.getOwnPropertySymbols(item).length, "NATIVE_MEDIA_JSON");
    const descriptors = Object.getOwnPropertyDescriptors(item), names = Object.keys(descriptors);
    need5(names.every((name) => Object.hasOwn(descriptors[name], "value")), "NATIVE_MEDIA_JSON");
    ancestors.add(item);
    let result;
    if (Array.isArray(item)) {
      const length = descriptors.length?.value;
      need5(integer(length) && length <= 1e4 && names.length === length + 1 && names.includes("length"), "NATIVE_MEDIA_JSON");
      result = "[" + Array.from({ length }, (_, index) => {
        const descriptor = descriptors[String(index)];
        need5(descriptor && descriptor.enumerable, "NATIVE_MEDIA_JSON");
        return visit(descriptor.value, depth + 1);
      }).join(",") + "]";
    } else {
      need5(names.every((name) => descriptors[name].enumerable), "NATIVE_MEDIA_JSON");
      result = "{" + names.sort().map((name) => JSON.stringify(name) + ":" + visit(descriptors[name].value, depth + 1)).join(",") + "}";
    }
    ancestors.delete(item);
    return result;
  }
  return visit(value);
}
var own = (value) => JSON.parse(canonical(value));
var same = (left, right) => canonical(left) === canonical(right);
function jid(value) {
  if (typeof value !== "string") return null;
  const match = /^(\d+)(?::\d{1,5})?@(s\.whatsapp\.net|lid)$/.exec(value);
  if (!match || match[2] === "s.whatsapp.net" && !/^\d{8,15}$/.test(match[1]) || match[2] === "lid" && !/^\d{6,20}$/.test(match[1])) return null;
  return { account: match[1] + "@" + match[2], sender: match[1] + (match[2] === "lid" ? "@lid" : ""), kind: match[2] };
}
function credentialOf(raw) {
  const credential = own(raw);
  need5(exact(credential, ["header_name", "header_value"]) && credential.header_name === HEADER && typeof credential.header_value === "string" && credential.header_value.length >= 1 && credential.header_value.length <= 4096 && !/[\r\n\0]/.test(credential.header_value), "NATIVE_MEDIA_PRIVATE_CREDENTIAL");
  return credential;
}
function createFixedHttpsMediaReservationRequest() {
  let consumed = false;
  return async (input) => {
    need5(!consumed, "NATIVE_MEDIA_HTTPS_ALREADY_CONSUMED");
    consumed = true;
    need5(object4(input) && input.url === ORIGIN3 + PATH && input.method === "POST" && input.redirect === "error" && integer(input.timeoutMs) && input.timeoutMs >= 1 && input.timeoutMs <= 6e4 && integer(input.maxResponseBytes) && input.maxResponseBytes >= 1024 && input.maxResponseBytes <= 64 * 1024 && input.signal && typeof input.signal.addEventListener === "function" && !input.signal.aborted, "NATIVE_MEDIA_FIXED_ENDPOINT");
    const auth = credentialOf(input.credential), body = own(input.body), data = Buffer.from(canonical(body));
    need5(exact(body, ["envelope", "source"]) && data.length <= MAX_REQUEST_BYTES, "NATIVE_MEDIA_REQUEST_BYTES");
    return new Promise((resolve, reject) => {
      let settled = false, timer, request, response;
      const clear = () => {
        if (timer !== void 0) clearTimeout(timer);
        input.signal.removeEventListener("abort", abort);
      };
      const fail = () => {
        if (settled) return;
        settled = true;
        clear();
        response?.destroy();
        request?.destroy();
        reject(Error("NATIVE_MEDIA_HTTPS_UNCONFIRMED_NO_RETRY"));
      };
      const abort = () => fail();
      try {
        request = httpsRequest(ORIGIN3 + PATH, { method: "POST", rejectUnauthorized: true, headers: {
          "content-type": "application/json",
          "content-length": data.length,
          [auth.header_name]: auth.header_value
        } }, (incoming) => {
          response = incoming;
          if ((incoming.statusCode ?? 0) >= 300 && (incoming.statusCode ?? 0) < 400) {
            fail();
            return;
          }
          let bytes = 0;
          const chunks = [];
          incoming.on("data", (chunk) => {
            if (settled) return;
            bytes += chunk.length;
            if (bytes > input.maxResponseBytes) {
              fail();
              return;
            }
            chunks.push(chunk);
          });
          incoming.on("error", fail);
          incoming.on("aborted", fail);
          incoming.on("close", () => {
            if (!incoming.complete) fail();
          });
          incoming.on("end", () => {
            if (settled) return;
            try {
              const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
              settled = true;
              clear();
              resolve({ statusCode: incoming.statusCode ?? 0, body: value, redirected: false, url: ORIGIN3 + PATH });
            } catch {
              fail();
            }
          });
        });
        request.on("error", fail);
        input.signal.addEventListener("abort", abort, { once: true });
        if (input.signal.aborted) {
          fail();
          return;
        }
        timer = setTimeout(abort, input.timeoutMs);
        request.end(data);
      } catch {
        fail();
      }
    });
  };
}
function createNativeMediaReservationClient(options = {}) {
  const enabled = options.enabled;
  if (enabled !== true) return { reserveMedia: async () => ({ status: "OFF", accepted: false, durable: false }), close: async () => {
  } };
  const cfg = own(options.config), readCredential = options.readCredential;
  const providedHTTPS = options.requestHTTPS, providedClock = options.readRealClock;
  const timeoutMs = options.timeoutMs ?? 6e4, maxResponseBytes = options.maxResponseBytes ?? 64 * 1024;
  need5(exact(cfg, ["https_origin", "tenant", "gateway_id", "account_jid"]) && cfg.https_origin === ORIGIN3 && identifier(cfg.tenant) && identifier(cfg.gateway_id) && jid(cfg.account_jid)?.account === cfg.account_jid && typeof readCredential === "function" && (providedHTTPS === void 0 || typeof providedHTTPS === "function") && (providedClock === void 0 || typeof providedClock === "function") && integer(timeoutMs) && timeoutMs >= 1 && timeoutMs <= 6e4 && integer(maxResponseBytes) && maxResponseBytes >= 1024 && maxResponseBytes <= 64 * 1024, "NATIVE_MEDIA_FIXED_CONFIGURATION");
  const requestHTTPS = providedHTTPS ?? createFixedHttpsMediaReservationRequest(), readClock = providedClock ?? Date.now;
  const now = () => {
    const value = readClock();
    need5(integer(value), "NATIVE_MEDIA_CLOCK");
    return value;
  };
  let closed = false, epoch = 0, consumed = false, active = null;
  function checkedRequest(raw) {
    const body = own(raw);
    need5(exact(body, ["envelope", "source"]) && Buffer.byteLength(canonical(body)) <= MAX_REQUEST_BYTES, "NATIVE_MEDIA_BODY");
    const { envelope: e, source: s } = body;
    need5(exact(s, ["tenantId", "account_jid", "sender", "message_id", "key"]) && s.tenantId === cfg.tenant && s.account_jid === cfg.account_jid && identifier(s.message_id) && s.sender === e?.from && s.message_id === e?.message?.id, "NATIVE_MEDIA_SOURCE");
    const primary = jid(s.key?.remoteJid), alt = s.key?.remoteJidAlt == null ? null : jid(s.key.remoteJidAlt);
    const allowedKey = ["remoteJid", "fromMe", "id", "participant", "remoteJidAlt", "participantAlt", "addressingMode", "isViewOnce"];
    need5(object4(s.key) && Buffer.byteLength(canonical(s.key)) <= 8192 && Object.keys(s.key).every((key) => allowedKey.includes(key)) && s.key.id === s.message_id && s.key.fromMe !== true && primary?.sender === s.sender && (s.key.remoteJidAlt == null || alt !== null) && !(alt && primary.kind === alt.kind && primary.sender !== alt.sender) && (s.key.participant == null || s.key.participant === "") && (s.key.participantAlt == null || s.key.participantAlt === ""), "NATIVE_MEDIA_SOURCE_KEY");
    const phone = primary.kind === "s.whatsapp.net" ? primary.sender : alt?.kind === "s.whatsapp.net" ? alt.sender : null;
    const allowedEnvelope = ["tenantId", "channel", "provider", "from", "name", "contact_display_name", "verified_phone", "message", "context", "raw", "media_reservation"];
    need5(object4(e) && Object.keys(e).every((key) => allowedEnvelope.includes(key)) && Buffer.byteLength(canonical(e)) <= MAX_ENVELOPE_BYTES && e.tenantId === cfg.tenant && e.provider === "baileys" && e.channel === "whatsapp" && exact(e.message, ["id", "type", "text", "timestamp"]) && e.message.type === "media" && typeof e.message.text === "string" && e.message.text.length <= 7800 && integer(e.message.timestamp) && exact(e.context, ["isGroup", "hasMedia"]) && e.context.isGroup === false && e.context.hasMedia === true && exact(e.raw, []) && exact(e.media_reservation, ["id", "mime_type"]) && e.media_reservation.id === s.message_id && ["image/png", "image/jpeg", "image/webp"].includes(e.media_reservation.mime_type) && (e.verified_phone || null) === phone && (e.name === null || typeof e.name === "string" && e.name.length <= 120) && (e.contact_display_name === null || typeof e.contact_display_name === "string" && e.contact_display_name.length <= 120), "NATIVE_MEDIA_IMAGE_METADATA_ONLY");
    return body;
  }
  return {
    async reserveMedia(raw) {
      need5(!closed && !consumed, "NATIVE_MEDIA_CLOSED_OR_ALREADY_CONSUMED");
      consumed = true;
      const expectedEpoch = epoch, started = now(), deadline = started + timeoutMs;
      need5(Number.isSafeInteger(deadline), "NATIVE_MEDIA_CLOCK");
      const controller = new AbortController();
      let expired = false, timer, rejectCancellation;
      const cancelled = new Promise((_, reject) => {
        rejectCancellation = reject;
      });
      const operation = { cancel() {
        expired = true;
        controller.abort();
        rejectCancellation(Error("NATIVE_MEDIA_CLOSED"));
      } };
      active = operation;
      const fence = () => {
        const at = now();
        need5(!closed && epoch === expectedEpoch && !expired && at >= started && at < deadline, "NATIVE_MEDIA_DEADLINE_OR_CLOSED");
        return deadline - at;
      };
      try {
        const body = checkedRequest(raw);
        fence();
        timer = setTimeout(() => {
          expired = true;
          controller.abort();
          rejectCancellation(Error("NATIVE_MEDIA_DEADLINE"));
        }, fence());
        const response = await Promise.race([Promise.resolve().then(async () => {
          fence();
          const credential = credentialOf(await readCredential()), remaining = fence();
          const value = await requestHTTPS({
            url: ORIGIN3 + PATH,
            method: "POST",
            body: own(body),
            credential,
            timeoutMs: remaining,
            maxResponseBytes,
            redirect: "error",
            signal: controller.signal
          });
          fence();
          return own(value);
        }), cancelled]);
        fence();
        need5(exact(response, ["statusCode", "body", "redirected", "url"]) && response.statusCode === 200 && response.redirected === false && response.url === ORIGIN3 + PATH && Buffer.byteLength(canonical(response.body)) <= maxResponseBytes, "NATIVE_MEDIA_HTTP_RECEIPT");
        const receipt = response.body;
        need5(exact(receipt, RECEIPT_KEYS) && receipt.accepted === true && receipt.durable === true && typeof receipt.duplicate === "boolean" && receipt.tenantId === body.source.tenantId && receipt.account_jid === body.source.account_jid && receipt.sender === body.source.sender && receipt.message_id === body.source.message_id && same(receipt.source_key, body.source.key) && receipt.reservation_id === body.envelope.media_reservation.id && identifier(receipt.reservation_run_id) && typeof receipt.reservation_binding_sha256 === "string" && /^[a-f0-9]{64}$/.test(receipt.reservation_binding_sha256), "NATIVE_MEDIA_DURABLE_RESERVATION_RECEIPT_REQUIRED");
        fence();
        return receipt;
      } catch {
        throw Error("NATIVE_MEDIA_RESERVATION_UNCONFIRMED_NO_RETRY");
      } finally {
        expired = true;
        if (timer !== void 0) clearTimeout(timer);
        controller.abort();
        if (active === operation) active = null;
      }
    },
    async close() {
      closed = true;
      epoch++;
      const operation = active;
      active = null;
      operation?.cancel();
    }
  };
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c459/client-preparation/native-media-completion-client-r01.mjs
var ORIGIN4 = "https://n8n.srv1547104.hstgr.cloud";
var PATH2 = "/webhook/escalar-native-baileys-c459/private-media/complete";
var HEADER2 = "X-Escalar-Native-Gateway";
var MAX_FILE_BYTES = 8 * 1024 * 1024;
var MAX_METADATA_BYTES = 64 * 1024;
var MAX_ENVELOPE_BYTES2 = 32 * 1024;
var MIME_EXTENSIONS = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp" };
var RECEIPT_KEYS2 = [
  "accepted",
  "durable",
  "tenantId",
  "account_jid",
  "sender",
  "message_id",
  "source_key",
  "duplicate",
  "reservation_id",
  "reservation_run_id",
  "reservation_binding_sha256"
];
var need6 = (condition, code) => {
  if (!condition) throw Error(code);
};
var object5 = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
var exact2 = (value, keys) => object5(value) && Object.keys(value).sort().join("|") === [...keys].sort().join("|");
var integer2 = (value) => Number.isSafeInteger(value) && value >= 0;
var identifier2 = (value) => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,160}$/.test(value) && !value.includes("__UNBOUND");
function canonical2(value) {
  const ancestors = /* @__PURE__ */ new Set();
  function visit(item, depth = 0) {
    need6(depth <= 32, "NATIVE_COMPLETE_JSON");
    if (item === null || typeof item === "string" || typeof item === "boolean") return JSON.stringify(item);
    if (typeof item === "number" && Number.isFinite(item)) return JSON.stringify(item);
    need6(item && typeof item === "object" && !ancestors.has(item) && !Object.getOwnPropertySymbols(item).length, "NATIVE_COMPLETE_JSON");
    const descriptors = Object.getOwnPropertyDescriptors(item), names = Object.keys(descriptors);
    need6(names.every((name) => Object.hasOwn(descriptors[name], "value")), "NATIVE_COMPLETE_JSON");
    ancestors.add(item);
    let result;
    if (Array.isArray(item)) {
      const length = descriptors.length?.value;
      need6(integer2(length) && length <= 1e4 && names.length === length + 1 && names.includes("length"), "NATIVE_COMPLETE_JSON");
      result = "[" + Array.from({ length }, (_, index) => {
        const field = descriptors[String(index)];
        need6(field && field.enumerable, "NATIVE_COMPLETE_JSON");
        return visit(field.value, depth + 1);
      }).join(",") + "]";
    } else {
      need6(names.every((name) => descriptors[name].enumerable), "NATIVE_COMPLETE_JSON");
      result = "{" + names.sort().map((name) => JSON.stringify(name) + ":" + visit(descriptors[name].value, depth + 1)).join(",") + "}";
    }
    ancestors.delete(item);
    return result;
  }
  return visit(value);
}
var own2 = (value) => JSON.parse(canonical2(value));
var same2 = (left, right) => canonical2(left) === canonical2(right);
function jid2(value) {
  if (typeof value !== "string") return null;
  const match = /^(\d+)(?::\d{1,5})?@(s\.whatsapp\.net|lid)$/.exec(value);
  if (!match || match[2] === "s.whatsapp.net" && !/^\d{8,15}$/.test(match[1]) || match[2] === "lid" && !/^\d{6,20}$/.test(match[1])) return null;
  return { account: match[1] + "@" + match[2], sender: match[1] + (match[2] === "lid" ? "@lid" : ""), kind: match[2] };
}
function credentialOf2(raw) {
  const credential = own2(raw);
  need6(exact2(credential, ["header_name", "header_value"]) && credential.header_name === HEADER2 && typeof credential.header_value === "string" && credential.header_value.length >= 1 && credential.header_value.length <= 4096 && !/[\r\n\0]/.test(credential.header_value), "NATIVE_COMPLETE_PRIVATE_CREDENTIAL");
  return credential;
}
function createFixedHttpsMediaCompletionRequest(options = {}) {
  const targetPath = options.targetPath ?? PATH2;
  const fetchImpl = options.fetchImpl ?? globalThis.fetch, FormDataCtor = options.FormDataCtor ?? globalThis.FormData;
  const BlobCtor = options.BlobCtor ?? globalThis.Blob, TextEncoderCtor = options.TextEncoderCtor ?? globalThis.TextEncoder;
  const TextDecoderCtor = options.TextDecoderCtor ?? globalThis.TextDecoder;
  const AbortControllerCtor = options.AbortControllerCtor ?? globalThis.AbortController;
  const readClock = options.readRealClock ?? Date.now;
  need6(targetPath === PATH2 && [fetchImpl, FormDataCtor, BlobCtor, TextEncoderCtor, TextDecoderCtor, AbortControllerCtor, readClock].every((value) => typeof value === "function"), "NATIVE_COMPLETE_HTTPS_CONFIGURATION");
  const encoder = new TextEncoderCtor(), url = ORIGIN4 + targetPath;
  let consumed = false;
  return async (input) => {
    need6(!consumed, "NATIVE_COMPLETE_HTTPS_ALREADY_CONSUMED");
    consumed = true;
    need6(exact2(input, ["url", "method", "body", "credential", "timeoutMs", "maxResponseBytes", "redirect", "signal"]) && input.url === url && input.method === "POST" && input.redirect === "error" && integer2(input.timeoutMs) && input.timeoutMs >= 1 && input.timeoutMs <= 6e4 && integer2(input.maxResponseBytes) && input.maxResponseBytes >= 1024 && input.maxResponseBytes <= 64 * 1024 && input.signal && typeof input.signal.addEventListener === "function" && !input.signal.aborted, "NATIVE_COMPLETE_FIXED_ENDPOINT");
    const credential = credentialOf2(input.credential), form = input.body;
    need6(form instanceof FormDataCtor, "NATIVE_COMPLETE_MULTIPART");
    const parts = [];
    for (const part of form.entries()) {
      parts.push(part);
      need6(parts.length <= 2, "NATIVE_COMPLETE_MULTIPART");
    }
    need6(parts.length === 2 && parts[0][0] === "metadata" && typeof parts[0][1] === "string" && encoder.encode(parts[0][1]).length <= MAX_METADATA_BYTES && parts[1][0] === "file" && parts[1][1] instanceof BlobCtor && parts[1][1].size >= 1 && parts[1][1].size <= MAX_FILE_BYTES && Object.hasOwn(MIME_EXTENSIONS, parts[1][1].type), "NATIVE_COMPLETE_MULTIPART");
    const metadata = own2(JSON.parse(parts[0][1]));
    need6(exact2(metadata, ["envelope", "source", "reservation"]) && metadata.envelope?.media_reservation?.mime_type === parts[1][1].type, "NATIVE_COMPLETE_MULTIPART_IDENTITY");
    const started = readClock(), deadline = started + input.timeoutMs;
    need6(integer2(started) && integer2(deadline), "NATIVE_COMPLETE_CLOCK");
    const controller = new AbortControllerCtor();
    let stopped = false, timer, reader, response, rejectCancellation;
    const cancelled = new Promise((_, reject) => {
      rejectCancellation = reject;
    });
    const cancelReader = () => {
      try {
        const result = reader?.cancel();
        if (result && typeof result.catch === "function") result.catch(() => {
        });
      } catch {
      }
    };
    const cancelResponse = (value) => {
      try {
        const result = value?.body?.cancel();
        if (result && typeof result.catch === "function") result.catch(() => {
        });
      } catch {
      }
    };
    const abort = () => {
      if (stopped) return;
      stopped = true;
      controller.abort();
      cancelReader();
      rejectCancellation(Error("NATIVE_COMPLETE_HTTPS_UNCONFIRMED_NO_RETRY"));
    };
    const fence = () => {
      const at = readClock();
      need6(integer2(at) && at >= started && at < deadline && !stopped && !input.signal.aborted, "NATIVE_COMPLETE_HTTPS_DEADLINE");
    };
    input.signal.addEventListener("abort", abort, { once: true });
    timer = setTimeout(abort, input.timeoutMs);
    try {
      if (input.signal.aborted) abort();
      fence();
      return await Promise.race([Promise.resolve().then(async () => {
        fence();
        response = await fetchImpl(url, {
          method: "POST",
          headers: { [credential.header_name]: credential.header_value },
          body: form,
          redirect: "error",
          credentials: "omit",
          cache: "no-store",
          signal: controller.signal
        });
        if (stopped || input.signal.aborted) {
          cancelResponse(response);
          throw Error("NATIVE_COMPLETE_HTTPS_DEADLINE");
        }
        fence();
        need6(response && response.status === 200 && response.redirected === false && response.url === url && response.body && typeof response.body.getReader === "function", "NATIVE_COMPLETE_HTTPS_RESPONSE");
        reader = response.body.getReader();
        let size = 0;
        const chunks = [];
        while (true) {
          fence();
          const item = await reader.read();
          fence();
          if (item.done) break;
          need6(item.value instanceof Uint8Array, "NATIVE_COMPLETE_RESPONSE_BYTES");
          size += item.value.byteLength;
          need6(size <= input.maxResponseBytes, "NATIVE_COMPLETE_RESPONSE_BYTES");
          chunks.push(item.value);
        }
        const bytes = new Uint8Array(size);
        let offset = 0;
        for (const chunk of chunks) {
          bytes.set(chunk, offset);
          offset += chunk.byteLength;
        }
        const value = JSON.parse(new TextDecoderCtor("utf-8", { fatal: true }).decode(bytes));
        fence();
        return { statusCode: 200, body: own2(value), redirected: false, url };
      }), cancelled]);
    } catch {
      throw Error("NATIVE_COMPLETE_HTTPS_UNCONFIRMED_NO_RETRY");
    } finally {
      stopped = true;
      clearTimeout(timer);
      input.signal.removeEventListener("abort", abort);
      controller.abort();
      cancelReader();
      if (!reader) cancelResponse(response);
    }
  };
}
function createNativeMediaCompletionClient(options = {}) {
  const enabled = options.enabled;
  if (enabled !== true) return { completeMedia: async () => ({ status: "OFF", accepted: false, durable: false }), close: async () => {
  } };
  const cfg = own2(options.config), targetPath = options.targetPath ?? PATH2, readCredential = options.readCredential;
  const requestProvided = options.requestHTTPS, readClock = options.readRealClock ?? Date.now;
  const FormDataCtor = options.FormDataCtor ?? globalThis.FormData, BlobCtor = options.BlobCtor ?? globalThis.Blob;
  const TextEncoderCtor = options.TextEncoderCtor ?? globalThis.TextEncoder, AbortControllerCtor = options.AbortControllerCtor ?? globalThis.AbortController;
  const timeoutMs = options.timeoutMs ?? 6e4, maxResponseBytes = options.maxResponseBytes ?? 64 * 1024;
  need6(exact2(cfg, ["https_origin", "tenant", "gateway_id", "account_jid", "scope"]) && cfg.https_origin === ORIGIN4 && targetPath === PATH2 && identifier2(cfg.tenant) && identifier2(cfg.gateway_id) && jid2(cfg.account_jid)?.account === cfg.account_jid && exact2(cfg.scope, ["business_id", "branch_id"]) && Object.values(cfg.scope).every((value) => integer2(value) && value > 0) && [readCredential, readClock, FormDataCtor, BlobCtor, TextEncoderCtor, AbortControllerCtor].every((value) => typeof value === "function") && (requestProvided === void 0 || typeof requestProvided === "function") && integer2(timeoutMs) && timeoutMs >= 1 && timeoutMs <= 6e4 && integer2(maxResponseBytes) && maxResponseBytes >= 1024 && maxResponseBytes <= 64 * 1024, "NATIVE_COMPLETE_FIXED_CONFIGURATION");
  const encoder = new TextEncoderCtor(), byteLength = (value) => encoder.encode(value).length;
  const requestHTTPS = requestProvided ?? createFixedHttpsMediaCompletionRequest({
    targetPath,
    fetchImpl: options.fetchImpl,
    FormDataCtor,
    BlobCtor,
    TextEncoderCtor,
    TextDecoderCtor: options.TextDecoderCtor,
    AbortControllerCtor,
    readRealClock: readClock
  });
  const now = () => {
    const at = readClock();
    need6(integer2(at), "NATIVE_COMPLETE_CLOCK");
    return at;
  };
  const typedArrayByteLength = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(Uint8Array.prototype), "byteLength").get;
  let closed = false, epoch = 0, consumed = false, active = null;
  function checkedRequest(raw) {
    need6(object5(raw) && !Object.getOwnPropertySymbols(raw).length, "NATIVE_COMPLETE_ARGUMENTS");
    const fields = Object.getOwnPropertyDescriptors(raw);
    need6(Object.keys(fields).sort().join("|") === "envelope|file|reservation|source" && Object.values(fields).every((field) => Object.hasOwn(field, "value") && field.enumerable), "NATIVE_COMPLETE_ARGUMENTS");
    const metadata = own2({ envelope: fields.envelope.value, source: fields.source.value, reservation: fields.reservation.value });
    need6(byteLength(canonical2(metadata)) <= MAX_METADATA_BYTES, "NATIVE_COMPLETE_METADATA_BYTES");
    const { envelope: e, source: s, reservation: r } = metadata;
    need6(exact2(s, ["tenantId", "account_jid", "sender", "message_id", "key"]) && s.tenantId === cfg.tenant && s.account_jid === cfg.account_jid && identifier2(s.message_id) && s.sender === e?.from && s.message_id === e?.message?.id, "NATIVE_COMPLETE_SOURCE");
    const primary = jid2(s.key?.remoteJid), alt = s.key?.remoteJidAlt == null ? null : jid2(s.key.remoteJidAlt);
    const allowedKey = ["remoteJid", "fromMe", "id", "participant", "remoteJidAlt", "participantAlt", "addressingMode", "isViewOnce"];
    need6(object5(s.key) && byteLength(canonical2(s.key)) <= 8192 && Object.keys(s.key).every((key) => allowedKey.includes(key)) && s.key.id === s.message_id && s.key.fromMe !== true && primary?.sender === s.sender && (s.key.remoteJidAlt == null || alt !== null) && !(alt && primary.kind === alt.kind && primary.sender !== alt.sender) && (s.key.participant == null || s.key.participant === "") && (s.key.participantAlt == null || s.key.participantAlt === ""), "NATIVE_COMPLETE_SOURCE_KEY");
    const phone = primary.kind === "s.whatsapp.net" ? primary.sender : alt?.kind === "s.whatsapp.net" ? alt.sender : null;
    const allowedEnvelope = ["tenantId", "channel", "provider", "from", "name", "contact_display_name", "verified_phone", "message", "context", "raw", "media_reservation"];
    const optionalName = (key) => !Object.hasOwn(e, key) || e[key] === null || typeof e[key] === "string" && e[key].length <= 120;
    need6(object5(e) && Object.keys(e).every((key) => allowedEnvelope.includes(key)) && byteLength(canonical2(e)) <= MAX_ENVELOPE_BYTES2 && e.tenantId === cfg.tenant && e.provider === "baileys" && e.channel === "whatsapp" && exact2(e.message, ["id", "type", "text", "timestamp"]) && e.message.type === "media" && typeof e.message.text === "string" && e.message.text.length <= 7800 && integer2(e.message.timestamp) && exact2(e.context, ["isGroup", "hasMedia"]) && e.context.isGroup === false && e.context.hasMedia === true && exact2(e.raw, []) && exact2(e.media_reservation, ["id", "mime_type"]) && e.media_reservation.id === s.message_id && Object.hasOwn(MIME_EXTENSIONS, e.media_reservation.mime_type) && (e.verified_phone || null) === phone && optionalName("name") && optionalName("contact_display_name"), "NATIVE_COMPLETE_ORIGINAL_METADATA");
    need6(exact2(r, RECEIPT_KEYS2) && r.accepted === true && r.durable === true && typeof r.duplicate === "boolean" && r.tenantId === s.tenantId && r.account_jid === s.account_jid && r.sender === s.sender && r.message_id === s.message_id && same2(r.source_key, s.key) && r.reservation_id === e.media_reservation.id && identifier2(r.reservation_run_id) && typeof r.reservation_binding_sha256 === "string" && /^[a-f0-9]{64}$/.test(r.reservation_binding_sha256), "NATIVE_COMPLETE_ORIGINAL_RESERVATION");
    const rawFile = fields.file.value;
    need6(rawFile instanceof Uint8Array, "NATIVE_COMPLETE_FILE");
    const length = typedArrayByteLength.call(rawFile);
    need6(integer2(length) && length >= 1 && length <= MAX_FILE_BYTES, "NATIVE_COMPLETE_FILE");
    const file = new Uint8Array(length);
    Uint8Array.prototype.set.call(file, rawFile);
    return { metadata, file, mimeType: e.media_reservation.mime_type };
  }
  return {
    async completeMedia(raw) {
      need6(!closed && !consumed, "NATIVE_COMPLETE_CLOSED_OR_ALREADY_CONSUMED");
      consumed = true;
      const expectedEpoch = epoch, started = now(), deadline = started + timeoutMs;
      need6(integer2(deadline), "NATIVE_COMPLETE_CLOCK");
      const controller = new AbortControllerCtor();
      let stopped = false, timer, rejectCancellation;
      const cancelled = new Promise((_, reject) => {
        rejectCancellation = reject;
      });
      const operation = { cancel() {
        if (stopped) return;
        stopped = true;
        controller.abort();
        rejectCancellation(Error("NATIVE_COMPLETE_CLOSED"));
      } };
      active = operation;
      const fence = () => {
        const at = now();
        need6(!closed && epoch === expectedEpoch && !stopped && at >= started && at < deadline, "NATIVE_COMPLETE_DEADLINE_OR_CLOSED");
        return deadline - at;
      };
      try {
        const checked = checkedRequest(raw);
        fence();
        const body = new FormDataCtor();
        body.append("metadata", JSON.stringify(checked.metadata));
        body.append("file", new BlobCtor([checked.file], { type: checked.mimeType }), "input." + MIME_EXTENSIONS[checked.mimeType]);
        fence();
        timer = setTimeout(() => {
          if (stopped) return;
          stopped = true;
          controller.abort();
          rejectCancellation(Error("NATIVE_COMPLETE_DEADLINE"));
        }, fence());
        const response = await Promise.race([Promise.resolve().then(async () => {
          fence();
          const credential = credentialOf2(await readCredential()), remaining = fence();
          const value = await requestHTTPS({
            url: ORIGIN4 + targetPath,
            method: "POST",
            body,
            credential,
            timeoutMs: remaining,
            maxResponseBytes,
            redirect: "error",
            signal: controller.signal
          });
          fence();
          return own2(value);
        }), cancelled]);
        fence();
        need6(exact2(response, ["statusCode", "body", "redirected", "url"]) && response.statusCode === 200 && response.redirected === false && response.url === ORIGIN4 + targetPath && byteLength(canonical2(response.body)) <= maxResponseBytes, "NATIVE_COMPLETE_HTTP_RECEIPT");
        const receipt = response.body;
        need6(
          exact2(receipt, RECEIPT_KEYS2) && receipt.accepted === true && receipt.durable === true && typeof receipt.duplicate === "boolean" && RECEIPT_KEYS2.filter((key) => key !== "duplicate").every((key) => same2(receipt[key], checked.metadata.reservation[key])),
          "NATIVE_COMPLETE_DURABLE_ORIGINAL_RECEIPT_REQUIRED"
        );
        fence();
        return own2(receipt);
      } catch {
        throw Error("NATIVE_MEDIA_COMPLETION_UNCONFIRMED_NO_RETRY");
      } finally {
        stopped = true;
        if (timer !== void 0) clearTimeout(timer);
        controller.abort();
        if (active === operation) active = null;
      }
    },
    async close() {
      if (closed) return;
      closed = true;
      epoch++;
      const operation = active;
      active = null;
      operation?.cancel();
    }
  };
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c460/preparation/native-media-validation-client-r01.mjs
import { request as httpsRequest2 } from "node:https";
var ORIGIN5 = "https://n8n.srv1547104.hstgr.cloud";
var PATH3 = "/webhook/escalar-native-baileys-c460/private-media/validate";
var HEADER3 = "X-Escalar-Native-Gateway";
var MAX_REQUEST_BYTES2 = 64 * 1024;
var MAX_ENVELOPE_BYTES3 = 32 * 1024;
var RECEIPT_KEYS3 = [
  "accepted",
  "durable",
  "tenantId",
  "account_jid",
  "sender",
  "message_id",
  "source_key",
  "duplicate",
  "reservation_id",
  "reservation_run_id",
  "reservation_binding_sha256"
];
var need7 = (condition, code) => {
  if (!condition) throw Error(code);
};
var object6 = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
var exact3 = (value, keys) => object6(value) && Object.keys(value).sort().join("|") === [...keys].sort().join("|");
var integer3 = (value) => Number.isSafeInteger(value) && value >= 0;
var identifier3 = (value) => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,160}$/.test(value) && !value.includes("__UNBOUND");
function canonical3(value) {
  const ancestors = /* @__PURE__ */ new Set();
  function visit(item, depth = 0) {
    need7(depth <= 32, "NATIVE_MEDIA_JSON");
    if (item === null || typeof item === "string" || typeof item === "boolean") return JSON.stringify(item);
    if (typeof item === "number" && Number.isFinite(item)) return JSON.stringify(item);
    need7(item && typeof item === "object" && !ancestors.has(item) && !Object.getOwnPropertySymbols(item).length, "NATIVE_MEDIA_JSON");
    const descriptors = Object.getOwnPropertyDescriptors(item), names = Object.keys(descriptors);
    need7(names.every((name) => Object.hasOwn(descriptors[name], "value")), "NATIVE_MEDIA_JSON");
    ancestors.add(item);
    let result;
    if (Array.isArray(item)) {
      const length = descriptors.length?.value;
      need7(integer3(length) && length <= 1e4 && names.length === length + 1 && names.includes("length"), "NATIVE_MEDIA_JSON");
      result = "[" + Array.from({ length }, (_, index) => {
        const descriptor = descriptors[String(index)];
        need7(descriptor && descriptor.enumerable, "NATIVE_MEDIA_JSON");
        return visit(descriptor.value, depth + 1);
      }).join(",") + "]";
    } else {
      need7(names.every((name) => descriptors[name].enumerable), "NATIVE_MEDIA_JSON");
      result = "{" + names.sort().map((name) => JSON.stringify(name) + ":" + visit(descriptors[name].value, depth + 1)).join(",") + "}";
    }
    ancestors.delete(item);
    return result;
  }
  return visit(value);
}
var own3 = (value) => JSON.parse(canonical3(value));
var same3 = (left, right) => canonical3(left) === canonical3(right);
function jid3(value) {
  if (typeof value !== "string") return null;
  const match = /^(\d+)(?::\d{1,5})?@(s\.whatsapp\.net|lid)$/.exec(value);
  if (!match || match[2] === "s.whatsapp.net" && !/^\d{8,15}$/.test(match[1]) || match[2] === "lid" && !/^\d{6,20}$/.test(match[1])) return null;
  return { account: match[1] + "@" + match[2], sender: match[1] + (match[2] === "lid" ? "@lid" : ""), kind: match[2] };
}
function credentialOf3(raw) {
  const credential = own3(raw);
  need7(exact3(credential, ["header_name", "header_value"]) && credential.header_name === HEADER3 && typeof credential.header_value === "string" && credential.header_value.length >= 1 && credential.header_value.length <= 4096 && !/[\r\n\0]/.test(credential.header_value), "NATIVE_MEDIA_PRIVATE_CREDENTIAL");
  return credential;
}
function createFixedHttpsMediaValidationRequest() {
  let consumed = false;
  return async (input) => {
    need7(!consumed, "NATIVE_MEDIA_HTTPS_ALREADY_CONSUMED");
    consumed = true;
    need7(object6(input) && input.url === ORIGIN5 + PATH3 && input.method === "POST" && input.redirect === "error" && integer3(input.timeoutMs) && input.timeoutMs >= 1 && input.timeoutMs <= 6e4 && integer3(input.maxResponseBytes) && input.maxResponseBytes >= 1024 && input.maxResponseBytes <= 64 * 1024 && input.signal && typeof input.signal.addEventListener === "function" && !input.signal.aborted, "NATIVE_MEDIA_FIXED_ENDPOINT");
    const auth = credentialOf3(input.credential), body = own3(input.body), data = Buffer.from(canonical3(body));
    need7(exact3(body, ["envelope", "source", "reservation"]) && data.length <= MAX_REQUEST_BYTES2, "NATIVE_MEDIA_REQUEST_BYTES");
    return new Promise((resolve, reject) => {
      let settled = false, timer, request, response;
      const clear = () => {
        if (timer !== void 0) clearTimeout(timer);
        input.signal.removeEventListener("abort", abort);
      };
      const fail = () => {
        if (settled) return;
        settled = true;
        clear();
        response?.destroy();
        request?.destroy();
        reject(Error("NATIVE_MEDIA_HTTPS_UNCONFIRMED_NO_RETRY"));
      };
      const abort = () => fail();
      try {
        request = httpsRequest2(ORIGIN5 + PATH3, { method: "POST", rejectUnauthorized: true, headers: {
          "content-type": "application/json",
          "content-length": data.length,
          [auth.header_name]: auth.header_value
        } }, (incoming) => {
          response = incoming;
          if ((incoming.statusCode ?? 0) >= 300 && (incoming.statusCode ?? 0) < 400) {
            fail();
            return;
          }
          let bytes = 0;
          const chunks = [];
          incoming.on("data", (chunk) => {
            if (settled) return;
            bytes += chunk.length;
            if (bytes > input.maxResponseBytes) {
              fail();
              return;
            }
            chunks.push(chunk);
          });
          incoming.on("error", fail);
          incoming.on("aborted", fail);
          incoming.on("close", () => {
            if (!incoming.complete) fail();
          });
          incoming.on("end", () => {
            if (settled) return;
            try {
              const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
              settled = true;
              clear();
              resolve({ statusCode: incoming.statusCode ?? 0, body: value, redirected: false, url: ORIGIN5 + PATH3 });
            } catch {
              fail();
            }
          });
        });
        request.on("error", fail);
        input.signal.addEventListener("abort", abort, { once: true });
        if (input.signal.aborted) {
          fail();
          return;
        }
        timer = setTimeout(abort, input.timeoutMs);
        request.end(data);
      } catch {
        fail();
      }
    });
  };
}
function createNativeMediaValidationClient(options = {}) {
  const enabled = options.enabled;
  if (enabled !== true) return { validateReservation: async () => ({ status: "OFF", accepted: false, durable: false }), close: async () => {
  } };
  const cfg = own3(options.config), readCredential = options.readCredential;
  const providedHTTPS = options.requestHTTPS, providedClock = options.readRealClock;
  const timeoutMs = options.timeoutMs ?? 6e4, maxResponseBytes = options.maxResponseBytes ?? 64 * 1024;
  need7(exact3(cfg, ["https_origin", "tenant", "gateway_id", "account_jid"]) && cfg.https_origin === ORIGIN5 && identifier3(cfg.tenant) && identifier3(cfg.gateway_id) && jid3(cfg.account_jid)?.account === cfg.account_jid && typeof readCredential === "function" && (providedHTTPS === void 0 || typeof providedHTTPS === "function") && (providedClock === void 0 || typeof providedClock === "function") && integer3(timeoutMs) && timeoutMs >= 1 && timeoutMs <= 6e4 && integer3(maxResponseBytes) && maxResponseBytes >= 1024 && maxResponseBytes <= 64 * 1024, "NATIVE_MEDIA_FIXED_CONFIGURATION");
  const requestHTTPS = providedHTTPS ?? createFixedHttpsMediaValidationRequest(), readClock = providedClock ?? Date.now;
  const now = () => {
    const value = readClock();
    need7(integer3(value), "NATIVE_MEDIA_CLOCK");
    return value;
  };
  let closed = false, epoch = 0, consumed = false, active = null;
  function checkedRequest(raw) {
    const body = own3(raw);
    need7(exact3(body, ["envelope", "source", "reservation"]) && Buffer.byteLength(canonical3(body)) <= MAX_REQUEST_BYTES2, "NATIVE_MEDIA_BODY");
    const { envelope: e, source: s } = body;
    need7(exact3(s, ["tenantId", "account_jid", "sender", "message_id", "key"]) && s.tenantId === cfg.tenant && s.account_jid === cfg.account_jid && identifier3(s.message_id) && s.sender === e?.from && s.message_id === e?.message?.id, "NATIVE_MEDIA_SOURCE");
    const primary = jid3(s.key?.remoteJid), alt = s.key?.remoteJidAlt == null ? null : jid3(s.key.remoteJidAlt);
    const allowedKey = ["remoteJid", "fromMe", "id", "participant", "remoteJidAlt", "participantAlt", "addressingMode", "isViewOnce"];
    need7(object6(s.key) && Buffer.byteLength(canonical3(s.key)) <= 8192 && Object.keys(s.key).every((key) => allowedKey.includes(key)) && s.key.id === s.message_id && s.key.fromMe !== true && primary?.sender === s.sender && (s.key.remoteJidAlt == null || alt !== null) && !(alt && primary.kind === alt.kind && primary.sender !== alt.sender) && (s.key.participant == null || s.key.participant === "") && (s.key.participantAlt == null || s.key.participantAlt === ""), "NATIVE_MEDIA_SOURCE_KEY");
    const phone = primary.kind === "s.whatsapp.net" ? primary.sender : alt?.kind === "s.whatsapp.net" ? alt.sender : null;
    const allowedEnvelope = ["tenantId", "channel", "provider", "from", "name", "contact_display_name", "verified_phone", "message", "context", "raw", "media_reservation"];
    need7(object6(e) && Object.keys(e).every((key) => allowedEnvelope.includes(key)) && Buffer.byteLength(canonical3(e)) <= MAX_ENVELOPE_BYTES3 && e.tenantId === cfg.tenant && e.provider === "baileys" && e.channel === "whatsapp" && exact3(e.message, ["id", "type", "text", "timestamp"]) && e.message.type === "media" && typeof e.message.text === "string" && e.message.text.length <= 7800 && integer3(e.message.timestamp) && exact3(e.context, ["isGroup", "hasMedia"]) && e.context.isGroup === false && e.context.hasMedia === true && exact3(e.raw, []) && exact3(e.media_reservation, ["id", "mime_type"]) && e.media_reservation.id === s.message_id && ["image/png", "image/jpeg", "image/webp"].includes(e.media_reservation.mime_type) && (e.verified_phone || null) === phone && (e.name === null || typeof e.name === "string" && e.name.length <= 120) && (e.contact_display_name === null || typeof e.contact_display_name === "string" && e.contact_display_name.length <= 120), "NATIVE_MEDIA_IMAGE_METADATA_ONLY");
    const r = body.reservation;
    need7(
      exact3(r, RECEIPT_KEYS3) && r.accepted === true && r.durable === true && typeof r.duplicate === "boolean" && r.tenantId === s.tenantId && r.account_jid === s.account_jid && r.sender === s.sender && r.message_id === s.message_id && same3(r.source_key, s.key) && r.reservation_id === e.media_reservation.id && identifier3(r.reservation_run_id) && typeof r.reservation_binding_sha256 === "string" && /^[a-f0-9]{64}$/.test(r.reservation_binding_sha256),
      "NATIVE_MEDIA_VALIDATION_ORIGINAL_RECEIPT"
    );
    return body;
  }
  return {
    async validateReservation(raw) {
      need7(!closed && !consumed, "NATIVE_MEDIA_CLOSED_OR_ALREADY_CONSUMED");
      consumed = true;
      const expectedEpoch = epoch, started = now(), deadline = started + timeoutMs;
      need7(Number.isSafeInteger(deadline), "NATIVE_MEDIA_CLOCK");
      const controller = new AbortController();
      let expired = false, timer, rejectCancellation;
      const cancelled = new Promise((_, reject) => {
        rejectCancellation = reject;
      });
      const operation = { cancel() {
        expired = true;
        controller.abort();
        rejectCancellation(Error("NATIVE_MEDIA_CLOSED"));
      } };
      active = operation;
      const fence = () => {
        const at = now();
        need7(!closed && epoch === expectedEpoch && !expired && at >= started && at < deadline, "NATIVE_MEDIA_DEADLINE_OR_CLOSED");
        return deadline - at;
      };
      try {
        const body = checkedRequest(raw);
        fence();
        timer = setTimeout(() => {
          expired = true;
          controller.abort();
          rejectCancellation(Error("NATIVE_MEDIA_DEADLINE"));
        }, fence());
        const response = await Promise.race([Promise.resolve().then(async () => {
          fence();
          const credential = credentialOf3(await readCredential()), remaining = fence();
          const value = await requestHTTPS({
            url: ORIGIN5 + PATH3,
            method: "POST",
            body: own3(body),
            credential,
            timeoutMs: remaining,
            maxResponseBytes,
            redirect: "error",
            signal: controller.signal
          });
          fence();
          return own3(value);
        }), cancelled]);
        fence();
        need7(exact3(response, ["statusCode", "body", "redirected", "url"]) && response.statusCode === 200 && response.redirected === false && response.url === ORIGIN5 + PATH3 && Buffer.byteLength(canonical3(response.body)) <= maxResponseBytes, "NATIVE_MEDIA_HTTP_RECEIPT");
        const receipt = response.body;
        need7(exact3(receipt, RECEIPT_KEYS3) && receipt.accepted === true && receipt.durable === true && typeof receipt.duplicate === "boolean" && receipt.tenantId === body.source.tenantId && receipt.account_jid === body.source.account_jid && receipt.sender === body.source.sender && receipt.message_id === body.source.message_id && same3(receipt.source_key, body.source.key) && receipt.reservation_id === body.envelope.media_reservation.id && identifier3(receipt.reservation_run_id) && typeof receipt.reservation_binding_sha256 === "string" && /^[a-f0-9]{64}$/.test(receipt.reservation_binding_sha256), "NATIVE_MEDIA_DURABLE_RESERVATION_RECEIPT_REQUIRED");
        need7(same3(receipt, body.reservation), "NATIVE_MEDIA_VALIDATION_ORIGINAL_CHANGED");
        fence();
        return receipt;
      } catch {
        throw Error("NATIVE_MEDIA_VALIDATION_UNCONFIRMED_NO_RETRY");
      } finally {
        expired = true;
        if (timer !== void 0) clearTimeout(timer);
        controller.abort();
        if (active === operation) active = null;
      }
    },
    async close() {
      closed = true;
      epoch++;
      const operation = active;
      active = null;
      operation?.cancel();
    }
  };
}

// APP POS/entregas/agente-20261028/native-n8n/direct-transport-c447/gateway/baileys-inbound-r01.mjs
function createBaileysInboundProjector({ tenantId, maxMessages = 100, maxTextChars = 4e3 } = {}) {
  const need13 = (ok, code) => {
    if (!ok) throw Error(code);
  };
  need13(typeof tenantId === "string" && /^[A-Za-z0-9_-]{1,80}$/.test(tenantId) && !tenantId.includes("__UNBOUND"), "baileys_inbound_tenant");
  need13(Number.isSafeInteger(maxMessages) && maxMessages >= 1 && maxMessages <= 500 && Number.isSafeInteger(maxTextChars) && maxTextChars >= 1 && maxTextChars <= 4e3, "baileys_inbound_limits");
  const object8 = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const messageId = (v) => typeof v === "string" && /^[A-Za-z0-9_.:-]{1,160}$/.test(v);
  const wrappers = [
    "ephemeralMessage",
    "viewOnceMessage",
    "documentWithCaptionMessage",
    "viewOnceMessageV2",
    "viewOnceMessageV2Extension",
    "associatedChildMessage"
  ];
  function jid4(value) {
    if (typeof value !== "string") return null;
    const m = /^([0-9]+)(?::[0-9]{1,5})?@(s\.whatsapp\.net|lid)$/.exec(value);
    if (!m || m[2] === "s.whatsapp.net" && !/^[0-9]{8,15}$/.test(m[1]) || m[2] === "lid" && !/^[0-9]{6,20}$/.test(m[1])) return null;
    return m[2] === "lid" ? { sender: m[1] + "@lid", kind: "lid", phone: null } : { sender: m[1], kind: "pn", phone: m[1] };
  }
  function identity(key) {
    const primary = jid4(key?.remoteJid);
    if (!primary) return null;
    const alternative = key.remoteJidAlt === void 0 || key.remoteJidAlt === null ? null : jid4(key.remoteJidAlt);
    if (key.remoteJidAlt != null && !alternative) return null;
    if (alternative && primary.kind === alternative.kind && primary.sender !== alternative.sender) return null;
    return { sender: primary.sender, phone: primary.phone || (alternative?.kind === "pn" ? alternative.phone : null) };
  }
  function profile(value) {
    if (typeof value !== "string") return null;
    const name = value.normalize("NFC").replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, "").replace(/\s+/gu, " ").trim();
    return name && [...name].length <= 120 ? name : null;
  }
  function timestamp(value) {
    if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0 ? value : null;
    if (typeof value === "bigint") return value >= 0n && value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : null;
    if (typeof value === "string" && /^[0-9]{1,16}$/.test(value)) {
      const n = Number(value);
      return Number.isSafeInteger(n) ? n : null;
    }
    if (object8(value) && Number.isInteger(value.low) && Number.isInteger(value.high) && value.low >= -2147483648 && value.low <= 4294967295 && value.high >= 0 && value.high <= 2097151) {
      const n = value.high * 4294967296 + (value.low >>> 0);
      return Number.isSafeInteger(n) ? n : null;
    }
    return null;
  }
  function unwrap(content) {
    if (!object8(content)) return null;
    for (let i = 0; i < 5; i++) {
      if (content.editedMessage || content.groupStatusMessage || content.groupStatusMessageV2) return null;
      const found = wrappers.filter((k) => content[k] != null);
      if (found.length > 1) return null;
      if (!found.length) return content;
      const inner = content[found[0]];
      if (!object8(inner) || !object8(inner.message)) return null;
      content = inner.message;
    }
    return wrappers.some((k) => content[k] != null) ? null : content;
  }
  function contentOf(raw) {
    const content = unwrap(raw);
    if (!content) return { ignored: "invalid_or_deep_wrapper" };
    const supported = [
      "conversation",
      "extendedTextMessage",
      "imageMessage",
      "stickerMessage",
      "locationMessage",
      "liveLocationMessage",
      "audioMessage",
      "videoMessage",
      "documentMessage"
    ];
    const keys = supported.filter((k) => content[k] != null);
    if (keys.length > 1) return { ignored: "ambiguous_message_content" };
    if (!keys.length) return { ignored: "non_consumer_message" };
    const kind = keys[0], data = content[kind];
    if (kind === "conversation" || kind === "extendedTextMessage") {
      const text2 = kind === "conversation" ? data : data?.text;
      if (typeof text2 !== "string" || !text2.trim() || text2.length > maxTextChars) return { ignored: "invalid_text" };
      return { type: "text", text: text2, hasMedia: false };
    }
    if (kind === "locationMessage" || kind === "liveLocationMessage") {
      const latitude = data?.degreesLatitude, longitude = data?.degreesLongitude;
      if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 || !Number.isFinite(longitude) || longitude < -180 || longitude > 180) return { ignored: "invalid_location" };
      return { type: "location", text: "", hasMedia: false, location: { latitude, longitude } };
    }
    const mime = data?.mimetype;
    if (["audioMessage", "videoMessage", "documentMessage"].includes(kind)) return { unsupported: kind, mime: typeof mime === "string" ? mime : null };
    if (!["image/png", "image/jpeg", "image/webp"].includes(mime)) return { unsupported: kind, mime: typeof mime === "string" ? mime : null };
    const text = data.caption === void 0 ? "" : data.caption;
    if (typeof text !== "string" || text.length > maxTextChars) return { ignored: "invalid_caption" };
    return { type: "media", text, hasMedia: true, mime };
  }
  function projectUpsert(event) {
    need13(object8(event) && Array.isArray(event.messages) && event.messages.length <= maxMessages, "baileys_upsert_shape");
    const ignored = [], unsupported = [], envelopes = [], seen = /* @__PURE__ */ new Map();
    if (event.type !== "notify") return { envelopes, unsupported, ignored: event.messages.map((_, index) => ({ index, reason: "historical_or_non_notify" })) };
    for (const [index, raw] of event.messages.entries()) {
      if (!object8(raw) || !object8(raw.key)) {
        ignored.push({ index, reason: "invalid_message" });
        continue;
      }
      if (raw.key.fromMe === true) {
        ignored.push({ index, reason: "from_me" });
        continue;
      }
      const person = identity(raw.key);
      if (!person) {
        ignored.push({ index, reason: "non_individual_or_ambiguous_identity" });
        continue;
      }
      if (!messageId(raw.key.id)) {
        ignored.push({ index, reason: "invalid_message_id" });
        continue;
      }
      const message = contentOf(raw.message);
      if (message.ignored) {
        ignored.push({ index, reason: message.ignored });
        continue;
      }
      if (message.unsupported) {
        unsupported.push({
          index,
          message_id: raw.key.id,
          sender: person.sender,
          kind: message.unsupported,
          ...typeof message.mime === "string" && message.mime.length <= 100 ? { mime_type: message.mime } : {}
        });
        continue;
      }
      const name = profile(raw.pushName);
      const envelope = {
        tenantId,
        channel: "whatsapp",
        provider: "baileys",
        from: person.sender,
        name: name || "",
        contact_display_name: name,
        ...person.phone ? { verified_phone: person.phone } : {},
        ...message.location ? { location: message.location } : {},
        message: { id: raw.key.id, type: message.type, text: message.text, timestamp: timestamp(raw.messageTimestamp) },
        context: { isGroup: false, hasMedia: message.hasMedia },
        raw: {},
        ...message.hasMedia ? { media_reservation: { id: raw.key.id, mime_type: message.mime } } : {}
      };
      const key = JSON.stringify([envelope.from, envelope.message.id]), serialized = JSON.stringify(envelope), previous = seen.get(key);
      if (previous !== void 0) {
        need13(previous === serialized, "baileys_message_identity_conflict");
        ignored.push({ index, reason: "duplicate_in_upsert" });
        continue;
      }
      seen.set(key, serialized);
      envelopes.push(envelope);
    }
    return { envelopes, ignored, unsupported };
  }
  return { projectUpsert, contract: {
    provider: "baileys",
    event: "messages.upsert",
    accepted_event_type: "notify",
    tenant_source: "host_configuration",
    profile_source: "authenticated_socket_pushName",
    phone_source: "authenticated_key_PN_only",
    media: "reservation_before_download",
    durable_dedup: false,
    business_logic: false
  } };
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c454/original-lease-preparation/baileys-inbound-host-r04.mjs
function createBaileysInboundHost(options = {}) {
  const requestedEnabled = options?.enabled;
  const enabled = requestedEnabled === void 0 ? false : requestedEnabled;
  const need13 = (ok, code) => {
    if (!ok) throw Error(code);
  };
  const object8 = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
  const accountJid = (value) => {
    if (typeof value !== "string") return null;
    const match = /^([0-9]{6,20})(?::[0-9]{1,5})?@(s\.whatsapp\.net|lid)$/.exec(value);
    if (!match || match[2] === "s.whatsapp.net" && !/^[0-9]{8,15}$/.test(match[1])) return null;
    return match[1] + "@" + match[2];
  };
  need13(typeof enabled === "boolean", "baileys_host_enabled");
  if (!enabled) return {
    attachSocket: () => ({ attached: false, reason: "off" }),
    detachSocket() {
    },
    whenIdle: async () => ({ status: "off", outcomes: [] }),
    getStatus: () => ({ enabled: false, attached: false, open: false, generation: 0, pending_events: 0, queued_peers: 0, last_status: "off" }),
    contract: {
      enabled_by_default: false,
      callbacks_are_http_body: false,
      native_authority_accredited: false,
      local_durable_dedup: false,
      peer_serialization: true,
      commercial_logic: false,
      private_original_lease_lifecycle: true,
      original_custody_accredited: false
    }
  };
  const {
    tenantId,
    expectedAccountJid,
    callbacks = {},
    nowMs = () => Date.now(),
    maxFutureSkewSeconds = 60,
    maxPendingEvents = 20,
    privateOriginalLeaseIssuer
  } = options;
  const expected = accountJid(expectedAccountJid), projector = enabled ? createBaileysInboundProjector({ tenantId }) : null;
  const names = ["enqueue", "reserveMedia", "downloadOriginal", "validateReservation", "completeMedia", "recordUnsupported"];
  if (enabled) need13(expected && typeof nowMs === "function" && names.every((key) => typeof callbacks[key] === "function") && Number.isSafeInteger(maxFutureSkewSeconds) && maxFutureSkewSeconds >= 0 && maxFutureSkewSeconds <= 300 && Number.isSafeInteger(maxPendingEvents) && maxPendingEvents >= 1 && maxPendingEvents <= 100, "baileys_host_bindings_unbound");
  const fixed = Object.fromEntries(names.map((name) => [name, callbacks[name]]));
  const registerOriginal = privateOriginalLeaseIssuer?.registerOriginal;
  const invalidateOriginalLeases = privateOriginalLeaseIssuer?.invalidateAll;
  need13(typeof registerOriginal === "function" && typeof invalidateOriginalLeases === "function", "baileys_host_private_original_lease_required");
  let current = null, generation = 0, lastResult = { status: enabled ? "unbound" : "off", outcomes: [] };
  const pending = /* @__PURE__ */ new Set(), peerQueues = /* @__PURE__ */ new Map();
  const freeze = (value) => {
    if (value && typeof value === "object" && !ArrayBuffer.isView(value)) {
      for (const child of Object.values(value)) freeze(child);
      Object.freeze(value);
    }
    return value;
  };
  const copy2 = (value) => structuredClone(value);
  const keySignature = (key) => object8(key) ? JSON.stringify(Object.keys(key).sort().map((name) => [name, key[name]])) : null;
  function providerTimestamp(value) {
    if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0 ? value : null;
    if (typeof value === "bigint") return value >= 0n && value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : null;
    if (typeof value === "string" && /^[0-9]{1,16}$/.test(value)) {
      const n = Number(value);
      return Number.isSafeInteger(n) ? n : null;
    }
    if (object8(value) && Number.isInteger(value.low) && Number.isInteger(value.high) && value.low >= -2147483648 && value.low <= 4294967295 && value.high >= 0 && value.high <= 2097151) {
      const n = value.high * 4294967296 + (value.low >>> 0);
      return Number.isSafeInteger(n) ? n : null;
    }
    return null;
  }
  function timeSeconds() {
    const value = nowMs();
    need13(Number.isFinite(value) && value >= 0, "baileys_host_clock");
    return Math.floor(value / 1e3);
  }
  function gate(binding, epoch) {
    return current === binding && binding.open && binding.epoch === epoch && binding.fence !== null && accountJid(binding.socket.user?.id) === expected;
  }
  function sourceFence(binding, epoch, envelope) {
    if (!gate(binding, epoch)) return "socket_or_account_changed";
    const timestamp = envelope.message.timestamp;
    if (!Number.isSafeInteger(timestamp) || timestamp < binding.fence) return "before_connection_fence";
    if (timestamp > timeSeconds() + maxFutureSkewSeconds) return "future_provider_timestamp";
    return null;
  }
  function receiptMatches(receipt, source, { reservationId = null, requireReservation = false } = {}) {
    return object8(receipt) && receipt.accepted === true && receipt.durable === true && receipt.tenantId === source.tenantId && receipt.sender === source.sender && receipt.message_id === source.message_id && receipt.account_jid === source.account_jid && keySignature(receipt.source_key) === keySignature(source.key) && (!requireReservation || typeof receipt.reservation_id === "string" && receipt.reservation_id.length > 0 && receipt.reservation_id.length <= 200) && (reservationId === null || receipt.reservation_id === reservationId);
  }
  function sourceFor(binding, envelope, original) {
    const retained = copy2(original), key = freeze(copy2(retained.key));
    retained.key = key;
    freeze(retained);
    return { original: retained, source: freeze({
      tenantId,
      account_jid: expected,
      sender: envelope.from,
      message_id: envelope.message.id,
      key
    }) };
  }
  async function processEnvelope(binding, epoch, envelope, original) {
    const outcome = { message_id: envelope.message.id, sender: envelope.from, status: "deferred" };
    const stale = () => sourceFence(binding, epoch, envelope);
    const retained = sourceFor(binding, envelope, original), source = retained.source;
    let reason = stale();
    if (reason) return { ...outcome, reason };
    if (!envelope.media_reservation) {
      const receipt2 = await fixed.enqueue({ envelope: copy2(envelope), source });
      return receiptMatches(receipt2, source) ? { ...outcome, status: "accepted" } : { ...outcome, reason: "durable_enqueue_not_accredited" };
    }
    const reserved = await fixed.reserveMedia({ envelope: copy2(envelope), source });
    if (!receiptMatches(reserved, source, { requireReservation: true })) return { ...outcome, reason: "durable_reservation_not_accredited" };
    const reservation = freeze(copy2(reserved));
    reason = stale();
    if (reason) return { ...outcome, reason };
    const unregister = registerOriginal({
      originalMessage: retained.original,
      source,
      reservation,
      socket: binding.socket,
      socketEpoch: epoch,
      isCurrent: () => sourceFence(binding, epoch, envelope) === null
    });
    if (typeof unregister !== "function") {
      invalidateOriginalLeases();
      throw Error("baileys_host_private_original_lease_unregister");
    }
    let downloaded;
    try {
      downloaded = await fixed.downloadOriginal({ originalMessage: retained.original, source, reservation });
    } finally {
      unregister();
    }
    reason = stale();
    if (reason) return { ...outcome, reason };
    const mime = envelope.media_reservation.mime_type;
    if (!object8(downloaded) || downloaded.mime_type !== mime || !(downloaded.bytes instanceof Uint8Array) || downloaded.bytes.byteLength === 0 || downloaded.bytes.byteLength > 8 * 1024 * 1024) return { ...outcome, reason: "download_shape_invalid" };
    const refreshed = await fixed.validateReservation({ source, reservation });
    if (!receiptMatches(refreshed, source, { requireReservation: true, reservationId: reservation.reservation_id })) return { ...outcome, reason: "reservation_revalidation_failed" };
    reason = stale();
    if (reason) return { ...outcome, reason };
    const receipt = await fixed.completeMedia({
      envelope: copy2(envelope),
      source,
      reservation: freeze(copy2(refreshed)),
      media: { mime_type: mime, bytes: Uint8Array.from(downloaded.bytes) }
    });
    return receiptMatches(receipt, source, { requireReservation: true, reservationId: reservation.reservation_id }) ? { ...outcome, status: "accepted" } : { ...outcome, reason: "durable_completion_not_accredited" };
  }
  async function executeJob(binding, epoch, job) {
    if (job.envelope) {
      const envelope2 = job.envelope;
      try {
        return await processEnvelope(binding, epoch, envelope2, job.original);
      } catch {
        return { message_id: envelope2.message.id, sender: envelope2.from, status: "deferred", reason: "private_callback_or_source_failed" };
      }
    }
    const record2 = job.record, raw = job.original, envelope = { from: record2.sender, message: {
      id: record2.message_id,
      timestamp: providerTimestamp(raw?.messageTimestamp)
    } };
    const outcome = { message_id: record2.message_id, sender: record2.sender, status: "deferred", kind: record2.kind };
    try {
      const reason = sourceFence(binding, epoch, envelope);
      if (reason) return { ...outcome, reason };
      const { source } = sourceFor(binding, envelope, raw);
      const receipt = await fixed.recordUnsupported({ record: copy2(record2), source });
      return receiptMatches(receipt, source) ? { ...outcome, status: "unsupported_recorded" } : { ...outcome, reason: "durable_unsupported_not_accredited" };
    } catch {
      return { ...outcome, reason: "private_callback_or_source_failed" };
    }
  }
  function schedulePeerJob(peer, execute) {
    const previous = peerQueues.get(peer) || Promise.resolve();
    const task = previous.then(execute);
    const tail = task.then(() => void 0, () => void 0);
    peerQueues.set(peer, tail);
    void tail.then(() => {
      if (peerQueues.get(peer) === tail) peerQueues.delete(peer);
    });
    return task;
  }
  async function processUpsert(binding, epoch, event) {
    if (!gate(binding, epoch)) return { status: "filtered", reason: "socket_not_current_open", outcomes: [] };
    let projected;
    try {
      projected = projector.projectUpsert(event);
    } catch {
      return { status: "rejected", reason: "provider_event_invalid", outcomes: [] };
    }
    const excluded = new Set([...projected.ignored, ...projected.unsupported].map((row) => row.index));
    const originals = event.messages.map((original, index) => ({ original, index })).filter((row) => !excluded.has(row.index));
    const jobs = [
      ...projected.envelopes.map((envelope, index) => ({ ...originals[index], envelope })),
      ...projected.unsupported.map((record2) => ({ index: record2.index, original: event.messages[record2.index], record: record2 }))
    ].sort((a, b) => a.index - b.index);
    const scheduled = jobs.map((job) => schedulePeerJob(
      job.envelope?.from || job.record.sender,
      () => executeJob(binding, epoch, job)
    ));
    const outcomes = await Promise.all(scheduled);
    return { status: "processed", outcomes, ignored: projected.ignored };
  }
  function invalidateBinding(binding) {
    binding.open = false;
    binding.fence = null;
    const invalidatedEpoch = ++binding.epoch;
    invalidateOriginalLeases();
    return invalidatedEpoch;
  }
  function detachSocket() {
    const previous = current;
    current = null;
    if (previous) {
      invalidateBinding(previous);
      previous.socket.ev.off("connection.update", previous.onConnection);
      previous.socket.ev.off("messages.upsert", previous.onMessages);
    } else invalidateOriginalLeases();
  }
  function attachSocket(socket) {
    detachSocket();
    need13(object8(socket) && object8(socket.ev) && typeof socket.ev.on === "function" && typeof socket.ev.off === "function", "baileys_host_socket_shape");
    const binding = { socket, generation: ++generation, open: false, fence: null, epoch: 0 };
    binding.onConnection = (update) => {
      if (current !== binding || !object8(update)) return;
      if (update.connection === "close" || update.connection === "connecting") {
        invalidateBinding(binding);
        return;
      }
      if (accountJid(socket.user?.id) !== expected) {
        invalidateBinding(binding);
        lastResult = { status: "filtered", reason: "account_mismatch", outcomes: [] };
        return;
      }
      if (update.connection === "open" && !binding.open) {
        const openingEpoch = invalidateBinding(binding);
        if (current !== binding || binding.epoch !== openingEpoch) return;
        try {
          const fence = timeSeconds();
          if (current !== binding || binding.epoch !== openingEpoch) return;
          if (accountJid(socket.user?.id) !== expected) {
            invalidateBinding(binding);
            lastResult = { status: "filtered", reason: "account_mismatch", outcomes: [] };
            return;
          }
          binding.fence = fence;
          binding.open = true;
        } catch {
          if (current === binding && binding.epoch === openingEpoch) {
            invalidateBinding(binding);
            lastResult = { status: "rejected", reason: "host_clock_invalid", outcomes: [] };
          }
        }
      }
    };
    binding.onMessages = (event) => {
      const epoch = binding.epoch;
      if (!gate(binding, epoch)) {
        lastResult = { status: "filtered", reason: "socket_not_current_open", outcomes: [] };
        return;
      }
      if (pending.size >= maxPendingEvents) {
        lastResult = { status: "deferred", reason: "host_capacity_no_admission", outcomes: [] };
        return;
      }
      let retainedEvent;
      try {
        retainedEvent = copy2(event);
      } catch {
        lastResult = { status: "rejected", reason: "provider_event_not_cloneable", outcomes: [] };
        return;
      }
      const task = processUpsert(binding, epoch, retainedEvent).then((result) => {
        lastResult = result;
        return result;
      }, () => {
        const result = { status: "rejected", reason: "host_event_failed", outcomes: [] };
        lastResult = result;
        return result;
      });
      pending.add(task);
      void task.finally(() => pending.delete(task));
    };
    current = binding;
    socket.ev.on("connection.update", binding.onConnection);
    socket.ev.on("messages.upsert", binding.onMessages);
    return { attached: true, generation: binding.generation };
  }
  return {
    attachSocket,
    detachSocket,
    whenIdle: async () => {
      await Promise.all([...pending]);
      return copy2(lastResult);
    },
    getStatus: () => ({
      enabled,
      attached: current !== null,
      open: current !== null && gate(current, current.epoch),
      generation,
      pending_events: pending.size,
      queued_peers: peerQueues.size,
      last_status: lastResult.status
    }),
    contract: {
      enabled_by_default: false,
      callbacks_are_http_body: false,
      native_authority_accredited: false,
      local_durable_dedup: false,
      peer_serialization: true,
      commercial_logic: false,
      private_original_lease_lifecycle: true,
      original_custody_accredited: false
    }
  };
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c460/preparation/native-media-host-callbacks-r01.mjs
var need8 = (condition, code) => {
  if (!condition) throw Error(code);
};
var copy = (value) => structuredClone(value);
var canonical4 = (value) => {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonical4).join(",") + "]";
  return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + canonical4(value[key])).join(",") + "}";
};
var same4 = (a, b) => canonical4(a) === canonical4(b);
var hash = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");
function createNativeMediaHostCallbacks(options = {}) {
  if (options.enabled !== true) return Object.freeze({
    callbacks: Object.freeze(Object.fromEntries(["enqueue", "reserveMedia", "downloadOriginal", "validateReservation", "completeMedia", "recordUnsupported"].map((key) => [key, async () => ({ accepted: false, durable: false, status: "OFF" })]))),
    invalidateAll: () => Promise.resolve(),
    close: async () => {
    },
    inspectMetadata: () => ({ enabled: false, status: "OFF", active: 0 })
  });
  const cfg = copy(options.config), readCredential = options.readCredential, privateCallbacks = options.callbacks;
  const requests = options.requestHTTPS ?? {}, readClock = options.readRealClock ?? Date.now;
  const maxActive = options.maxActive ?? 4, retentionMs = options.retentionMs ?? 6e4;
  need8(cfg?.https_origin === "https://n8n.srv1547104.hstgr.cloud" && typeof readCredential === "function" && typeof readClock === "function" && privateCallbacks && ["enqueue", "downloadOriginal", "recordUnsupported"].every((k) => typeof privateCallbacks[k] === "function") && Number.isSafeInteger(maxActive) && maxActive >= 1 && maxActive <= 20 && Number.isSafeInteger(retentionMs) && retentionMs >= 1 && retentionMs <= 6e4, "C460_HOST_PRIVATE_CONFIGURATION");
  const fixed = Object.fromEntries(["enqueue", "downloadOriginal", "recordUnsupported"].map((k) => [k, privateCallbacks[k]]));
  const nativeRequests = Object.fromEntries(["reserve", "validate", "complete"].map((k) => [k, requests[k]]));
  need8(Object.values(nativeRequests).every((f) => f === void 0 || typeof f === "function"), "C460_HOST_PRIVATE_REQUESTS");
  const { scope, ...reserveConfig } = cfg;
  const active = /* @__PURE__ */ new Set(), bySource = /* @__PURE__ */ new WeakMap(), consumed = /* @__PURE__ */ new WeakSet();
  let closed = false, generation = 0;
  const now = () => {
    const at = readClock();
    need8(Number.isSafeInteger(at) && at >= 0, "C460_HOST_CLOCK");
    return at;
  };
  const current = (a) => !closed && a.active && a.generation === generation && bySource.get(a.source) === a && now() >= a.started && now() < a.deadline;
  const check = (a) => need8(current(a), "C460_HOST_ORIGINAL_EXPIRED_OR_INVALIDATED");
  const mark = (a) => {
    if (!a.active) return [];
    a.active = false;
    active.delete(a);
    bySource.delete(a.source);
    clearTimeout(a.timer);
    const clients = [...a.clients];
    a.clients.clear();
    return clients;
  };
  const closeClients = (clients) => Promise.allSettled(clients.map((client) => {
    try {
      return client.close();
    } catch {
      return Promise.reject(Error("C460_PRIVATE_CLOSE_FAILED"));
    }
  }));
  const retire = (a) => closeClients(mark(a));
  const invalidateAll = () => {
    generation++;
    const clients = [];
    for (const a of [...active]) clients.push(...mark(a));
    return closeClients(clients);
  };
  const find = (source, phase) => {
    const a = bySource.get(source);
    need8(a && a.phase === phase, "C460_HOST_ORIGINAL_PHASE");
    check(a);
    return a;
  };
  async function call(a, phase, make, method, args) {
    check(a);
    a.phase = phase;
    let client;
    try {
      client = make();
      a.clients.add(client);
      check(a);
      const result = await client[method](args);
      check(a);
      return result;
    } finally {
      if (client) {
        a.clients.delete(client);
        await client.close();
      }
    }
  }
  const clientOptions = (a, kind, config) => ({
    enabled: true,
    config,
    readCredential,
    readRealClock: readClock,
    timeoutMs: Math.max(1, a.deadline - now()),
    ...nativeRequests[kind] ? { requestHTTPS: nativeRequests[kind] } : {}
  });
  const callbacks = {
    enqueue: (input) => {
      need8(!closed, "C460_HOST_CLOSED");
      return fixed.enqueue(input);
    },
    recordUnsupported: (input) => {
      need8(!closed, "C460_HOST_CLOSED");
      return fixed.recordUnsupported(input);
    },
    async reserveMedia({ envelope, source }) {
      need8(!closed && source && typeof source === "object" && !consumed.has(source) && active.size < maxActive, "C460_HOST_RESERVE_UNAVAILABLE");
      consumed.add(source);
      const started = now(), a = {
        active: true,
        generation,
        source,
        envelope: copy(envelope),
        receipt: null,
        phase: "starting",
        started,
        deadline: started + retentionMs,
        clients: /* @__PURE__ */ new Set(),
        timer: null,
        digest: null,
        mime: null
      };
      bySource.set(source, a);
      active.add(a);
      a.timer = setTimeout(() => {
        void retire(a);
      }, retentionMs);
      try {
        const receipt = await call(
          a,
          "reserving",
          () => createNativeMediaReservationClient(clientOptions(a, "reserve", reserveConfig)),
          "reserveMedia",
          { envelope: a.envelope, source }
        );
        a.receipt = copy(receipt);
        a.phase = "reserved";
        return receipt;
      } catch {
        await retire(a);
        throw Error("C460_HOST_RESERVATION_UNCONFIRMED");
      }
    },
    async downloadOriginal(args) {
      const a = find(args.source, "reserved");
      need8(same4(args.reservation, a.receipt), "C460_HOST_RESERVATION_CHANGED");
      a.phase = "downloading";
      try {
        const media = await fixed.downloadOriginal(args);
        check(a);
        need8(media?.bytes instanceof Uint8Array && media.bytes.byteLength > 0 && media.bytes.byteLength <= 8 * 1024 * 1024 && media.mime_type === a.envelope.media_reservation.mime_type, "C460_HOST_ORIGINAL_IMAGE");
        a.digest = hash(media.bytes);
        a.mime = media.mime_type;
        a.phase = "downloaded";
        return media;
      } catch {
        await retire(a);
        throw Error("C460_HOST_DOWNLOAD_UNCONFIRMED");
      }
    },
    async validateReservation({ source, reservation }) {
      const a = find(source, "downloaded");
      need8(same4(reservation, a.receipt), "C460_HOST_RESERVATION_CHANGED");
      try {
        const receipt = await call(
          a,
          "validating",
          () => createNativeMediaValidationClient(clientOptions(a, "validate", reserveConfig)),
          "validateReservation",
          { envelope: a.envelope, source, reservation }
        );
        need8(same4(receipt, a.receipt), "C460_HOST_REVALIDATION_CHANGED_ORIGINAL");
        a.phase = "validated";
        return receipt;
      } catch {
        await retire(a);
        throw Error("C460_HOST_REVALIDATION_UNCONFIRMED");
      }
    },
    async completeMedia({ envelope, source, reservation, media }) {
      const a = find(source, "validated");
      try {
        need8(same4(envelope, a.envelope) && same4(reservation, a.receipt) && media?.mime_type === a.mime && media.bytes instanceof Uint8Array && hash(media.bytes) === a.digest, "C460_HOST_COMPLETION_ORIGINAL_CHANGED");
        return await call(
          a,
          "completing",
          () => createNativeMediaCompletionClient(clientOptions(a, "complete", cfg)),
          "completeMedia",
          { envelope: a.envelope, source, reservation, file: media.bytes }
        );
      } finally {
        await retire(a);
      }
    }
  };
  return Object.freeze({
    callbacks: Object.freeze(callbacks),
    invalidateAll,
    close: async () => {
      closed = true;
      await invalidateAll();
    },
    inspectMetadata: () => ({
      enabled: true,
      status: closed ? "CLOSED" : "PRIVATE_BINDING_PREPARED",
      active: active.size,
      generation,
      max_active: maxActive,
      technical_retention_ms: retentionMs,
      business_authority: false,
      native_live_bound: false
    })
  });
}
function createNativeMediaInboundHost(options = {}) {
  if (options.enabled !== true) return createBaileysInboundHost();
  const issuer = options.privateOriginalLeaseIssuer;
  need8(issuer && typeof issuer.registerOriginal === "function" && typeof issuer.invalidateAll === "function", "C460_HOST_PRIVATE_ISSUER");
  const registerOriginal = issuer.registerOriginal, invalidateLeases = issuer.invalidateAll;
  const controller = createNativeMediaHostCallbacks(options);
  let host;
  try {
    host = createBaileysInboundHost({
      enabled: true,
      tenantId: options.config.tenant,
      expectedAccountJid: options.config.account_jid,
      callbacks: controller.callbacks,
      nowMs: options.readRealClock ?? Date.now,
      maxPendingEvents: options.maxPendingEvents ?? 20,
      privateOriginalLeaseIssuer: {
        registerOriginal,
        invalidateAll() {
          void controller.invalidateAll();
          invalidateLeases();
        }
      }
    });
  } catch (caught) {
    void controller.close();
    throw caught;
  }
  return Object.freeze({
    ...host,
    close: async () => {
      host.detachSocket();
      await controller.close();
      await host.whenIdle();
    },
    inspectMediaMetadata: controller.inspectMetadata
  });
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c462/preparation/native-media-host-owned-downloads-r01.mjs
var need9 = (ok, code) => {
  if (!ok) throw Error(code);
};
var object7 = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
function createOwnedDownloadPool(options) {
  const { config, installed, downloadTimeoutMs = 15e3, cleanupTimeoutMs = 1e3, maxActive = 4 } = options;
  need9(
    object7(installed) && typeof installed.Agent === "function" && typeof installed.getMediaKeys === "function" && typeof installed.fetch === "function" && Number.isSafeInteger(maxActive) && maxActive >= 1 && maxActive <= 20 && Number.isSafeInteger(downloadTimeoutMs) && downloadTimeoutMs >= 1 && downloadTimeoutMs <= 3e4 && Number.isSafeInteger(cleanupTimeoutMs) && cleanupTimeoutMs >= 1 && cleanupTimeoutMs <= 2e3,
    "C462_PRIVATE_DOWNLOAD_CONFIGURATION"
  );
  const Agent = installed.Agent, getMediaKeys = installed.getMediaKeys, privateFetch = installed.fetch;
  const registry = createPrivateOriginalMediaLeaseRegistry({
    enabled: true,
    tenantId: config.tenant,
    expectedAccountJid: config.account_jid,
    maxActiveLeases: maxActive
  });
  const records = /* @__PURE__ */ new Set(), consumed = /* @__PURE__ */ new WeakSet();
  let closed = false, blocked = false, generation = 0;
  function dispose(record2) {
    if (record2.disposal) return record2.disposal;
    let resolve;
    record2.disposal = new Promise((done) => {
      resolve = done;
    });
    const invoke = (callback) => {
      try {
        return Promise.resolve(callback());
      } catch {
        return Promise.reject(Error("C462_PRIVATE_CLOSE_FAILED"));
      }
    };
    const pending = [invoke(() => record2.downloader?.close()), invoke(() => record2.factory?.close())];
    void Promise.allSettled(pending).then((results) => {
      let confirmed = false;
      try {
        const download = record2.downloader?.inspectMetadata(), dispatch = record2.factory?.inspectMetadata();
        confirmed = results.every((result) => result.status === "fulfilled") && (!download || download.closed === true && download.active === false) && (!dispatch || dispatch.closed === true && dispatch.blocked === false && dispatch.active_dispatchers === 0);
      } catch {
      }
      record2.closeConfirmed = confirmed;
      if (confirmed) records.delete(record2);
      else blocked = true;
      resolve(confirmed);
    });
    return record2.disposal;
  }
  function invalidateAll() {
    generation++;
    registry.issuer.invalidateAll();
    for (const record2 of [...records]) void dispose(record2);
  }
  async function downloadOriginal(args = {}) {
    need9(!closed && !blocked && records.size < maxActive, "C462_DOWNLOAD_OWNER_UNAVAILABLE");
    const epoch = generation, source = args.source;
    need9(object7(source) && !consumed.has(source), "C462_ORIGINAL_ALREADY_CONSUMED");
    const descriptor = { originalMessage: args.originalMessage, source, reservation: args.reservation };
    const lease = registry.acquireOriginalLease(descriptor);
    need9(lease.isCurrent() === true && !closed && !blocked && epoch === generation, "C462_ORIGINAL_NOT_CURRENT");
    consumed.add(source);
    const record2 = { factory: null, downloader: null, disposal: null, closeConfirmed: false };
    records.add(record2);
    let media;
    try {
      record2.factory = createOwnedMediaDispatcherFactory({ enabled: true, installed: { Agent }, cleanupTimeoutMs });
      record2.downloader = createBaileysOriginalImageDownloader({
        enabled: true,
        tenantId: config.tenant,
        expectedAccountJid: config.account_jid,
        installed: { getMediaKeys },
        timeoutMs: downloadTimeoutMs,
        cleanupTimeoutMs,
        privateBindings: {
          crypto: nodeCrypto,
          fetch: privateFetch,
          acquireOriginalLease: registry.acquireOriginalLease,
          openOwnedDispatcher: record2.factory.openOwnedDispatcher
        }
      });
      need9(!closed && !blocked && epoch === generation, "C462_ORIGINAL_NOT_CURRENT");
      media = await record2.downloader.downloadOriginal(descriptor);
    } finally {
      await dispose(record2);
    }
    need9(record2.closeConfirmed && !closed && !blocked && epoch === generation && lease.isCurrent() === true && epoch === generation && !closed && !blocked, "C462_ORIGINAL_OR_CLOSE_UNCONFIRMED");
    return media;
  }
  return Object.freeze({
    issuer: Object.freeze({ registerOriginal: registry.issuer.registerOriginal, invalidateAll }),
    downloadOriginal,
    async close() {
      closed = true;
      invalidateAll();
      await Promise.all([...records].map(dispose));
      need9(!blocked && records.size === 0, "C462_DOWNLOAD_CLOSE_UNCONFIRMED");
    },
    inspectMetadata: () => ({
      enabled: true,
      closed,
      blocked,
      generation,
      active_download_owners: records.size,
      active_original_leases: registry.inspectMetadata().active_leases,
      max_active: maxActive,
      business_authority: false,
      native_live_bound: false,
      physical_close_accredited: false
    })
  });
}
function createNativeMediaHostWithOwnedDownloads(options = {}) {
  if (options.enabled !== true) return Object.freeze({
    ...createNativeMediaInboundHost(),
    close: async () => {
    },
    inspectDownloadMetadata: () => ({ enabled: false, status: "OFF", active_download_owners: 0, active_original_leases: 0 })
  });
  const config = structuredClone(options.config), callbacks = options.callbacks;
  need9(object7(config) && object7(callbacks) && typeof callbacks.enqueue === "function" && typeof callbacks.recordUnsupported === "function", "C462_PRIVATE_HOST_CONFIGURATION");
  const pool = createOwnedDownloadPool({ ...options, config });
  let host;
  try {
    host = createNativeMediaInboundHost({
      ...options,
      config,
      privateOriginalLeaseIssuer: pool.issuer,
      callbacks: { enqueue: callbacks.enqueue, recordUnsupported: callbacks.recordUnsupported, downloadOriginal: pool.downloadOriginal }
    });
  } catch (caught) {
    void pool.close().catch(() => {
    });
    throw caught;
  }
  let closed = false, closing = null;
  return Object.freeze({
    ...host,
    attachSocket(socket) {
      need9(!closed, "C462_HOST_CLOSED");
      return host.attachSocket(socket);
    },
    close() {
      if (closing) return closing;
      closed = true;
      let resolve, reject;
      closing = new Promise((done, fail) => {
        resolve = done;
        reject = fail;
      });
      void closing.catch(() => {
      });
      const invoke = (callback) => {
        try {
          return Promise.resolve(callback());
        } catch {
          return Promise.reject(Error("C462_HOST_CLOSE_FAILED"));
        }
      };
      const pending = [invoke(() => host.close()), invoke(() => pool.close())];
      void Promise.allSettled(pending).then((results) => {
        if (results.every((result) => result.status === "fulfilled")) resolve();
        else reject(Error("C462_HOST_CLOSE_UNCONFIRMED"));
      });
      return closing;
    },
    inspectDownloadMetadata: pool.inspectMetadata
  });
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c450/mailbox-client/private-mailbox-gateway-client-r02.mjs
import { createHash as createHash2, randomUUID as randomUUID2 } from "node:crypto";
import { request as httpsRequest3 } from "node:https";
import { closeSync as closeSync2, existsSync as existsSync2, fsyncSync as fsyncSync2, mkdirSync as mkdirSync2, openSync as openSync2, readFileSync as readFileSync2, renameSync as renameSync2, unlinkSync, writeFileSync as writeFileSync2 } from "node:fs";
import { isAbsolute as isAbsolute3, join as join2 } from "node:path";

// APP POS/entregas/agente-20261028/native-n8n/direct-transport-c447/gateway/baileys-delivery-r02.mjs
import { createHash, randomUUID } from "node:crypto";
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import { isAbsolute as isAbsolute2, join } from "node:path";
var need10 = (ok, code) => {
  if (!ok) throw Error(code);
};
var hash2 = (x) => createHash("sha256").update(x).digest("hex");
var isHash = (x) => typeof x === "string" && /^[a-f0-9]{64}$/.test(x);
var ident = (x) => typeof x === "string" && /^[A-Za-z0-9_.:-]{1,200}$/.test(x);
var uuid = (x) => typeof x === "string" && /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(x);
function canonical5(x) {
  if (x === null || ["boolean", "string"].includes(typeof x)) return JSON.stringify(x);
  if (typeof x === "number" && Number.isFinite(x)) return JSON.stringify(x);
  need10(x && typeof x === "object", "baileys_json_required");
  if (Array.isArray(x)) return "[" + x.map(canonical5).join(",") + "]";
  const ds = Object.getOwnPropertyDescriptors(x);
  need10(Object.getOwnPropertySymbols(x).length === 0 && Object.values(ds).every((d) => Object.hasOwn(d, "value") && d.enumerable), "baileys_plain_json_required");
  return "{" + Object.keys(ds).sort().map((k) => JSON.stringify(k) + ":" + canonical5(ds[k].value)).join(",") + "}";
}
var clone = (x) => JSON.parse(canonical5(x));
function baileysRecipient(value) {
  need10(typeof value === "string", "baileys_recipient");
  const s = value.trim().replace(/^\+/, "").replace(/:(?:\d+)@/, "@");
  if (/^\d{6,20}@lid$/.test(s)) return { sender: s, jid: s };
  const pn = /^(\d{8,15})(?:@(?:s\.whatsapp\.net|c\.us))?$/.exec(s);
  need10(pn, "baileys_recipient");
  return { sender: pn[1], jid: pn[1] + "@s.whatsapp.net" };
}
function actionOf(raw) {
  need10(raw && typeof raw === "object" && !Array.isArray(raw), "baileys_action");
  const x = clone(raw);
  if (x.type === "text") {
    need10(Object.keys(x).every((k) => ["type", "text"].includes(k)) && typeof x.text === "string" && x.text.trim() && x.text.length <= 6e3, "baileys_text");
    return { type: "text", text: x.text };
  }
  need10(x.type === "media_binary" && Object.keys(x).every((k) => ["type", "data", "mimeType", "caption"].includes(k)) && ["image/png", "image/jpeg", "image/webp"].includes(x.mimeType) && typeof x.data === "string" && x.data.length > 0 && x.data.length <= 11184812 && x.data.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(x.data) && (x.caption === void 0 || typeof x.caption === "string" && x.caption.length <= 1024), "baileys_image");
  const bytes = Buffer.from(x.data, "base64");
  const valid = x.mimeType === "image/png" ? bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) : x.mimeType === "image/jpeg" ? bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 : bytes.toString("ascii", 0, 4) === "RIFF" && bytes.toString("ascii", 8, 12) === "WEBP";
  need10(bytes.length > 0 && bytes.length <= 8 * 1024 * 1024 && bytes.toString("base64") === x.data && valid, "baileys_image_bytes");
  return { type: "media_binary", data: x.data, mimeType: x.mimeType, ...x.caption !== void 0 ? { caption: x.caption } : {} };
}
var BaileysDeliveryTransport = class {
  #policy;
  #directory;
  #authorize;
  #messageId;
  #clock;
  #timeout;
  #authorizeTimeout;
  #count = 0;
  #socket = null;
  #listener = null;
  #pending = /* @__PURE__ */ new Map();
  #running = /* @__PURE__ */ new Map();
  #waiters = /* @__PURE__ */ new Map();
  #closed = false;
  #fault = false;
  constructor({ tenant, businessId, branchId, accountJid, directory, authorizeAction, createMessageId, readRealClock = Date.now, ackTimeoutMs = 6e4, authorizeTimeoutMs = 1e4 }) {
    need10(/^[A-Za-z0-9_-]{1,80}$/.test(tenant || "") && [businessId, branchId].every((x) => Number.isSafeInteger(x) && x > 0) && isAbsolute2(directory || "") && typeof authorizeAction === "function" && typeof createMessageId === "function" && typeof readRealClock === "function" && Number.isSafeInteger(ackTimeoutMs) && ackTimeoutMs > 0 && ackTimeoutMs <= 6e4 && Number.isSafeInteger(authorizeTimeoutMs) && authorizeTimeoutMs > 0 && authorizeTimeoutMs <= 6e4, "baileys_transport_configuration");
    const account3 = baileysRecipient(accountJid);
    need10(!account3.sender.endsWith("@lid"), "baileys_account_phone_required");
    this.#policy = { tenant, business_id: businessId, branch_id: branchId, account_jid: account3.jid };
    this.#directory = directory;
    this.#authorize = authorizeAction;
    this.#messageId = createMessageId;
    this.#clock = readRealClock;
    this.#timeout = ackTimeoutMs;
    this.#authorizeTimeout = authorizeTimeoutMs;
    mkdirSync(directory, { recursive: true, mode: 448 });
    const files = readdirSync(directory).filter((x) => /^[a-f0-9]{64}\.json$/.test(x));
    need10(files.length <= 1e4, "baileys_journal_capacity_review");
    this.#count = files.length;
    for (const name of files) {
      const row = this.#readPath(join(directory, name));
      for (const step of row.steps) this.#index(row, step);
    }
  }
  #path(id) {
    return join(this.#directory, hash2(JSON.stringify([this.#policy.tenant, this.#policy.business_id, this.#policy.branch_id, this.#policy.account_jid, id])) + ".json");
  }
  #readPath(file) {
    const row = JSON.parse(readFileSync(file, "utf8")), { checksum, ...body } = row;
    need10(checksum === hash2(canonical5(body)) && row.version === 2 && row.provider === "baileys" && ident(row.delivery_id) && ident(row.message_id) && row.tenant === this.#policy.tenant && row.business_id === this.#policy.business_id && row.branch_id === this.#policy.branch_id && row.account_jid === this.#policy.account_jid && file === this.#path(row.delivery_id) && baileysRecipient(row.sender).sender === row.sender && isHash(row.envelope_digest) && isHash(row.source_digest) && uuid(row.claim_nonce) && Number.isSafeInteger(row.created_at_ms) && row.created_at_ms > 0 && Number.isSafeInteger(row.action_count) && row.action_count > 0 && row.action_count <= 12 && ["uncertain", "sent"].includes(row.status) && Array.isArray(row.steps) && row.steps.length <= row.action_count, "baileys_journal_invalid");
    const ids = /* @__PURE__ */ new Set();
    row.steps.forEach((s, i) => {
      need10(s.index === i && ident(s.provider_message_id) && !ids.has(s.provider_message_id) && isHash(s.action_digest) && ["reserved", "accepted"].includes(s.state) && uuid(s.action_nonce) && Number.isSafeInteger(s.post_entered_at_ms) && s.post_entered_at_ms >= row.created_at_ms && Number.isSafeInteger(s.grant_expires_at_ms) && s.grant_expires_at_ms > s.post_entered_at_ms && (s.state === "accepted" ? Number.isInteger(s.ack) && s.ack >= 2 && s.ack <= 5 && Number.isSafeInteger(s.accepted_at_ms) && s.accepted_at_ms >= s.post_entered_at_ms : s.ack === null && s.accepted_at_ms === null), "baileys_journal_step");
      ids.add(s.provider_message_id);
    });
    if (row.status === "sent") need10(row.steps.length === row.action_count && row.steps.every((s) => s.state === "accepted") && canonical5(row.receipt) === canonical5(this.#receipt(row)), "baileys_journal_receipt");
    return row;
  }
  #read(id) {
    const p = this.#path(id);
    return existsSync(p) ? this.#readPath(p) : null;
  }
  #syncDirectory() {
    if (process.platform !== "win32") {
      const fd = openSync(this.#directory, "r");
      try {
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
    }
  }
  #persist(row, exclusive = false) {
    const { checksum, ...body } = row;
    row.checksum = hash2(canonical5(body));
    const destination = this.#path(row.delivery_id), target = exclusive ? destination : destination + "." + randomUUID() + ".tmp";
    const fd = openSync(target, "wx", 384);
    try {
      writeFileSync(fd, JSON.stringify(row));
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    if (!exclusive) renameSync(target, destination);
    this.#syncDirectory();
  }
  #index(row, step) {
    const old = this.#pending.get(step.provider_message_id);
    need10(!old || old.delivery_id === row.delivery_id && old.index === step.index, "baileys_provider_id_collision");
    this.#pending.set(step.provider_message_id, { delivery_id: row.delivery_id, index: step.index });
  }
  bindSocket(socket) {
    need10(!this.#closed && socket?.ev?.on && socket.ev.off && typeof socket.sendMessage === "function" && baileysRecipient(socket.user?.id).jid === this.#policy.account_jid, "baileys_socket_account_binding");
    if (this.#socket && this.#listener) this.#socket.ev.off("messages.update", this.#listener);
    for (const [id, w] of this.#waiters) {
      clearTimeout(w.timer);
      w.reject(Error("baileys_socket_replaced"));
      this.#waiters.delete(id);
    }
    this.#socket = socket;
    this.#listener = (updates) => {
      if (this.#socket !== socket || this.#closed) return;
      try {
        need10(baileysRecipient(socket.user?.id).jid === this.#policy.account_jid, "baileys_socket_account_binding");
        this.#accept(updates);
      } catch {
        this.#fault = true;
        for (const [id, w] of this.#waiters) {
          clearTimeout(w.timer);
          w.reject(Error("baileys_journal_unavailable"));
          this.#waiters.delete(id);
        }
      }
    };
    socket.ev.on("messages.update", this.#listener);
  }
  #receipt(row) {
    return {
      tenant: row.tenant,
      transport_id: "baileys:" + hash2(JSON.stringify([row.tenant, row.delivery_id])),
      provider: "baileys",
      delivery_id: row.delivery_id,
      message_id: row.message_id,
      sender: row.sender,
      action_receipts: row.steps.map((s) => ({ index: s.index, provider: "baileys", provider_message_id: s.provider_message_id, ack: s.ack }))
    };
  }
  #accept(updates) {
    if (!Array.isArray(updates)) return;
    for (const u of updates) {
      if (u?.key?.fromMe !== true || !ident(u.key.id) || !Number.isInteger(u.update?.status) || u.update.status < 2 || u.update.status > 5) continue;
      const saved = this.#pending.get(u.key.id);
      if (!saved) continue;
      const row = this.#read(saved.delivery_id), step = row?.steps[saved.index];
      need10(step?.provider_message_id === u.key.id, "baileys_private_receipt_binding");
      let recipient;
      try {
        recipient = baileysRecipient(u.key.remoteJid).sender;
      } catch {
        continue;
      }
      if (recipient !== row.sender || !step.post_entered_at_ms) continue;
      if (step.state === "accepted") continue;
      const observed = this.#clock();
      need10(Number.isSafeInteger(observed) && observed >= step.post_entered_at_ms, "baileys_ack_clock");
      step.state = "accepted";
      step.ack = u.update.status;
      step.accepted_at_ms = observed;
      if (row.steps.length === row.action_count && row.steps.every((s) => s.state === "accepted")) {
        row.status = "sent";
        row.receipt = this.#receipt(row);
      }
      this.#persist(row);
      const waiter = this.#waiters.get(u.key.id);
      if (waiter) {
        clearTimeout(waiter.timer);
        this.#waiters.delete(u.key.id);
        waiter.resolve();
      }
    }
  }
  inspect(deliveryId) {
    need10(ident(deliveryId) && !this.#fault, "baileys_journal_unavailable");
    const row = this.#read(deliveryId);
    need10(row, "baileys_delivery_missing");
    return {
      tenant: row.tenant,
      delivery_id: row.delivery_id,
      message_id: row.message_id,
      sender: row.sender,
      status: row.status,
      action_receipts: row.steps.filter((s) => s.state === "accepted").map((s) => ({ index: s.index, provider: "baileys", provider_message_id: s.provider_message_id, ack: s.ack })),
      ...row.status === "sent" ? { receipt: row.receipt } : {}
    };
  }
  // Private authenticated gateway endpoint only. This observation is never an
  // incoming webhook field and is not part of the public delivery receipt.
  observeAction({ source, descriptor, action_nonce }) {
    need10(!this.#fault && descriptor?.tenant === this.#policy.tenant && ident(descriptor.delivery_id), "baileys_private_receipt_binding");
    const row = this.#read(descriptor.delivery_id), step = row?.steps[descriptor.index];
    need10(row && step?.state === "accepted" && hash2(canonical5(source)) === row.source_digest && step.action_nonce === action_nonce, "baileys_private_receipt_unavailable");
    const expected = {
      tenant: row.tenant,
      scope: { business_id: row.business_id, branch_id: row.branch_id },
      delivery_id: row.delivery_id,
      message_id: row.message_id,
      sender: row.sender,
      recipient_jid: baileysRecipient(row.sender).jid,
      account_jid: row.account_jid,
      envelope_digest: row.envelope_digest,
      action_digest: step.action_digest,
      index: step.index,
      provider_message_id: step.provider_message_id,
      claim_nonce: row.claim_nonce
    };
    need10(canonical5(expected) === canonical5(descriptor), "baileys_private_receipt_binding");
    return {
      mode: "authenticated_baileys_ack",
      descriptor_sha256: hash2(canonical5(expected)),
      action_nonce: step.action_nonce,
      provider: "baileys",
      provider_message_id: step.provider_message_id,
      index: step.index,
      recipient_jid: expected.recipient_jid,
      account_jid: row.account_jid,
      ack: step.ack,
      observed_at_ms: step.accepted_at_ms
    };
  }
  async #authorizeBounded(input) {
    let timer;
    try {
      return await Promise.race([Promise.resolve().then(() => this.#authorize(input)), new Promise((_, reject) => {
        timer = setTimeout(() => reject(Error("baileys_private_authorization_unknown")), this.#authorizeTimeout);
      })]);
    } finally {
      clearTimeout(timer);
    }
  }
  async deliver(raw, { source, claim_nonce }) {
    need10(!this.#closed && !this.#fault && this.#socket, "baileys_transport_not_ready");
    const wire = clone(raw), policy = this.#policy;
    need10(Object.keys(wire).every((k) => ["tenant", "delivery_id", "message_id", "sender", "turn", "actions"].includes(k)) && wire.tenant === policy.tenant && ident(wire.delivery_id) && ident(wire.message_id) && Array.isArray(wire.actions) && wire.actions.length > 0 && wire.actions.length <= 12 && (wire.turn === void 0 || wire.turn?.id === wire.message_id), "baileys_delivery_envelope");
    const recipient = baileysRecipient(wire.sender), actions = wire.actions.map(actionOf);
    const sourceKeys2 = ["row_id", "workflow_id", "code_manifest_sha256", "business_id", "branch_id", "run_id", "effect_id", "request_hash", "owner_execution", "nonce"];
    need10(actions.filter((a) => a.type === "media_binary").length <= 10 && uuid(claim_nonce) && source && Object.keys(source).sort().join("|") === sourceKeys2.sort().join("|") && Number.isSafeInteger(source.row_id) && source.row_id > 0 && [source.workflow_id, source.run_id, source.owner_execution].every(ident) && [source.code_manifest_sha256, source.effect_id, source.request_hash].every(isHash) && uuid(source.nonce) && source.business_id === policy.business_id && source.branch_id === policy.branch_id, "baileys_private_source_required");
    const envelopeDigest = hash2(JSON.stringify([policy.tenant, wire.delivery_id, recipient.sender, wire.message_id, actions])), sourceDigest = hash2(canonical5(source));
    let row = this.#read(wire.delivery_id);
    if (row) {
      need10(row.envelope_digest === envelopeDigest && row.source_digest === sourceDigest && row.claim_nonce === claim_nonce, "baileys_delivery_conflict");
      if (row.status === "sent") return clone(row.receipt);
      const running = this.#running.get(wire.delivery_id);
      if (running) return running;
      throw Error("baileys_delivery_uncertain_no_resend");
    }
    need10(this.#count < 1e4, "baileys_journal_capacity_review");
    const created = this.#clock();
    need10(Number.isSafeInteger(created) && created > 0, "baileys_real_clock");
    row = {
      version: 2,
      provider: "baileys",
      ...policy,
      delivery_id: wire.delivery_id,
      message_id: wire.message_id,
      sender: recipient.sender,
      envelope_digest: envelopeDigest,
      source_digest: sourceDigest,
      claim_nonce,
      status: "uncertain",
      created_at_ms: created,
      action_count: actions.length,
      steps: []
    };
    this.#persist(row, true);
    this.#count++;
    const work = this.#send(row, actions, clone(source), recipient.jid);
    this.#running.set(wire.delivery_id, work);
    try {
      return await work;
    } finally {
      this.#running.delete(wire.delivery_id);
    }
  }
  async #send(initial, actions, source, jid4) {
    for (const [index, action] of actions.entries()) {
      const socket = this.#socket, providerId = this.#messageId();
      need10(ident(providerId) && !this.#pending.has(providerId), "baileys_message_id_required");
      const row = this.#read(initial.delivery_id);
      need10(row.steps.length === index && row.steps.every((s) => s.state === "accepted"), "baileys_action_order");
      const descriptor = {
        tenant: row.tenant,
        scope: { business_id: row.business_id, branch_id: row.branch_id },
        delivery_id: row.delivery_id,
        message_id: row.message_id,
        sender: row.sender,
        recipient_jid: jid4,
        account_jid: row.account_jid,
        envelope_digest: row.envelope_digest,
        action_digest: hash2(canonical5(action)),
        index,
        provider_message_id: providerId,
        claim_nonce: row.claim_nonce
      };
      const grant = clone(await this.#authorizeBounded({ source: clone(source), descriptor: clone(descriptor) })), at = this.#clock();
      need10(grant?.mode === "native_baileys_action_dispatch" && grant.descriptor_sha256 === hash2(canonical5(descriptor)) && uuid(grant.action_nonce) && Number.isSafeInteger(grant.issued_at_ms) && Number.isSafeInteger(grant.expires_at_ms) && grant.issued_at_ms <= at && at < grant.expires_at_ms && grant.expires_at_ms - grant.issued_at_ms <= 2e3, "baileys_private_action_grant");
      const entered = this.#clock();
      need10(Number.isSafeInteger(entered) && entered >= row.created_at_ms && entered < grant.expires_at_ms, "baileys_immediate_dispatch_fence");
      const step = {
        index,
        provider_message_id: providerId,
        action_digest: descriptor.action_digest,
        action_nonce: grant.action_nonce,
        state: "reserved",
        ack: null,
        accepted_at_ms: null,
        post_entered_at_ms: entered,
        grant_expires_at_ms: grant.expires_at_ms
      };
      row.steps.push(step);
      this.#persist(row);
      this.#index(row, step);
      need10(this.#socket === socket && !this.#closed && !this.#fault && this.#clock() < grant.expires_at_ms && baileysRecipient(socket.user?.id).jid === row.account_jid, "baileys_immediate_dispatch_fence");
      const ack = new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          this.#waiters.delete(providerId);
          reject(Error("baileys_ack_unknown"));
        }, this.#timeout);
        this.#waiters.set(providerId, { resolve, reject, timer });
      });
      const content = action.type === "text" ? { text: action.text, linkPreview: null } : { image: Buffer.from(action.data, "base64"), mimetype: action.mimeType, ...action.caption !== void 0 ? { caption: action.caption } : {} };
      try {
        Promise.resolve(socket.sendMessage(jid4, content, { messageId: providerId })).catch(() => {
          const w = this.#waiters.get(providerId);
          if (w) {
            clearTimeout(w.timer);
            this.#waiters.delete(providerId);
            w.reject(Error("baileys_provider_unknown"));
          }
        });
      } catch {
        const w = this.#waiters.get(providerId);
        if (w) {
          clearTimeout(w.timer);
          this.#waiters.delete(providerId);
          w.reject(Error("baileys_provider_unknown"));
        }
      }
      await ack;
    }
    const completed = this.inspect(initial.delivery_id);
    need10(completed.status === "sent", "baileys_delivery_uncertain_no_resend");
    return completed.receipt;
  }
  async close() {
    this.#closed = true;
    if (this.#socket && this.#listener) this.#socket.ev.off("messages.update", this.#listener);
    for (const [id, w] of this.#waiters) {
      clearTimeout(w.timer);
      w.reject(Error("baileys_transport_closed"));
      this.#waiters.delete(id);
    }
    await Promise.allSettled(this.#running.values());
  }
};

// APP POS/entregas/agente-20261028/native-n8n/native-control-c450/mailbox-client/native-mailbox-entry-controller-r03.mjs
function createNativeMailboxEntryController({ Buffer: Buffer2, sha256, canonicalJSON, config, coordinator }) {
  const fail = (code) => {
    throw Object.assign(Error(code), { code });
  };
  const need13 = (ok, code) => {
    if (!ok) fail(code);
  };
  const object8 = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const exact5 = (value, keys) => object8(value) && Object.keys(value).sort().join("|") === [...keys].sort().join("|");
  const integer5 = (value) => Number.isSafeInteger(value) && value >= 0;
  const id = (value) => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,200}$/.test(value);
  const hash4 = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  const uuid3 = (value) => typeof value === "string" && /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(value);
  const providerId = (value) => typeof value === "string" && value.length > 0 && value.length <= 512 && !/[\u0000-\u0020]/.test(value);
  need13(Buffer2?.from && typeof sha256 === "function" && typeof canonicalJSON === "function", "C448_DEPENDENCIES");
  need13(typeof coordinator?.recoverAdmission === "function", "C448_PRIVATE_SOURCE_COORDINATOR");
  const own5 = (value) => JSON.parse(canonicalJSON(value)), same6 = (a, b) => canonicalJSON(a) === canonicalJSON(b);
  const sha = (value) => {
    const out = sha256(typeof value === "string" || Buffer2.isBuffer(value) ? value : canonicalJSON(value));
    need13(hash4(out), "C448_HASH");
    return out;
  };
  const cfg = own5(config), configKeys = ["tenant", "scope", "gateway_id", "account_jid", "main_workflow_id", "main_manifest", "mailbox_workflow_id", "mailbox_manifest", "mailbox_table_id"];
  need13(exact5(cfg, configKeys) && typeof cfg.tenant === "string" && /^[A-Za-z0-9_-]{1,80}$/.test(cfg.tenant) && exact5(cfg.scope, ["business_id", "branch_id"]) && Object.values(cfg.scope).every((v) => integer5(v) && v > 0) && ["gateway_id", "main_workflow_id", "mailbox_workflow_id", "mailbox_table_id"].every((k) => id(cfg[k])) && ["main_manifest", "mailbox_manifest"].every((k) => hash4(cfg[k])) && typeof cfg.account_jid === "string" && /^\d{8,15}@s\.whatsapp\.net$/.test(cfg.account_jid), "C448_PRIVATE_CONFIG");
  const configured = !["tenant", "gateway_id", "account_jid", "main_workflow_id", "mailbox_workflow_id", "mailbox_table_id"].some((k) => cfg[k].includes("__UNBOUND"));
  const paths2 = { pull: "escalar-native-baileys-c450/mailbox/pull", action_authorize: "escalar-native-baileys-c450/mailbox/action-authorize", ack_observe: "escalar-native-baileys-c450/mailbox/ack-observe" };
  const sourceKeys2 = ["row_id", "workflow_id", "code_manifest_sha256", "business_id", "branch_id", "run_id", "effect_id", "request_hash", "owner_execution", "nonce"];
  const descriptorKeys = ["tenant", "scope", "delivery_id", "message_id", "sender", "recipient_jid", "account_jid", "envelope_digest", "action_digest", "index", "provider_message_id", "claim_nonce"];
  const deniedAuthority = { granted: false, dispatch_authority: false, can_resend: false };
  function sourceOf(raw) {
    const source = own5(raw);
    need13(exact5(source, sourceKeys2) && integer5(source.row_id) && source.row_id > 0 && source.workflow_id === cfg.main_workflow_id && source.code_manifest_sha256 === cfg.main_manifest && source.business_id === cfg.scope.business_id && source.branch_id === cfg.scope.branch_id && id(source.run_id) && id(source.owner_execution) && uuid3(source.nonce) && hash4(source.effect_id) && hash4(source.request_hash), "C448_SOURCE");
    return source;
  }
  function recipient(sender) {
    need13(typeof sender === "string", "C448_RECIPIENT");
    if (/^\d{6,20}@lid$/.test(sender)) return sender;
    need13(/^\d{8,15}$/.test(sender), "C448_RECIPIENT");
    return sender + "@s.whatsapp.net";
  }
  function validateFullDescriptor(raw) {
    const descriptor = own5(raw);
    need13(exact5(descriptor, descriptorKeys) && descriptor.tenant === cfg.tenant && same6(descriptor.scope, cfg.scope) && id(descriptor.delivery_id) && id(descriptor.message_id) && descriptor.recipient_jid === recipient(descriptor.sender) && descriptor.account_jid === cfg.account_jid && hash4(descriptor.envelope_digest) && hash4(descriptor.action_digest) && integer5(descriptor.index) && descriptor.index < 12 && providerId(descriptor.provider_message_id) && uuid3(descriptor.claim_nonce), "C448_DESCRIPTOR");
    return descriptor;
  }
  function evidenceOf(raw, descriptor, actionNonce = null, reservedAt = null) {
    const evidence = own5(raw);
    need13(exact5(evidence, ["mode", "descriptor_sha256", "action_nonce", "provider", "provider_message_id", "index", "recipient_jid", "account_jid", "ack", "observed_at_ms"]) && evidence.mode === "authenticated_baileys_ack" && evidence.descriptor_sha256 === sha(descriptor) && uuid3(evidence.action_nonce) && evidence.provider === "baileys" && evidence.provider_message_id === descriptor.provider_message_id && evidence.index === descriptor.index && evidence.recipient_jid === descriptor.recipient_jid && evidence.account_jid === cfg.account_jid && Number.isInteger(evidence.ack) && evidence.ack >= 2 && evidence.ack <= 5 && integer5(evidence.observed_at_ms) && (actionNonce === null || evidence.action_nonce === actionNonce) && (reservedAt === null || evidence.observed_at_ms >= reservedAt), "C448_ACK_EVIDENCE");
    return evidence;
  }
  function enter({ endpoint, method, path, body, engineMode, executionMode } = {}) {
    need13(configured, "C448_CONFIG_UNBOUND");
    need13(Object.hasOwn(paths2, endpoint) && method === "POST" && path === paths2[endpoint], "C448_ENDPOINT");
    need13(engineMode === "webhook" && executionMode === "production", "C448_EXECUTION_CONTEXT");
    const value = own5(body);
    need13(exact5(value, endpoint === "pull" ? [] : endpoint === "action_authorize" ? ["source", "descriptor"] : ["source", "descriptor", "evidence"]), "C448_REQUEST_BODY");
    if (endpoint === "pull") return { endpoint, body: {}, ...deniedAuthority };
    const source = sourceOf(value.source), descriptor = validateFullDescriptor(value.descriptor);
    return { endpoint, body: { source, descriptor, ...endpoint === "ack_observe" ? { evidence: evidenceOf(value.evidence, descriptor) } : {} }, ...deniedAuthority };
  }
  function checkedMailbox(raw) {
    const row = own5(raw);
    need13(integer5(row.id) && row.id > 0 && row.workflow_id === cfg.mailbox_workflow_id && row.code_manifest_sha256 === cfg.mailbox_manifest && row.business_id === cfg.scope.business_id && row.branch_id === cfg.scope.branch_id && integer5(row.head) && id(row.run_id) && typeof row.body_json === "string" && hash4(row.body_hash) && sha(row.body_json) === row.body_hash, "C448_MAILBOX_ROW");
    let body;
    try {
      body = JSON.parse(row.body_json);
    } catch {
      fail("C448_MAILBOX_JSON");
    }
    need13(exact5(body, ["schema", "tenant", "scope", "gateway_id", "account_jid", "source", "envelope", "envelope_digest", "action_digests", "recipient_jid", "admitted_at_ms", "deadline_at_ms", "created_at_ms", "claim", "actions"]) && body.schema === "native-qr-mailbox/v1" && body.tenant === cfg.tenant && same6(body.scope, cfg.scope) && body.gateway_id === cfg.gateway_id && body.account_jid === cfg.account_jid && body.recipient_jid === recipient(body.envelope?.sender) && hash4(body.envelope_digest) && same6(sourceOf(body.source), body.source) && row.run_id === "qr-mailbox:" + sha(JSON.stringify([cfg.tenant, cfg.scope.business_id, cfg.scope.branch_id, body.envelope?.delivery_id])) && integer5(body.admitted_at_ms) && integer5(body.deadline_at_ms) && body.deadline_at_ms > body.admitted_at_ms && body.deadline_at_ms - body.admitted_at_ms <= 3e5 && integer5(body.created_at_ms) && body.created_at_ms >= body.admitted_at_ms && body.created_at_ms < body.deadline_at_ms && Array.isArray(body.actions) && body.actions.length >= 1 && body.actions.length <= 12 && Array.isArray(body.action_digests) && body.action_digests.length === body.actions.length && body.action_digests.every(hash4), "C448_MAILBOX_BINDING");
    need13(exact5(body.envelope, ["delivery_id", "message_id", "sender", "turn", "actions"]) && id(body.envelope.delivery_id) && id(body.envelope.message_id) && exact5(body.envelope.turn, ["id", "outcome"]) && body.envelope.turn.id === body.envelope.message_id && ["none", "committed", "uncertain"].includes(body.envelope.turn.outcome) && Array.isArray(body.envelope.actions) && body.envelope.actions.length === body.actions.length, "C448_MAILBOX_ENVELOPE");
    if (body.claim !== null) need13(exact5(body.claim, ["gateway_id", "owner_execution", "nonce", "claimed_at_ms"]) && body.claim.gateway_id === cfg.gateway_id && id(body.claim.owner_execution) && uuid3(body.claim.nonce) && integer5(body.claim.claimed_at_ms) && body.claim.claimed_at_ms >= body.created_at_ms, "C448_MAILBOX_CLAIM");
    for (const [index, action] of body.actions.entries()) {
      need13(exact5(action, ["index", "action_digest", "state", "provider_message_id", "action_nonce", "reserved_at_ms", "receipt"]) && action.index === index && action.action_digest === body.action_digests[index] && ["QUEUED", "RESERVED", "UNKNOWN", "SENT"].includes(action.state), "C448_MAILBOX_ACTION");
      if (action.state === "QUEUED") need13([action.provider_message_id, action.action_nonce, action.reserved_at_ms, action.receipt].every((v) => v === null), "C448_MAILBOX_ACTION");
      else need13(body.claim && providerId(action.provider_message_id) && uuid3(action.action_nonce) && integer5(action.reserved_at_ms) && action.reserved_at_ms >= body.claim.claimed_at_ms && action.reserved_at_ms < body.deadline_at_ms, "C448_MAILBOX_ACTION");
      if (index > 0 && action.state !== "QUEUED") need13(body.actions[index - 1].state === "SENT", "C448_MAILBOX_ACTION_ORDER");
    }
    const providerIds = body.actions.filter((a) => a.provider_message_id !== null).map((a) => a.provider_message_id);
    need13(new Set(providerIds).size === providerIds.length, "C448_MAILBOX_PROVIDER_IDS");
    return { row, body };
  }
  function matchOriginalJournal(raw, source, mailboxBody) {
    const recovered = coordinator.recoverAdmission({ rows: [own5(raw)], runId: raw?.run_id });
    need13(recovered?.status === "recovered_read_only", "C448_ORIGINAL_PRIVATE_READ");
    const row = own5(recovered.record);
    need13(row.id === source.row_id && row.workflow_id === cfg.main_workflow_id && row.code_manifest_sha256 === cfg.main_manifest && row.run_id === source.run_id && row.business_id === cfg.scope.business_id && row.branch_id === cfg.scope.branch_id && row.owner_execution === source.owner_execution && row.nonce === source.nonce && integer5(row.head) && row.state === "mailbox_admission_consumed" && typeof row.body_json === "string" && sha(row.body_json) === row.body_hash, "C448_ORIGINAL_JOURNAL_ROW");
    let body;
    try {
      body = JSON.parse(row.body_json);
    } catch {
      fail("C448_ORIGINAL_JOURNAL_JSON");
    }
    const checkpoint = body.checkpoint, sealed = own5(checkpoint);
    delete sealed.checkpoint_hash;
    const pending = checkpoint?.pending;
    need13(body.version === 1 && body.workflow_id === cfg.main_workflow_id && body.code_manifest_sha256 === cfg.main_manifest && body.run_id === source.run_id && same6(body.scope, cfg.scope) && body.admitted_at_ms === mailboxBody.admitted_at_ms && body.deadline_at_ms === mailboxBody.deadline_at_ms && checkpoint?.checkpoint_hash === sha(sealed) && checkpoint.binding?.run_id === source.run_id && checkpoint.binding?.bundle_sha256 === cfg.main_manifest && checkpoint.binding?.created_at_ms === body.admitted_at_ms && pending?.kind === "sink" && pending.dispatch_status === "dispatched" && pending.effect_id === source.effect_id && pending.request_hash === source.request_hash && sha(pending.request) === source.request_hash && same6(pending.request.envelope, mailboxBody.envelope), "C448_ORIGINAL_PENDING_BINDING");
    return { row, body };
  }
  function expectedDescriptorFromRow({ mailboxRecord, index, providerMessageId, stage } = {}) {
    const { body } = checkedMailbox(mailboxRecord), action = body.actions[index];
    need13(integer5(index) && action && body.claim, "C448_ACTION_CLAIM_REQUIRED");
    if (stage === "authorize") {
      need13(action.state === "QUEUED" && providerId(providerMessageId) && !body.actions.some((a) => a.provider_message_id === providerMessageId) && body.actions.slice(0, index).every((a) => a.state === "SENT"), "C448_ACTION_QUEUED_REQUIRED");
    } else {
      need13(stage === "ack" && ["RESERVED", "UNKNOWN", "SENT"].includes(action.state), "C448_ACTION_RESERVED_REQUIRED");
      providerMessageId = action.provider_message_id;
    }
    return {
      tenant: cfg.tenant,
      scope: own5(cfg.scope),
      delivery_id: body.envelope.delivery_id,
      message_id: body.envelope.message_id,
      sender: body.envelope.sender,
      recipient_jid: body.recipient_jid,
      account_jid: cfg.account_jid,
      envelope_digest: body.envelope_digest,
      action_digest: body.action_digests[index],
      index,
      provider_message_id: providerMessageId,
      claim_nonce: body.claim.nonce
    };
  }
  function matchPrivate({ entry, mailboxRecord, originalJournalRecord } = {}) {
    need13(configured && entry && Object.hasOwn(paths2, entry.endpoint), "C448_ENTRY_REQUIRED");
    const { row, body } = checkedMailbox(mailboxRecord), source = sourceOf(body.source);
    if (entry.endpoint !== "pull") need13(same6(source, sourceOf(entry.body?.source)), "C448_PRIVATE_SOURCE_MISMATCH");
    matchOriginalJournal(originalJournalRecord, source, body);
    if (entry.endpoint === "pull") need13(row.state === "queued" && body.claim === null, "C448_PULL_QUEUED_REQUIRED");
    else {
      const descriptor = validateFullDescriptor(entry.body.descriptor), stage = entry.endpoint === "action_authorize" ? "authorize" : "ack";
      const expected = expectedDescriptorFromRow({ mailboxRecord, index: descriptor.index, providerMessageId: descriptor.provider_message_id, stage });
      need13(same6(descriptor, expected), "C448_FULL_DESCRIPTOR_MISMATCH");
      if (stage === "ack") evidenceOf(entry.body.evidence, descriptor, body.actions[descriptor.index].action_nonce, body.actions[descriptor.index].reserved_at_ms);
    }
    return { status: "private_correlations_match", source, mailbox_binding: { row_id: row.id, head: row.head, body_hash: row.body_hash }, ...deniedAuthority };
  }
  function comparePlannedDescriptor({ entry, planDescriptor } = {}) {
    need13(entry?.endpoint === "action_authorize" && same6(validateFullDescriptor(entry.body?.descriptor), validateFullDescriptor(planDescriptor)), "C448_PLAN_DESCRIPTOR_MISMATCH");
    return { status: "planned_descriptor_matches", ...deniedAuthority };
  }
  function validatePullWire(raw) {
    const value = own5(raw);
    need13(exact5(value, ["source", "claim_nonce", "envelope", "envelope_digest", "account_jid"]) && value.account_jid === cfg.account_jid && uuid3(value.claim_nonce), "C448_PULL_WIRE");
    sourceOf(value.source);
    const envelope = value.envelope;
    need13(exact5(envelope, ["tenant", "delivery_id", "message_id", "sender", "turn", "actions"]) && envelope.tenant === cfg.tenant && id(envelope.delivery_id) && id(envelope.message_id) && exact5(envelope.turn, ["id", "outcome"]) && envelope.turn.id === envelope.message_id && ["none", "committed", "uncertain"].includes(envelope.turn.outcome) && recipient(envelope.sender) && Array.isArray(envelope.actions) && envelope.actions.length >= 1 && envelope.actions.length <= 12 && envelope.actions.filter((a) => a.type === "media_binary").length <= 10, "C448_PULL_ENVELOPE");
    const actions = envelope.actions.map((action) => {
      if (action?.type === "text") {
        need13(exact5(action, ["type", "text"]) && typeof action.text === "string" && action.text.trim() && action.text.length <= 6e3, "C448_PULL_TEXT");
        return { type: "text", text: action.text };
      }
      need13(exact5(action, ["type", "data", "mimeType", ...Object.hasOwn(action, "caption") ? ["caption"] : []]) && action.type === "media_binary" && ["image/png", "image/jpeg", "image/webp"].includes(action.mimeType) && typeof action.data === "string" && action.data.length > 0 && action.data.length <= 11184812 && action.data.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(action.data) && (!Object.hasOwn(action, "caption") || typeof action.caption === "string" && action.caption.length <= 1024), "C448_PULL_IMAGE");
      const bytes = Buffer2.from(action.data, "base64"), valid = action.mimeType === "image/png" ? bytes.subarray(0, 8).equals(Buffer2.from([137, 80, 78, 71, 13, 10, 26, 10])) : action.mimeType === "image/jpeg" ? bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 : bytes.toString("ascii", 0, 4) === "RIFF" && bytes.toString("ascii", 8, 12) === "WEBP";
      need13(bytes.length > 0 && bytes.length <= 8 * 1024 * 1024 && bytes.toString("base64") === action.data && valid, "C448_PULL_IMAGE_BYTES");
      return { type: "media_binary", data: action.data, mimeType: action.mimeType, ...Object.hasOwn(action, "caption") ? { caption: action.caption } : {} };
    });
    need13(hash4(value.envelope_digest) && value.envelope_digest === sha(JSON.stringify([cfg.tenant, envelope.delivery_id, envelope.sender, envelope.message_id, actions])), "C448_PULL_WIRE_DIGEST");
    return { status: "wire_correlations_match", wire: { ...value, envelope: { ...envelope, actions } }, ...deniedAuthority };
  }
  return {
    enter,
    matchPrivate,
    validateFullDescriptor,
    expectedDescriptorFromRow,
    comparePlannedDescriptor,
    validatePullWire,
    contract: {
      configured,
      paths: own5(paths2),
      requires_native_header_auth: true,
      http_auth_accredited: false,
      requires_private_original_chain_validation: true,
      authorization_granted: false,
      provider_calls: 0,
      model_calls: 0
    }
  };
}

// APP POS/entregas/agente-20261028/native-n8n/native-control-c450/mailbox-client/private-mailbox-gateway-client-r02.mjs
var need11 = (ok, code) => {
  if (!ok) throw Error(code);
};
var hash3 = (x) => createHash2("sha256").update(x).digest("hex");
var digest = (x) => typeof x === "string" && /^[a-f0-9]{64}$/.test(x);
var integer4 = (x) => Number.isSafeInteger(x) && x >= 0;
var uuid2 = (x) => typeof x === "string" && /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(x);
var ident2 = (x) => typeof x === "string" && /^[A-Za-z0-9_.:-]{1,200}$/.test(x);
var exact4 = (x, keys) => x && typeof x === "object" && !Array.isArray(x) && Object.keys(x).sort().join("|") === [...keys].sort().join("|");
function canonical6(x) {
  if (x === null || ["string", "boolean"].includes(typeof x)) return JSON.stringify(x);
  if (typeof x === "number" && Number.isFinite(x)) return JSON.stringify(x);
  need11(x && typeof x === "object", "C448_CLIENT_JSON");
  if (Array.isArray(x)) return "[" + x.map(canonical6).join(",") + "]";
  const ds = Object.getOwnPropertyDescriptors(x);
  need11(!Object.getOwnPropertySymbols(x).length && Object.values(ds).every((d) => Object.hasOwn(d, "value") && d.enumerable), "C448_CLIENT_JSON");
  return "{" + Object.keys(ds).sort().map((k) => JSON.stringify(k) + ":" + canonical6(ds[k].value)).join(",") + "}";
}
var own4 = (x) => JSON.parse(canonical6(x));
var same5 = (a, b) => canonical6(a) === canonical6(b);
var paths = { pull: "/webhook/escalar-native-baileys-c450/mailbox/pull", action_authorize: "/webhook/escalar-native-baileys-c450/mailbox/action-authorize", ack_observe: "/webhook/escalar-native-baileys-c450/mailbox/ack-observe" };
var controllerKeys = ["tenant", "scope", "gateway_id", "account_jid", "main_workflow_id", "main_manifest", "mailbox_workflow_id", "mailbox_manifest", "mailbox_table_id"];
var sourceKeys = ["row_id", "workflow_id", "code_manifest_sha256", "business_id", "branch_id", "run_id", "effect_id", "request_hash", "owner_execution", "nonce"];
var accounts = /* @__PURE__ */ new Set();
function originOf(raw) {
  const url = new URL(raw);
  need11(url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash && url.pathname === "/" && url.origin === raw, "C448_CLIENT_HTTPS_ORIGIN");
  return url.origin;
}
var credentials = (raw) => {
  need11(exact4(raw, ["header_name", "header_value"]) && /^[A-Za-z][A-Za-z0-9-]{0,63}$/.test(raw.header_name) && !["host", "content-type", "content-length", "connection", "transfer-encoding"].includes(raw.header_name.toLowerCase()) && typeof raw.header_value === "string" && raw.header_value.length >= 1 && raw.header_value.length <= 4096 && !/[\r\n\0]/.test(raw.header_value), "C448_CLIENT_PRIVATE_CREDENTIAL");
  return raw;
};
function createFixedHttpsMailboxRequest({ httpsOrigin }) {
  const origin = originOf(httpsOrigin);
  return async ({ url, method, body, credential, timeoutMs, maxResponseBytes, redirect }) => {
    need11(Object.values(paths).some((path) => url === origin + path) && method === "POST" && redirect === "error", "C448_CLIENT_FIXED_ENDPOINT");
    need11(integer4(timeoutMs) && timeoutMs >= 1 && timeoutMs <= 6e4 && integer4(maxResponseBytes) && maxResponseBytes >= 1024 && maxResponseBytes <= 128 * 1024 * 1024, "C448_CLIENT_HTTP_LIMITS");
    const auth = credentials(credential), data = Buffer.from(JSON.stringify(body));
    return new Promise((resolve, reject) => {
      let settled = false, timer;
      const fail = () => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(Error("C448_CLIENT_HTTPS_UNKNOWN"));
        }
      };
      const req = httpsRequest3(url, { method: "POST", rejectUnauthorized: true, headers: { "content-type": "application/json", "content-length": data.length, [auth.header_name]: auth.header_value } }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400) {
          res.destroy();
          req.destroy();
          fail();
          return;
        }
        let size = 0;
        const chunks = [];
        res.on("data", (chunk) => {
          size += chunk.length;
          if (size > maxResponseBytes) {
            res.destroy();
            req.destroy();
            fail();
          } else chunks.push(chunk);
        });
        res.on("error", fail);
        res.on("end", () => {
          if (settled) return;
          try {
            const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            settled = true;
            clearTimeout(timer);
            resolve({ statusCode: res.statusCode, body: parsed, redirected: false, url });
          } catch {
            fail();
          }
        });
      });
      req.on("error", fail);
      timer = setTimeout(() => {
        req.destroy();
        fail();
      }, timeoutMs);
      req.end(data);
    });
  };
}
function createPrivateMailboxGatewayClient({ enabled = false, config, directory, readCredential, requestHTTPS, createMessageId, readRealClock = Date.now, httpTimeoutMs = 1e4, ackTimeoutMs = 6e4, maxResponseBytes = 128 * 1024 * 1024, maxDeliveries = 1e3 } = {}) {
  if (enabled !== true) return { bindSocket: () => ({ status: "OFF" }), runOnce: async () => ({ status: "OFF" }), retryPendingAcks: async () => ({ status: "OFF" }), inspectMetadata: () => ({ status: "OFF" }), close: async () => ({ status: "OFF" }) };
  return new PrivateMailboxGatewayClient({ config, directory, readCredential, requestHTTPS, createMessageId, readRealClock, httpTimeoutMs, ackTimeoutMs, maxResponseBytes, maxDeliveries });
}
var PrivateMailboxGatewayClient = class {
  #cfg;
  #origin;
  #controller;
  #transport;
  #directory;
  #file;
  #lock;
  #state;
  #clock;
  #credential;
  #request;
  #httpMs;
  #maxBytes;
  #cap;
  #closed = false;
  #busy = false;
  #work = null;
  #socket = null;
  #epoch = 0;
  #active = null;
  constructor({ config, directory, readCredential, requestHTTPS, createMessageId, readRealClock, httpTimeoutMs, ackTimeoutMs, maxResponseBytes, maxDeliveries }) {
    need11(exact4(config, ["https_origin", ...controllerKeys]) && isAbsolute3(directory || "") && [readCredential, createMessageId, readRealClock].every((x) => typeof x === "function") && integer4(httpTimeoutMs) && httpTimeoutMs >= 1 && httpTimeoutMs <= 6e4 && integer4(ackTimeoutMs) && ackTimeoutMs >= 1 && ackTimeoutMs <= 6e4 && integer4(maxResponseBytes) && maxResponseBytes >= 1024 && maxResponseBytes <= 128 * 1024 * 1024 && integer4(maxDeliveries) && maxDeliveries >= 1 && maxDeliveries <= 1e4, "C448_CLIENT_CONFIGURATION");
    this.#cfg = own4(config);
    this.#origin = originOf(config.https_origin);
    this.#clock = readRealClock;
    this.#credential = readCredential;
    this.#request = requestHTTPS || createFixedHttpsMailboxRequest({ httpsOrigin: this.#origin });
    need11(typeof this.#request === "function", "C448_CLIENT_HTTP_CALLBACK");
    this.#httpMs = httpTimeoutMs;
    this.#maxBytes = maxResponseBytes;
    this.#cap = maxDeliveries;
    this.#directory = directory;
    this.#file = join2(directory, "client-metadata.json");
    this.#lock = join2(directory, "client-exclusive.lock");
    const fixed = Object.fromEntries(controllerKeys.map((k) => [k, config[k]]));
    this.#controller = createNativeMailboxEntryController({ Buffer, sha256: hash3, canonicalJSON: canonical6, config: fixed, coordinator: { recoverAdmission: () => {
      throw Error("C448_CLIENT_NO_BUSINESS_JOURNAL");
    } } });
    need11(this.#controller.contract.configured && !accounts.has(config.account_jid), "C448_CLIENT_ACCOUNT_ALREADY_OWNED");
    mkdirSync2(directory, { recursive: true, mode: 448 });
    let fd;
    try {
      fd = openSync2(this.#lock, "wx", 384);
      writeFileSync2(fd, JSON.stringify({ pid: process.pid }));
      fsyncSync2(fd);
    } catch {
      throw Error("C448_CLIENT_EXCLUSIVE_REVIEW_REQUIRED");
    } finally {
      if (fd !== void 0) closeSync2(fd);
    }
    try {
      this.#state = existsSync2(this.#file) ? this.#read() : { schema: 1, config_sha256: hash3(canonical6(config)), pull: null, deliveries: [] };
      if (!existsSync2(this.#file)) this.#persist(true);
      this.#transport = new BaileysDeliveryTransport({ tenant: config.tenant, businessId: config.scope.business_id, branchId: config.scope.branch_id, accountJid: config.account_jid, directory: join2(directory, "recorder"), authorizeAction: (x) => this.#authorize(x), createMessageId, readRealClock, ackTimeoutMs, authorizeTimeoutMs: Math.min(6e4, httpTimeoutMs * 2 + 1e3) });
      accounts.add(config.account_jid);
    } catch (error2) {
      try {
        unlinkSync(this.#lock);
      } catch {
      }
      throw Error("C448_CLIENT_METADATA_REVIEW_REQUIRED");
    }
  }
  #now() {
    const at = this.#clock();
    need11(integer4(at) && at > 0, "C448_CLIENT_CLOCK");
    return at;
  }
  #entry(endpoint, body) {
    return this.#controller.enter({ endpoint, method: "POST", path: paths[endpoint].slice("/webhook/".length), body, engineMode: "webhook", executionMode: "production" });
  }
  #read() {
    const raw = JSON.parse(readFileSync2(this.#file, "utf8")), { checksum, ...body } = raw;
    need11(exact4(raw, ["schema", "config_sha256", "pull", "deliveries", "checksum"]) && checksum === hash3(canonical6(body)) && body.schema === 1 && body.config_sha256 === hash3(canonical6(this.#cfg)) && Array.isArray(body.deliveries) && body.deliveries.length <= this.#cap, "C448_CLIENT_METADATA_INTEGRITY");
    if (body.pull !== null) need11(exact4(body.pull, ["intent_nonce", "at_ms"]) && uuid2(body.pull.intent_nonce) && integer4(body.pull.at_ms) && body.pull.at_ms > 0, "C448_CLIENT_PULL_INTENT");
    const seen = /* @__PURE__ */ new Set();
    for (const row of body.deliveries) {
      need11(exact4(row, ["delivery_id", "message_id", "sender", "source", "claim_nonce", "envelope_digest", "action_count", "created_at_ms", "status", "actions"]) && !seen.has(row.delivery_id) && ident2(row.delivery_id) && ident2(row.message_id) && baileysRecipient(row.sender).sender === row.sender && ["pending", "review_required", "ack_durable"].includes(row.status) && uuid2(row.claim_nonce) && digest(row.envelope_digest) && integer4(row.action_count) && row.action_count >= 1 && row.action_count <= 12 && integer4(row.created_at_ms) && row.created_at_ms > 0 && Array.isArray(row.actions) && row.actions.length <= row.action_count, "C448_CLIENT_DELIVERY_METADATA");
      seen.add(row.delivery_id);
      const source = row.source;
      need11(exact4(source, sourceKeys) && integer4(source.row_id) && source.row_id > 0 && source.workflow_id === this.#cfg.main_workflow_id && source.code_manifest_sha256 === this.#cfg.main_manifest && source.business_id === this.#cfg.scope.business_id && source.branch_id === this.#cfg.scope.branch_id && [source.run_id, source.owner_execution].every(ident2) && [source.effect_id, source.request_hash].every(digest) && uuid2(source.nonce), "C448_CLIENT_SOURCE_METADATA");
      for (const [i, a] of row.actions.entries()) {
        need11(exact4(a, ["descriptor", "action_nonce", "issued_at_ms", "expires_at_ms", "evidence", "ack_durable"]) && typeof a.ack_durable === "boolean", "C448_CLIENT_ACTION_METADATA");
        const d = this.#controller.validateFullDescriptor(a.descriptor);
        this.#entry("action_authorize", { source: row.source, descriptor: d });
        need11(d.index === i && d.delivery_id === row.delivery_id && d.message_id === row.message_id && d.sender === row.sender && d.envelope_digest === row.envelope_digest && d.claim_nonce === row.claim_nonce, "C448_CLIENT_METADATA_BINDING");
        if (a.action_nonce !== null) need11(uuid2(a.action_nonce) && integer4(a.issued_at_ms) && integer4(a.expires_at_ms) && a.expires_at_ms > a.issued_at_ms && a.expires_at_ms - a.issued_at_ms <= 2e3, "C448_CLIENT_ACTION_GRANT");
        else need11(a.issued_at_ms === null && a.expires_at_ms === null && a.evidence === null && !a.ack_durable, "C448_CLIENT_ACTION_UNKNOWN");
        if (a.evidence !== null) {
          this.#entry("ack_observe", { source: row.source, descriptor: d, evidence: a.evidence });
          need11(a.evidence.action_nonce === a.action_nonce && a.evidence.observed_at_ms >= a.issued_at_ms, "C448_CLIENT_EVIDENCE_METADATA");
        }
        if (a.ack_durable) need11(a.evidence !== null, "C448_CLIENT_ACK_METADATA");
      }
      if (row.status === "ack_durable") need11(row.actions.length === row.action_count && row.actions.every((a) => a.ack_durable), "C448_CLIENT_DONE_METADATA");
    }
    return body;
  }
  #persist(exclusive = false) {
    const body = own4(this.#state), data = JSON.stringify({ ...body, checksum: hash3(canonical6(body)) }), target = exclusive ? this.#file : this.#file + "." + randomUUID2() + ".tmp";
    const fd = openSync2(target, "wx", 384);
    try {
      writeFileSync2(fd, data);
      fsyncSync2(fd);
    } finally {
      closeSync2(fd);
    }
    if (!exclusive) renameSync2(target, this.#file);
    if (process.platform !== "win32") {
      const dir = openSync2(this.#directory, "r");
      try {
        fsyncSync2(dir);
      } finally {
        closeSync2(dir);
      }
    }
  }
  #fence(epoch) {
    need11(!this.#closed && this.#socket && this.#epoch === epoch && baileysRecipient(this.#socket.user?.id).jid === this.#cfg.account_jid, "C448_CLIENT_SOCKET_EPOCH_CHANGED");
  }
  async #http(endpoint, body, epoch) {
    this.#fence(epoch);
    this.#entry(endpoint, body);
    let timer, expired = false;
    try {
      const url = this.#origin + paths[endpoint], response = await Promise.race([Promise.resolve().then(async () => {
        const credential = credentials(await this.#credential());
        need11(!expired, "C448_CLIENT_HTTP_UNKNOWN");
        this.#fence(epoch);
        return this.#request({ url, method: "POST", body: own4(body), credential, timeoutMs: this.#httpMs, maxResponseBytes: this.#maxBytes, redirect: "error" });
      }), new Promise((_, reject) => {
        timer = setTimeout(() => reject(Error("C448_CLIENT_HTTP_UNKNOWN")), this.#httpMs);
      })]);
      this.#fence(epoch);
      need11(exact4(response, ["statusCode", "body", "redirected", "url"]) && response.redirected === false && response.url === url && integer4(response.statusCode) && response.statusCode >= 100 && response.statusCode <= 599 && Buffer.byteLength(JSON.stringify(response.body)) <= this.#maxBytes, "C448_CLIENT_HTTP_RESPONSE");
      need11(response.statusCode === 200, response.statusCode === 409 ? "C448_CLIENT_ACK_REVIEW_REQUIRED" : "C448_CLIENT_HTTP_REVIEW_REQUIRED");
      return own4(response.body);
    } catch {
      throw Error("C448_CLIENT_HTTP_REVIEW_REQUIRED");
    } finally {
      expired = true;
      clearTimeout(timer);
    }
  }
  bindSocket(socket) {
    need11(!this.#closed && baileysRecipient(socket?.user?.id).jid === this.#cfg.account_jid, "C448_CLIENT_ACCOUNT_BINDING");
    this.#epoch++;
    this.#socket = socket;
    this.#transport.bindSocket(socket);
    return { status: "BOUND", epoch: this.#epoch };
  }
  async #serial(work) {
    need11(!this.#closed && !this.#busy, "C448_CLIENT_BUSY_OR_CLOSED");
    this.#busy = true;
    const operation = Promise.resolve().then(work);
    this.#work = operation;
    try {
      return await operation;
    } finally {
      this.#busy = false;
      this.#work = null;
    }
  }
  #row(id) {
    const row = this.#state.deliveries.find((r) => r.delivery_id === id);
    need11(row, "C448_CLIENT_DELIVERY_METADATA_MISSING");
    return row;
  }
  async #flush(row, a, epoch) {
    this.#fence(epoch);
    if (a.ack_durable) return;
    need11(uuid2(a.action_nonce), "C448_CLIENT_ORIGINAL_ACTION_PROOF_REQUIRED");
    const evidence = this.#transport.observeAction({ source: row.source, descriptor: a.descriptor, action_nonce: a.action_nonce });
    this.#entry("ack_observe", { source: row.source, descriptor: a.descriptor, evidence });
    need11(evidence.observed_at_ms <= this.#now() && evidence.observed_at_ms >= a.issued_at_ms, "C448_CLIENT_ACK_CLOCK");
    if (a.evidence !== null) need11(same5(a.evidence, evidence), "C448_CLIENT_ORIGINAL_EVIDENCE_CHANGED");
    else {
      a.evidence = own4(evidence);
      this.#persist();
    }
    const result = await this.#http("ack_observe", { source: row.source, descriptor: a.descriptor, evidence: a.evidence }, epoch);
    need11(exact4(result, ["status"]) && ["action_receipt_durable", "receipt_already_durable"].includes(result.status), "C448_CLIENT_ACK_NOT_DURABLE");
    this.#fence(epoch);
    a.ack_durable = true;
    this.#persist();
  }
  async #authorize({ source, descriptor }) {
    const active = this.#active;
    need11(active, "C448_CLIENT_NO_ACTIVE_DELIVERY");
    this.#fence(active.epoch);
    const row = this.#row(active.delivery_id), d = this.#controller.validateFullDescriptor(descriptor);
    need11(same5(source, row.source) && d.delivery_id === row.delivery_id && d.message_id === row.message_id && d.sender === row.sender && d.claim_nonce === row.claim_nonce && d.envelope_digest === row.envelope_digest && d.index === row.actions.length, "C448_CLIENT_ACTIVE_BINDING");
    for (const previous of row.actions) await this.#flush(row, previous, active.epoch);
    this.#fence(active.epoch);
    const a = { descriptor: own4(d), action_nonce: null, issued_at_ms: null, expires_at_ms: null, evidence: null, ack_durable: false };
    row.actions.push(a);
    this.#persist();
    const grant = await this.#http("action_authorize", { source: row.source, descriptor: d }, active.epoch), at = this.#now();
    need11(exact4(grant, ["mode", "descriptor_sha256", "action_nonce", "issued_at_ms", "expires_at_ms"]) && grant.mode === "native_baileys_action_dispatch" && grant.descriptor_sha256 === hash3(canonical6(d)) && uuid2(grant.action_nonce) && integer4(grant.issued_at_ms) && integer4(grant.expires_at_ms) && grant.issued_at_ms <= at && at < grant.expires_at_ms && grant.expires_at_ms - grant.issued_at_ms <= 2e3, "C448_CLIENT_DIRECT_GATE_REQUIRED");
    a.action_nonce = grant.action_nonce;
    a.issued_at_ms = grant.issued_at_ms;
    a.expires_at_ms = grant.expires_at_ms;
    this.#persist();
    this.#fence(active.epoch);
    return grant;
  }
  async runOnce() {
    return this.#serial(async () => {
      const epoch = this.#epoch;
      this.#fence(epoch);
      need11(this.#state.pull === null && this.#state.deliveries.every((r) => r.status === "ack_durable") && this.#state.deliveries.length < this.#cap, "C448_CLIENT_REVIEW_REQUIRED_NO_RESEND");
      this.#state.pull = { intent_nonce: randomUUID2(), at_ms: this.#now() };
      this.#persist();
      const raw = await this.#http("pull", {}, epoch);
      if (exact4(raw, ["status"]) && raw.status === "idle") {
        this.#state.pull = null;
        this.#persist();
        return { status: "idle" };
      }
      const checked = this.#controller.validatePullWire(raw).wire;
      this.#fence(epoch);
      need11(!this.#state.deliveries.some((r) => r.delivery_id === checked.envelope.delivery_id), "C448_CLIENT_DUPLICATE_PULL_REVIEW_REQUIRED");
      const row = { delivery_id: checked.envelope.delivery_id, message_id: checked.envelope.message_id, sender: checked.envelope.sender, source: checked.source, claim_nonce: checked.claim_nonce, envelope_digest: checked.envelope_digest, action_count: checked.envelope.actions.length, created_at_ms: this.#now(), status: "pending", actions: [] };
      this.#state.deliveries.push(row);
      this.#state.pull = null;
      this.#persist();
      this.#active = { delivery_id: row.delivery_id, epoch };
      try {
        await this.#transport.deliver(checked.envelope, { source: checked.source, claim_nonce: checked.claim_nonce });
        this.#fence(epoch);
        for (const a of row.actions) await this.#flush(row, a, epoch);
        need11(row.actions.length === row.action_count && row.actions.every((a) => a.ack_durable), "C448_CLIENT_ACK_INCOMPLETE");
        row.status = "ack_durable";
        this.#persist();
        return { status: "mailbox_ack_durable", delivery_id: row.delivery_id, actions: row.action_count };
      } catch {
        row.status = "review_required";
        this.#persist();
        throw Error("C448_CLIENT_REVIEW_REQUIRED_NO_RESEND");
      } finally {
        this.#active = null;
      }
    });
  }
  async retryPendingAcks() {
    return this.#serial(async () => {
      const epoch = this.#epoch;
      this.#fence(epoch);
      let acknowledgements = 0, pending = Boolean(this.#state.pull);
      for (const row of this.#state.deliveries) {
        if (row.status === "ack_durable") continue;
        for (const a of row.actions) {
          if (a.ack_durable) continue;
          try {
            await this.#flush(row, a, epoch);
            acknowledgements++;
          } catch {
            pending = true;
          }
        }
        if (row.actions.length === row.action_count && row.actions.every((a) => a.ack_durable)) {
          row.status = "ack_durable";
          this.#persist();
        } else pending = true;
      }
      return { status: pending ? "REVIEW_REQUIRED" : "ACK_ONLY_DURABLE", acknowledgements, provider_resends: 0 };
    });
  }
  inspectMetadata() {
    return { status: this.#closed ? "CLOSED" : "OPEN", epoch: this.#epoch, metadata: own4(this.#state) };
  }
  async close() {
    if (this.#closed) return { status: "CLOSED" };
    this.#closed = true;
    this.#epoch++;
    await this.#transport.close();
    if (this.#work) await Promise.allSettled([this.#work]);
    accounts.delete(this.#cfg.account_jid);
    try {
      unlinkSync(this.#lock);
    } catch {
    }
    return { status: "CLOSED" };
  }
};

// APP POS/entregas/agente-20261028/native-n8n/native-control-c464/preparation/native-owned-connection-r01.mjs
var need12 = (ok, code) => {
  if (!ok) throw Error(code);
};
function createNativeOwnedConnection(options = {}) {
  if (options.enabled !== true) return createDirectNativeTransport();
  const cfg = structuredClone(options.config), directory = options.directory;
  const { readCredential, createMessageId, isCurrentSocket, installed } = options;
  const callbacks = options.callbacks, requests = options.requestHTTPS ?? {};
  need12(cfg?.https_origin === "https://n8n.srv1547104.hstgr.cloud" && isAbsolute4(directory ?? "") && [readCredential, createMessageId, isCurrentSocket].every((f) => typeof f === "function") && typeof callbacks?.enqueue === "function" && typeof callbacks?.recordUnsupported === "function" && Object.keys(requests).every((k) => ["mailbox", "reserve", "validate", "complete"].includes(k)) && Object.values(requests).every((f) => typeof f === "function"), "C464_PRIVATE_OWNED_CONFIGURATION");
  const fixedCallbacks = { enqueue: callbacks.enqueue, recordUnsupported: callbacks.recordUnsupported };
  const fixedRequests = { ...requests };
  const mediaConfig = Object.fromEntries(["https_origin", "tenant", "gateway_id", "account_jid", "scope"].map((k) => [k, cfg[k]]));
  const ownedOnly = () => {
    throw Error("C464_MEDIA_CALLBACK_REQUIRES_OWNED_HOST");
  };
  return createDirectNativeTransport({
    enabled: true,
    isCurrentSocket,
    ...options.pollIntervalMs !== void 0 ? { pollIntervalMs: options.pollIntervalMs } : {},
    loadPrivateBindings: () => ({
      config: cfg,
      directory,
      readCredential,
      createMessageId,
      inboundCallbacks: {
        ...fixedCallbacks,
        reserveMedia: ownedOnly,
        downloadOriginal: ownedOnly,
        validateReservation: ownedOnly,
        completeMedia: ownedOnly
      },
      createInboundHost: () => createNativeMediaHostWithOwnedDownloads({
        enabled: true,
        config: mediaConfig,
        installed,
        readCredential,
        callbacks: fixedCallbacks,
        requestHTTPS: Object.fromEntries(["reserve", "validate", "complete"].filter((k) => fixedRequests[k]).map((k) => [k, fixedRequests[k]])),
        ...options.readRealClock ? { readRealClock: options.readRealClock } : {},
        ...options.downloadTimeoutMs !== void 0 ? { downloadTimeoutMs: options.downloadTimeoutMs } : {},
        ...options.cleanupTimeoutMs !== void 0 ? { cleanupTimeoutMs: options.cleanupTimeoutMs } : {},
        ...options.maxActive !== void 0 ? { maxActive: options.maxActive } : {}
      }),
      createMailboxClient: (args) => createPrivateMailboxGatewayClient({
        ...args,
        ...fixedRequests.mailbox ? { requestHTTPS: fixedRequests.mailbox } : {},
        ...options.readRealClock ? { readRealClock: options.readRealClock } : {}
      })
    })
  });
}
export {
  createNativeOwnedConnection
};
