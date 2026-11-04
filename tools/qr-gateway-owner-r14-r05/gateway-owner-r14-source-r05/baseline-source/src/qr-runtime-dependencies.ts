// Dependencies belong to the QR connector package, independently of the
// developer's Node, package manager, working directory or Codex installation.
import { createRequire } from 'node:module';
import type { InstalledQrBindings } from './native-profile-bootstrap.js';

const runtimeRequire = createRequire(new URL('../qr-direct-runtime/package.json', import.meta.url));
function need(ok: unknown, code: string): asserts ok { if (!ok) throw Error(code); }

export function resolveQrListenHost(input: unknown): '127.0.0.1' | '0.0.0.0' {
  if (input === undefined) return '127.0.0.1';
  need(input === '127.0.0.1' || input === '0.0.0.0', 'QR_LISTEN_HOST');
  return input;
}

export async function loadDirectQrDependencies() {
  const providerUrl = new URL('../qr-direct-runtime/node_modules/@builderbot/provider-baileys/dist/index.mjs', import.meta.url);
  const { BaileysProvider } = await import(providerUrl.href);
  const qrImage = runtimeRequire('qr-image') as { imageSync: (raw: string, options: object) => Buffer };
  need(typeof BaileysProvider === 'function' && typeof qrImage.imageSync === 'function', 'QR_RUNTIME_DIRECT_EXPORTS');
  return { BaileysProvider, qrImage };
}

// Importing these utilities creates neither an Agent nor a WhatsApp socket.
export async function loadInstalledQrBindings(): Promise<InstalledQrBindings> {
  const { Agent } = runtimeRequire('undici');
  const mediaUrl = new URL('../qr-direct-runtime/node_modules/baileys/lib/Utils/messages-media.js', import.meta.url);
  const idsUrl = new URL('../qr-direct-runtime/node_modules/baileys/lib/Utils/generics.js', import.meta.url);
  const [{ getMediaKeys }, { generateMessageIDV2 }] = await Promise.all([import(mediaUrl.href), import(idsUrl.href)]);
  need([Agent, getMediaKeys, generateMessageIDV2, globalThis.fetch].every(f => typeof f === 'function'), 'NATIVE_BOOTSTRAP_INSTALLED_EXPORTS');
  return { Agent, getMediaKeys, fetch: globalThis.fetch, createMessageId: account => generateMessageIDV2(account) };
}
