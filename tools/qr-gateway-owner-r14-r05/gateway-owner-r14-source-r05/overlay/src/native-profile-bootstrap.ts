// Private process composition. The staged file loader still rejects activation.
// OFF never loads media dependencies, reads a header or starts a connection.
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { createNativeProfileConnection, type NativeProfileConnectionOptions } from './native-profile-connection.js';
import { validateStagedNativeProfile, type StagedNativeProfile } from './native-runtime-profile.js';
import type { DirectNativeSocket, DirectNativeTransport } from './direct-native-transport.js';
export { loadInstalledQrBindings } from './qr-runtime-dependencies.js';

export type NativeBootstrapProfile = Omit<StagedNativeProfile, 'enabled'> & { enabled: boolean };
type EnabledConnection = Extract<NativeProfileConnectionOptions, { enabled: true }>;
export type InstalledQrBindings = EnabledConnection['installed'] & { createMessageId(accountJid: string): string };
export type NativeBootstrapOptions = {
  profile: NativeBootstrapProfile | null;
  sessionDirectory: string;
  getProvider(): { vendor?: DirectNativeSocket; isCleaned?: boolean } | undefined;
  loadInstalled(): Promise<InstalledQrBindings>;
};
function need(ok: unknown, code: string): asserts ok { if (!ok) throw Error(code); }
function inside(path: string, root: string) {
  const rel = relative(root, path);
  return rel.length > 0 && !isAbsolute(rel) && rel !== '..' && !rel.startsWith('..' + sep);
}

// The registered file also contains routing metadata. Clients accept only the
// two header fields; metadata is never forwarded as a credential or payload.
export function readNativeGatewayCredential(file: string, privateRoot: string) {
  need(isAbsolute(file) && isAbsolute(privateRoot) && inside(resolve(file), resolve(privateRoot)), 'NATIVE_BOOTSTRAP_CREDENTIAL_PATH');
  const stat = lstatSync(file);
  need(stat.isFile() && !stat.isSymbolicLink() && stat.size > 0 && stat.size <= 65536
    && inside(realpathSync(file), realpathSync(privateRoot)), 'NATIVE_BOOTSTRAP_CREDENTIAL_FILE');
  const raw = readFileSync(file);
  need(raw.length > 0 && raw.length <= 65536, 'NATIVE_BOOTSTRAP_CREDENTIAL_SIZE');
  let value: any;
  try { value = JSON.parse(raw.toString('utf8')); } catch { throw Error('NATIVE_BOOTSTRAP_CREDENTIAL_JSON'); }
  need(value && typeof value === 'object' && !Array.isArray(value)
    && value.header_name === 'X-Escalar-Native-Gateway' && typeof value.header_value === 'string'
    && value.header_value.length > 0 && value.header_value.length <= 4096 && !/[\r\n\0]/.test(value.header_value), 'NATIVE_BOOTSTRAP_CREDENTIAL_FIELDS');
  return { header_name: value.header_name as string, header_value: value.header_value as string };
}

export function createNativeProfileBootstrapComposer(connect: typeof createNativeProfileConnection) {
  return async function bootstrap(options: NativeBootstrapOptions): Promise<DirectNativeTransport> {
    const profile = options.profile;
    if (profile === null || profile.enabled === false) return connect({ enabled: false });
    need(profile.enabled === true, 'NATIVE_BOOTSTRAP_ENABLED_BOOLEAN');
    // Share every existing scope/path/manifest validation. This explicit process
    // seam does not change the staged loader's ACTIVATION_NOT_AVAILABLE rule.
    const fixed = validateStagedNativeProfile({ ...profile, enabled: false }, options.sessionDirectory);
    need(typeof fixed.endpoints.unsupported === 'string', 'NATIVE_BOOTSTRAP_UNSUPPORTED_ROUTE');
    need(typeof options.getProvider === 'function' && typeof options.loadInstalled === 'function', 'NATIVE_BOOTSTRAP_PRIVATE_BINDINGS');
    const getProvider = options.getProvider, privateRoot = dirname(resolve(options.sessionDirectory));
    const installed = await options.loadInstalled();
    need(installed && [installed.Agent, installed.getMediaKeys, installed.fetch, installed.createMessageId].every(f => typeof f === 'function'), 'NATIVE_BOOTSTRAP_INSTALLED_BINDINGS');
    const createMessageId = installed.createMessageId;
    return connect({ enabled: true, ackOwnerDrain:true, config: fixed.native, endpoints: { ...fixed.endpoints, unsupported: fixed.endpoints.unsupported },
      directory: fixed.state_directory, installed,
      readCredential: () => readNativeGatewayCredential(fixed.credential_file, privateRoot),
      createMessageId: () => createMessageId(fixed.native.account_jid),
      isCurrentSocket: socket => { const owner = getProvider(); return !!owner && owner.vendor === socket && !owner.isCleaned; },
    });
  };
}
export const createNativeProfileBootstrap = createNativeProfileBootstrapComposer(createNativeProfileConnection);
