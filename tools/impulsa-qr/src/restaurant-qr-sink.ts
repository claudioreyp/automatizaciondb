import { createHash, randomUUID, timingSafeEqual } from 'node:crypto';
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { createServer, type Server } from 'node:http';
import { isAbsolute, join, resolve } from 'node:path';
import { acceptedProviderReceipt, individualRecipient, restaurantAction, type ProviderReceipt, type RestaurantAction } from './restaurant-transport.js';

const digest = (value: unknown) => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const id = (value: unknown): string => {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_.:-]{1,200}$/.test(value)) throw new SinkError(422, 'invalid_identifier');
  return value;
};
export class SinkError extends Error {
  constructor(readonly status: number, readonly code: string) { super(code); }
}
export type DeliveryReceipt = {
  transport_id: string; provider: 'wppconnect'; delivery_id: string; message_id: string; sender: string;
  action_receipts: (ProviderReceipt & { index: number })[];
};
type RecordEntry = {
  version: 1; tenant: string; delivery_id: string; message_id: string; sender: string; digest: string;
  status: 'uncertain' | 'sent'; created_at: string; action_count: number; action_receipts: DeliveryReceipt['action_receipts'];
  receipt?: DeliveryReceipt;
};
export type ReceiptTransport = (tenant: string, sender: string, action: RestaurantAction) => Promise<ProviderReceipt>;

/** Single process owns this private directory. A stale lock fails closed for review.
 * Records contain hashes/provider receipts, never message text or image bytes. */
