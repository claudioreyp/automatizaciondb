import { validateBinaryMedia } from './binary-media.js';
import type { BinaryMediaAction, TextAction } from './types.js';

export type RestaurantAction = TextAction | BinaryMediaAction;
export type ProviderReceipt = { provider: 'wppconnect'; provider_message_id: string; ack?: number };

/** Individual provider identities only; an opaque LID is never a phone number. */
export function individualRecipient(value: unknown): string {
  if (typeof value !== 'string') throw new Error('invalid_recipient');
  const id = value.trim().replace(/^\+/, '').toLowerCase().replace(/@s\.whatsapp\.net$/, '@c.us');
  if (/^\d{6,20}@lid$/.test(id)) return id;
  if (/^\d{8,15}(?:@c\.us)?$/.test(id)) return id.replace(/@c\.us$/, '');
  throw new Error('invalid_recipient');
}

export function restaurantAction(value: unknown): RestaurantAction {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_action');
  const action = value as Record<string, unknown>;
  if (action.type === 'text' && Object.keys(action).every(k => ['type', 'text'].includes(k))
      && typeof action.text === 'string' && action.text.trim() && action.text.length <= 6000) {
    return { type: 'text', text: action.text };
  }
  if (action.type === 'media_binary' && Object.keys(action).every(k => ['type', 'data', 'mimeType', 'caption'].includes(k))
      && (action.caption === undefined || typeof action.caption === 'string' && action.caption.length <= 1024)) {
    return validateBinaryMedia(action);
  }
  // No URL fetch, arbitrary provider command, implicit menu lookup or remote filename.
  throw new Error('invalid_action');
}

export function providerReceipt(id: unknown, ack?: unknown): ProviderReceipt {
  const serial = typeof id === 'string' ? id : id && typeof id === 'object' ? (id as { _serialized?: unknown })._serialized : null;
  if (typeof serial !== 'string' || !serial.trim() || serial.length > 512 || /[\u0000-\u001f]/.test(serial)) throw new Error('provider_receipt_missing');
  return { provider: 'wppconnect', provider_message_id: serial, ...(typeof ack === 'number' && Number.isFinite(ack) ? { ack } : {}) };
}

/** WPP ACK enum: FAILED=-1, CLOCK=0, SENT=1, RECEIVED=2, READ=3, PLAYED=4, PEER=5.
 * A local message ID alone does not confirm provider acceptance. */
export function acceptedProviderReceipt(value: ProviderReceipt): ProviderReceipt {
  if (value.provider !== 'wppconnect' || !Number.isInteger(value.ack) || value.ack! < 1 || value.ack! > 5) {
    throw new Error('provider_acceptance_unconfirmed');
  }
  return providerReceipt(value.provider_message_id, value.ack);
}
