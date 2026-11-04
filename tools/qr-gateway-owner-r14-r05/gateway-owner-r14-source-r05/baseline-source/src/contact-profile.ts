/** WhatsApp profile display data. Never use it as an instruction or identity key. */
export function normalizeContactDisplayName(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const name = value.normalize('NFC')
    .replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '')
    .replace(/\s+/gu, ' ').trim();
  return name && [...name].length <= 120 ? name : null;
}

export function providerContactDisplayName(raw: Record<string, unknown>): string | null {
  // Polling supplies an explicit profile value, including null when unavailable.
  // Its legacy name/notifyName can be an address-book label and is not a fallback.
  if (Object.hasOwn(raw, 'contact_display_name')) return normalizeContactDisplayName(raw.contact_display_name);
  const sender = raw.sender && typeof raw.sender === 'object' ? raw.sender as Record<string, unknown> : {};
  for (const value of [sender.pushname, sender.pushName, raw.pushname, raw.pushName, raw.notifyName]) {
    const name = normalizeContactDisplayName(value);
    if (name) return name;
  }
  return null;
}