export class RestaurantDeliveryJournal {
  private readonly directory: string;
  private readonly lockPath: string;
  private readonly lock: number;
  private closed = false;
  private readonly running = new Map<string, Promise<DeliveryReceipt>>();
  constructor(readonly tenant: string, directory: string, private readonly send: ReceiptTransport, private readonly sendTimeoutMs = 60000) {
    if (!/^[A-Za-z0-9_-]{1,80}$/.test(tenant) || !isAbsolute(directory)
        || !Number.isInteger(sendTimeoutMs) || sendTimeoutMs < 1 || sendTimeoutMs > 60000) throw new Error('sink_configuration_invalid');
    this.directory = resolve(directory);
    mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    this.lockPath = join(this.directory, 'writer.lock');
    this.lock = openSync(this.lockPath, 'wx', 0o600);
    try { writeFileSync(this.lock, JSON.stringify({ pid: process.pid, tenant })); fsyncSync(this.lock); }
    catch (error) { closeSync(this.lock); throw error; }
  }
  private path(delivery: string) { return join(this.directory, digest([this.tenant, delivery]) + '.json'); }
  private read(delivery: string): RecordEntry | null {
    const path = this.path(delivery);
    if (!existsSync(path)) return null;
    let row: RecordEntry;
    try { row = JSON.parse(readFileSync(path, 'utf8')) as RecordEntry; }
    catch { throw new SinkError(503, 'journal_invalid'); }
    try {
      if (row.version !== 1 || row.tenant !== this.tenant || row.delivery_id !== delivery || id(row.message_id) !== row.message_id
          || individualRecipient(row.sender) !== row.sender || !/^[a-f0-9]{64}$/.test(row.digest)
          || !Number.isInteger(row.action_count) || row.action_count < 1 || row.action_count > 12
          || !['sent', 'uncertain'].includes(row.status) || !Array.isArray(row.action_receipts)
          || row.action_receipts.length > row.action_count) throw new Error('invalid_record');
      const identifiers = new Set<string>();
      for (const [index, action] of row.action_receipts.entries()) {
        const verified = acceptedProviderReceipt(action);
        if (action.index !== index || action.provider !== 'wppconnect' || identifiers.has(verified.provider_message_id)) throw new Error('invalid_action_receipt');
        identifiers.add(verified.provider_message_id);
      }
      if (row.status === 'sent') {
        const receipt = row.receipt;
        if (!receipt || receipt.provider !== 'wppconnect' || receipt.transport_id !== 'wppconnect:' + digest([this.tenant, delivery])
            || receipt.delivery_id !== row.delivery_id || receipt.message_id !== row.message_id || receipt.sender !== row.sender
            || row.action_receipts.length !== row.action_count
            || JSON.stringify(receipt.action_receipts) !== JSON.stringify(row.action_receipts)) throw new Error('invalid_complete_receipt');
      } else if (row.receipt !== undefined) throw new Error('uncertain_with_complete_receipt');
    } catch { throw new SinkError(503, 'journal_invalid'); }
    return row;
  }
  private persist(row: RecordEntry) {
    const temporary = this.path(row.delivery_id) + '.' + randomUUID() + '.tmp';
    const fd = openSync(temporary, 'wx', 0o600);
    try { writeFileSync(fd, JSON.stringify(row)); fsyncSync(fd); } finally { closeSync(fd); }
    renameSync(temporary, this.path(row.delivery_id));
    // POSIX directory entries also need fsync; Windows does not support opening directories this way.
    if (process.platform !== 'win32') { const dir = openSync(this.directory, 'r'); try { fsyncSync(dir); } finally { closeSync(dir); } }
  }
  inspect(delivery: string) {
    if (this.closed) throw new SinkError(503, 'journal_closed');
    const row = this.read(id(delivery));
    if (!row) throw new SinkError(404, 'delivery_not_found');
    return { delivery_id: row.delivery_id, message_id: row.message_id, sender: row.sender,
      status: row.status, action_receipts: row.action_receipts, ...(row.receipt ? { receipt: row.receipt } : {}) };
  }
  async deliver(raw: unknown): Promise<DeliveryReceipt> {
    if (this.closed) throw new SinkError(503, 'journal_closed');
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new SinkError(422, 'invalid_envelope');
    const value = raw as Record<string, unknown>;
    if (Object.keys(value).some(k => !['delivery_id', 'sender', 'message_id', 'turn', 'actions'].includes(k))) throw new SinkError(422, 'invalid_envelope');
    const delivery = id(value.delivery_id), message = id(value.message_id);
    let sender: string, actions: RestaurantAction[];
    try {
      sender = individualRecipient(value.sender);
      if (!Array.isArray(value.actions) || !value.actions.length || value.actions.length > 12) throw new Error('invalid_actions');
      actions = value.actions.map(restaurantAction);
      if (actions.filter(a => a.type === 'media_binary').length > 10) throw new Error('too_many_images');
    } catch { throw new SinkError(422, 'invalid_actions_or_recipient'); }
    if (value.turn && (typeof value.turn !== 'object' || Array.isArray(value.turn)
        || (value.turn as { id?: unknown }).id !== message)) throw new SinkError(422, 'turn_mismatch');
    const fingerprint = digest([this.tenant, delivery, sender, message, actions]);
    const previous = this.read(delivery);
    if (previous) {
      if (previous.digest !== fingerprint) throw new SinkError(409, 'delivery_conflict');
      if (previous.status === 'sent') return previous.receipt!;
      const pending = this.running.get(delivery);
      if (pending) return pending;
      throw new SinkError(409, 'delivery_uncertain');
    }
    if (this.running.size >= 2) throw new SinkError(429, 'sink_busy');
    const row: RecordEntry = { version: 1, tenant: this.tenant, delivery_id: delivery, message_id: message,
      sender, digest: fingerprint, status: 'uncertain', created_at: new Date().toISOString(), action_count: actions.length, action_receipts: [] };
    // This write must succeed BEFORE any call which may reach the provider.
    this.persist(row);
    const work = this.sendActions(row, actions);
    this.running.set(delivery, work);
    try { return await work; } finally { this.running.delete(delivery); }
  }
  private async sendActions(row: RecordEntry, actions: RestaurantAction[]): Promise<DeliveryReceipt> {
    try {
      for (const [index, action] of actions.entries()) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        let result: ProviderReceipt;
        try {
          result = await Promise.race([
            Promise.resolve().then(() => this.send(this.tenant, row.sender, action)),
            new Promise<never>((_resolve, reject) => { timer = setTimeout(() => reject(new Error('provider_timeout')), this.sendTimeoutMs); }),
          ]);
        } finally { clearTimeout(timer); }
        const verified = acceptedProviderReceipt(result);
        if (row.action_receipts.some(saved => saved.provider_message_id === verified.provider_message_id)) throw new Error('duplicate_provider_receipt');
        row.action_receipts.push({ index, ...verified });
        this.persist(row);
      }
      const receipt: DeliveryReceipt = { transport_id: 'wppconnect:' + digest([this.tenant, row.delivery_id]),
        provider: 'wppconnect', delivery_id: row.delivery_id, sender: row.sender, message_id: row.message_id,
        action_receipts: row.action_receipts };
      row.receipt = receipt; row.status = 'sent'; this.persist(row);
      return receipt;
    } catch { throw new SinkError(409, 'delivery_uncertain'); }
  }
  async close() {
    this.closed = true;
    await Promise.allSettled(this.running.values());
    closeSync(this.lock); unlinkSync(this.lockPath);
  }
}

