import { createHash } from 'node:crypto';
import type { N8nBridgeResponse, N8nInboundPayload } from './types.js';

export function mergeTurn(messages: N8nInboundPayload[]): N8nInboundPayload {
  const last = messages[messages.length - 1];
  const records = messages.flatMap(p => p.context.messages || [{ id: p.message.id, text: p.message.text, type: p.message.type }]);
  const ids = records.map(m => m.id);
  return { ...last, message: { ...last.message, text: messages.map(p => p.message.text).filter(Boolean).join('\n') },
    context: { ...last.context, batched: records.length > 1, messageCount: records.length, messageIds: ids,
      messages: records, turnId: createHash('sha256').update(JSON.stringify([last.tenantId, last.from, ids])).digest('hex') } };
}

export function obsoleteInformation(response: N8nBridgeResponse, turn: N8nInboundPayload, superseded: boolean): boolean {
  return superseded && Boolean(turn.context.turnId) && response.turn?.id === turn.context.turnId
    && response.turn?.outcome === 'none';
}

// WhatsApp displays each action separately. Keep its text conversational even
// when an upstream workflow returns no emoji or several of them.
export function withSingleEmoji(value: string): string {
  const segments = [...new Intl.Segmenter('es', { granularity: 'grapheme' }).segment(value)].map(part => part.segment);
  const isEmoji = (part: string) => /\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20E3/u.test(part);
  const originals = segments.filter(isEmoji);
  const body = segments.filter(part => !isEmoji(part)).join('').replace(/[ \t]{2,}/g, ' ').trim();
  const topic = value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const needsApology = /(?:no pude|no pudimos|lo siento|disculp|molesti|error|problema|fall[ao]|no se pudo|intenta de nuevo|reenvia)/.test(topic);
  const needsCare = /(?:por seguridad|revision|revisar|revisaremos|revisara|comprobaremos)/.test(topic);
  const emoji = needsApology ? (originals.find(item => ['🙏', '😔', '😥'].includes(item)) || '🙏')
    : needsCare ? (originals.find(item => ['🔒', '🧾', '✅'].includes(item)) || '🔒')
      : originals[0] || (/(?:pago|yape|comprobante|captura)/.test(topic) ? '🧾'
        : /(?:ubicacion|direccion|delivery|repartidor|camino)/.test(topic) ? '📍'
          : /(?:carta|menu|plato|pizza|pedido|preparac)/.test(topic) ? '🍕' : '🙂');
  return body ? `${body} ${emoji}` : emoji;
}

export class InboundMessageIds {
  private readonly seen = new Map<string, number>();

  has(tenantId: string, messageId: string, now = Date.now()): boolean {
    const previous = this.seen.get(JSON.stringify([tenantId, messageId]));
    return previous !== undefined && now - previous <= 5 * 60 * 1000;
  }

  // Call only after inbound media has been materialized, so an empty listener
  // copy cannot consume the ID of a later complete polling copy.
  isDuplicate(tenantId: string, messageId: string, now = Date.now()): boolean {
    const key = JSON.stringify([tenantId, messageId]);
    const duplicate = this.has(tenantId, messageId, now);
    this.seen.set(key, now);
    for (const [oldKey, recordedAt] of this.seen) {
      if (now - recordedAt > 5 * 60 * 1000) this.seen.delete(oldKey);
    }
    return duplicate;
  }
}

type QueueState = { pending: N8nInboundPayload[]; running: boolean; lastTextAt: number; timer?: ReturnType<typeof setTimeout> };

export class OrderedPreparation {
  private pending = new Map<string, Promise<void>>();
  async run(key: string, prepare: () => Promise<void>): Promise<void> {
    const previous = this.pending.get(key) || Promise.resolve();
    const current = previous.catch(() => undefined).then(prepare);
    this.pending.set(key, current);
    try { await current; }
    finally { if (this.pending.get(key) === current) this.pending.delete(key); }
  }
}

// Media/location form barriers. Only contiguous waiting texts may become one turn.
export class ConversationQueue {
  private states = new Map<string, QueueState>();
  constructor(private silenceMs: number,
    private process: (payload: N8nInboundPayload, superseded: () => boolean) => Promise<void>,
    private onError: (error: unknown, payload: N8nInboundPayload) => void = () => undefined) {}

  push(key: string, payload: N8nInboundPayload): void {
    const state = this.states.get(key) || { pending: [], running: false, lastTextAt: 0 };
    this.states.set(key, state);
    state.pending.push(payload);
    if (payload.message.type === 'text') state.lastTextAt = Date.now();
    this.schedule(key, state);
  }

  count(tenant: string): number {
    return [...this.states.values()].filter(s => s.pending.some(p => p.tenantId === tenant)).length;
  }

  clear(tenant: string): void {
    for (const [key, state] of this.states) {
      if (!key.startsWith(tenant + ':')) continue;
      if (state.timer) clearTimeout(state.timer);
      state.pending = [];
      if (!state.running) this.states.delete(key);
    }
  }

  private schedule(key: string, state: QueueState): void {
    if (state.timer) clearTimeout(state.timer);
    if (state.running || !state.pending.length) return;
    const onlyTexts = state.pending.every(p => p.message.type === 'text');
    const delay = onlyTexts ? Math.max(0, state.lastTextAt + this.silenceMs - Date.now()) : 0;
    state.timer = setTimeout(() => { void this.drain(key, state); }, delay);
  }

  private async drain(key: string, state: QueueState): Promise<void> {
    if (state.running || !state.pending.length) return;
    const first = state.pending.shift()!;
    const group = [first];
    if (first.message.type === 'text') {
      while (state.pending[0]?.message.type === 'text') group.push(state.pending.shift()!);
    }
    const turn = mergeTurn(group);
    state.running = true;
    try { await this.process(turn, () => state.pending.length > 0); }
    catch (error) { this.onError(error, turn); }
    finally {
      state.running = false;
      if (state.pending.length) this.schedule(key, state);
      else this.states.delete(key);
    }
  }
}
