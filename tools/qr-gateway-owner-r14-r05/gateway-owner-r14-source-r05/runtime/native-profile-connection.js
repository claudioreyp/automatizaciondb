import { createNativeAckOwnerDrain } from './native-ack-owner.mjs';
import { isAbsolute } from 'node:path';
import { createDirectNativeTransport } from './direct-native-transport.js';
import { createNativeOwnedConnection } from './native-owned-connection.mjs';
import { createNativeProfileRequests } from './native-profile-requests.mjs';
import { createNativeTextIngressClient } from './native-text-ingress-client.js';
import { createNativeUnsupportedClient } from './native-unsupported-client.mjs';
import { createNativeQueuedUnsupportedClient } from './native-queued-unsupported-client.mjs';
function need(v, c) { if (!v)
    throw Error(c); }
// The factories are module dependencies, never request fields. This seam tests
// ownership/close behavior without starting the closed mailbox ACK scenario.
export function createNativeProfileConnectionComposer(factories) {
    return function createProfileConnection(options = {}) {
        if (options.enabled !== true)
            return createDirectNativeTransport();
        const cfg = structuredClone(options.config), directory = options.directory;
        need(cfg && cfg.https_origin === 'https://n8n.srv1547104.hstgr.cloud' && typeof directory === 'string' && isAbsolute(directory)
            && typeof options.endpoints?.unsupported === 'string' && [options.readCredential, options.createMessageId, options.isCurrentSocket].every(f => typeof f === 'function')
            && options.installed && [options.installed.Agent, options.installed.getMediaKeys, options.installed.fetch].every(f => typeof f === 'function'), 'PROFILE_CONNECTION_PRIVATE_BINDINGS');
        const readCredential = options.readCredential;
        let requests, text, unsupported, owner;
        let ackOwnerDrain;
        try {
            requests = factories.requests({ enabled: true, endpoints: options.endpoints, ...(options.fetchImpl ? { fetchImpl: options.fetchImpl } : {}) });
            if (options.ackOwnerDrain === true)
                ackOwnerDrain = createNativeAckOwnerDrain({ enabled: true, config: cfg, readCredential, requestHTTPS: requests.ack });
            text = factories.text({ enabled: true, config: { https_origin: cfg.https_origin, tenant: cfg.tenant, gateway_id: cfg.gateway_id, account_jid: cfg.account_jid }, readCredential, requestHTTPS: requests.text });
            const queued = options.endpoints.unsupported.endsWith('/private-unsupported-queue');
            if (queued)
                need(typeof factories.queuedUnsupported === 'function', 'PROFILE_CONNECTION_QUEUE_CLIENT_REQUIRED');
            unsupported = queued
                ? factories.queuedUnsupported({ enabled: true, config: { https_origin: cfg.https_origin, tenant: cfg.tenant, account_jid: cfg.account_jid, scope: cfg.scope }, readCredential, requestHTTPS: requests.unsupported })
                : factories.unsupported({ enabled: true, config: { https_origin: cfg.https_origin, tenant: cfg.tenant, account_jid: cfg.account_jid }, readCredential, requestHTTPS: requests.unsupported });
            owner = factories.owner({ enabled: true, config: cfg, directory, readCredential, ...(ackOwnerDrain ? { drainAck: input => ackOwnerDrain.run(input) } : {}), createMessageId: options.createMessageId, isCurrentSocket: options.isCurrentSocket,
                installed: options.installed, callbacks: { enqueue: input => text.enqueue(input), recordUnsupported: input => unsupported.recordUnsupported(input) },
                requestHTTPS: { mailbox: requests.mailbox, reserve: requests.reserve, validate: requests.validate, complete: requests.complete } });
        }
        catch (error) {
            // All these constructors are dormant: no socket, credential reads or wire
            // has started. Still invalidate every successfully constructed dependency.
            const invoke = (fn) => { try {
                return Promise.resolve(fn());
            }
            catch (e) {
                return Promise.reject(e);
            } };
            void Promise.allSettled([invoke(() => text?.close()), invoke(() => unsupported?.close()), invoke(() => requests?.close()), invoke(() => ackOwnerDrain?.close())]);
            throw error;
        }
        let closing = false, unconfirmed = false, closed = false, closePromise;
        function close() {
            if (closePromise)
                return closePromise;
            let resolve, reject;
            closePromise = new Promise((done, fail) => { resolve = done; reject = fail; });
            void closePromise.catch(() => { });
            closing = true;
            const invoke = (fn) => { try {
                return Promise.resolve(fn());
            }
            catch (error) {
                return Promise.reject(error);
            } };
            // Epoch invalidation and cancellation happen synchronously, before await.
            const work = Promise.allSettled([invoke(() => owner.close()), invoke(() => text.close()), invoke(() => unsupported.close()), invoke(() => requests.close()), invoke(() => ackOwnerDrain?.close())]);
            let timer;
            void Promise.race([work, new Promise((_, fail) => { timer = setTimeout(() => fail(Error('PROFILE_CONNECTION_CLOSE_UNCONFIRMED')), 5000); })])
                .then(results => { if (results.some(r => r.status === 'rejected'))
                throw Error('PROFILE_CONNECTION_CLOSE_UNCONFIRMED'); closed = true; resolve(); })
                .catch(() => { unconfirmed = true; reject(Error('PROFILE_CONNECTION_CLOSE_UNCONFIRMED')); }).finally(() => clearTimeout(timer));
            return closePromise;
        }
        return {
            observeSocket(socket) { if (!closing)
                owner.observeSocket(socket); },
            observeConnection(socket, update) { if (!closing)
                owner.observeConnection(socket, update); }, close,
            ackReference: () => closing || closed || unconfirmed ? null : owner.ackReference(),
            inspectMetadata: () => ({ ...owner.inspectMetadata(), status: unconfirmed ? 'CLOSE_UNCONFIRMED' : closed ? 'CLOSED' : closing ? 'CLOSING' : owner.inspectMetadata().status }),
        };
    };
}
export const createNativeProfileConnection = createNativeProfileConnectionComposer({ requests: createNativeProfileRequests, text: createNativeTextIngressClient, unsupported: createNativeUnsupportedClient, queuedUnsupported: createNativeQueuedUnsupportedClient, owner: createNativeOwnedConnection });