export function createRestaurantSinkServer(journal: RestaurantDeliveryJournal, secret: string, maxBodyBytes = 112 * 1024 * 1024): Server {
  if (secret.length < 32 || !Number.isInteger(maxBodyBytes) || maxBodyBytes < 1024 || maxBodyBytes > 112 * 1024 * 1024) throw new Error('sink_configuration_invalid');
  let readers = 0;
  const server = createServer(async (req, res) => {
    const reply = (status: number, body: unknown) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(body)); };
    const actual = Buffer.from(String(req.headers['x-restaurant-sink-key'] || '')), expected = Buffer.from(secret);
    // Dedicated server: no CORS, optional auth, or shared unbounded JSON middleware.
    if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) return reply(401, { error: 'unauthorized' });
    if (req.headers.origin || req.headers['sec-fetch-site']) return reply(403, { error: 'server_to_server_only' });
    if (req.method === 'GET' && /^\/deliveries\/[A-Za-z0-9_.:-]{1,200}$/.test(req.url || '')) {
      try { return reply(200, journal.inspect(req.url!.slice('/deliveries/'.length))); }
      catch (e) { const error = e instanceof SinkError ? e : new SinkError(503, 'sink_unavailable'); return reply(error.status, { error: error.code }); }
    }
    if (req.method !== 'POST' || req.url !== '/deliveries') return reply(404, { error: 'not_found' });
    if (req.headers['content-type']?.split(';')[0].trim() !== 'application/json') return reply(415, { error: 'json_required' });
    if (readers >= 2) return reply(429, { error: 'sink_busy' });
    if (Number(req.headers['content-length']) > maxBodyBytes) return reply(413, { error: 'body_too_large' });
    readers++;
    try {
      const chunks: Buffer[] = []; let bytes = 0;
      for await (const chunk of req) { bytes += chunk.length; if (bytes > maxBodyBytes) throw new SinkError(413, 'body_too_large'); chunks.push(chunk); }
      let body: unknown;
      try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw new SinkError(422, 'invalid_json'); }
      reply(200, { receipt: await journal.deliver(body) });
    } catch (e) { const error = e instanceof SinkError ? e : new SinkError(503, 'sink_unavailable'); reply(error.status, { error: error.code }); }
    finally { readers--; }
  });
  server.requestTimeout = 120000; server.headersTimeout = 10000;
  return server;
}

/** Pure configuration read. No dotenv, sessions, providers or network side effects. */
export function restaurantSinkConfiguration(env: NodeJS.ProcessEnv) {
  if (env.RESTAURANT_QR_SINK_ENABLED !== 'true') return null;
  const tenant = env.RESTAURANT_QR_TENANT || '', secret = env.RESTAURANT_QR_SINK_KEY || '';
  const directory = env.RESTAURANT_QR_JOURNAL_DIR || '', port = Number(env.RESTAURANT_QR_SINK_PORT || 8791);
  if (!/^[A-Za-z0-9_-]{1,80}$/.test(tenant) || secret.length < 32 || !isAbsolute(directory)
      || !Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('sink_configuration_invalid');
  return { tenant, secret, directory, port, host: '127.0.0.1' as const };
}

export async function startRestaurantSink(gateway: { sendRestaurantAction: ReceiptTransport }, env: NodeJS.ProcessEnv) {
  const config = restaurantSinkConfiguration(env);
  if (!config) return null;
  const journal = new RestaurantDeliveryJournal(config.tenant, config.directory, gateway.sendRestaurantAction.bind(gateway));
  const server = createRestaurantSinkServer(journal, config.secret);
  try { await new Promise<void>((ready, reject) => { server.once('error', reject); server.listen(config.port, config.host, ready); }); }
  catch (error) { await journal.close(); throw error; }
  return { server, journal, close: async () => { await new Promise<void>((resolveClose, reject) => server.close(e => e ? reject(e) : resolveClose())); await journal.close(); } };
}
