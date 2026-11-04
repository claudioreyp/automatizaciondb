// Private transport metadata only. No provider, POS API, model, token lookup,
// message callback or environment search. Activation requires an explicit process
// flag together with an enabled private profile; loading OFF remains dormant.
import { createHash } from "node:crypto";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import type { DirectNativeConfig } from "./direct-native-transport.js";
import { createNativeProfileFetch, type NativeProfileEndpoints } from "./native-profile-fetch.mjs";

export interface StagedNativeProfile {
  schema: "escalarai.impulsa-native-runtime.v1";
  enabled: false;
  native: DirectNativeConfig;
  endpoints: NativeProfileEndpoints;
  credential_file: string;
  state_directory: string;
}
export interface LoadedStagedNativeProfile {
  profile: Readonly<StagedNativeProfile>;
  fingerprint: string;
  status: "STAGED_OFF";
}
const ORIGIN = "https://n8n.srv1547104.hstgr.cloud";
function need(ok: unknown, code: string): asserts ok { if (!ok) throw Error(code); }
function exact(value: any, keys: string[], code: string) {
  need(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join("|") === [...keys].sort().join("|"), code);
}
function inside(path: string, root: string) {
  const rel = relative(root, path);
  return rel.length > 0 && !isAbsolute(rel) && rel !== ".." && !rel.startsWith(".." + sep);
}
function freeze<T>(value: T): Readonly<T> {
  if (value && typeof value === "object") { for (const child of Object.values(value)) freeze(child); Object.freeze(value); }
  return value;
}
export function validateStagedNativeProfile(input: unknown, sessionDirectory: string): Readonly<StagedNativeProfile> {
  need(typeof sessionDirectory === "string" && isAbsolute(sessionDirectory), "NATIVE_PROFILE_PRIVATE_DIRECTORY");
  const p = structuredClone(input) as StagedNativeProfile;
  exact(p, ["schema", "enabled", "native", "endpoints", "credential_file", "state_directory"], "NATIVE_PROFILE_FIELDS");
  need(p.schema === "escalarai.impulsa-native-runtime.v1", "NATIVE_PROFILE_SCHEMA");
  // Preserve the OFF-only validator for callers that prepare staged profiles.
  need(p.enabled === false, "NATIVE_PROFILE_ACTIVATION_NOT_AVAILABLE");
  const c = p.native;
  exact(c, ["https_origin", "tenant", "scope", "gateway_id", "account_jid", "main_workflow_id", "main_manifest", "mailbox_workflow_id", "mailbox_manifest", "mailbox_table_id"], "NATIVE_PROFILE_NATIVE_FIELDS");
  exact(c.scope, ["business_id", "branch_id"], "NATIVE_PROFILE_SCOPE_FIELDS");
  need(Object.values(c.scope).every(v => Number.isSafeInteger(v) && v > 0), "NATIVE_PROFILE_SCOPE");
  need(c.https_origin === ORIGIN && typeof c.tenant === "string" && typeof c.gateway_id === "string" && typeof c.account_jid === "string" && /^[A-Za-z0-9_-]{1,80}$/.test(c.tenant)
    && !c.tenant.includes("__UNBOUND") && /^[A-Za-z0-9_.:-]{1,160}$/.test(c.gateway_id)
    && !c.gateway_id.includes("__UNBOUND") && /^[1-9][0-9]{7,14}@s\.whatsapp\.net$/.test(c.account_jid), "NATIVE_PROFILE_ACCOUNT_ORIGIN");
  const resources = [c.main_workflow_id, c.mailbox_workflow_id, c.mailbox_table_id];
  need(resources.every(v => typeof v === "string" && /^[A-Za-z0-9]{8,32}$/.test(v))
    && new Set(resources).size === resources.length, "NATIVE_PROFILE_NATIVE_RESOURCES");
  need([c.main_manifest, c.mailbox_manifest].every(v => typeof v === "string" && /^[a-f0-9]{64}$/.test(v)), "NATIVE_PROFILE_MANIFESTS");
  // Reuse the seven-route validator; this constructor performs no I/O and this
  // callback is never called while validating the staged configuration.
  createNativeProfileFetch({ enabled: true, endpoints: p.endpoints, fetchImpl: async () => { throw Error("NATIVE_PROFILE_VALIDATOR_NO_NETWORK"); } });
  need(p.endpoints.origin === c.https_origin, "NATIVE_PROFILE_ENDPOINT_ORIGIN");
  const directory = resolve(sessionDirectory);
  need(typeof p.state_directory === "string" && isAbsolute(p.state_directory)
    && inside(resolve(p.state_directory), directory), "NATIVE_PROFILE_STATE_OUTSIDE_PRIVATE_DIRECTORY");
  need(typeof p.credential_file === "string" && isAbsolute(p.credential_file)
    && inside(resolve(p.credential_file), dirname(directory))
    && resolve(p.credential_file) !== resolve(p.state_directory), "NATIVE_PROFILE_CREDENTIAL_REFERENCE");
  return freeze(p);
}
export type NativeRuntimeProfile = Omit<StagedNativeProfile, "enabled"> & { enabled: boolean };
export interface LoadedNativeRuntimeProfile {
  profile: Readonly<NativeRuntimeProfile>;
  fingerprint: string;
  status: "STAGED_OFF" | "ENABLED";
}

/** The private file alone cannot activate processing. The process owner must
 * supply --activate-native as well. All scope, account, route, path and manifest
 * checks continue through the same staged validator. */
export function validateNativeRuntimeProfile(input: unknown, sessionDirectory: string,
    activationRequested = false): Readonly<NativeRuntimeProfile> {
  need(typeof activationRequested === "boolean", "NATIVE_PROFILE_ACTIVATION_BOOLEAN");
  const enabled = (input as { enabled?: unknown } | null)?.enabled;
  need(typeof enabled === "boolean", "NATIVE_PROFILE_ENABLED_BOOLEAN");
  const fixed = validateStagedNativeProfile({ ...(input as object), enabled: false }, sessionDirectory);
  need(!enabled || activationRequested, "NATIVE_PROFILE_ACTIVATION_NOT_REQUESTED");
  need(!activationRequested || enabled, "NATIVE_PROFILE_ENABLED_FILE_REQUIRED");
  return freeze({ ...fixed, enabled });
}

export function loadNativeRuntimeProfile(profileFile: string | undefined, sessionDirectory: string,
    activationRequested = false): LoadedNativeRuntimeProfile | null {
  // No profile or secret-directory search on the default pairing-only launch.
  if (profileFile === undefined) {
    need(!activationRequested, "NATIVE_PROFILE_ACTIVATION_REQUIRES_FILE");
    return null;
  }
  need(typeof profileFile === "string" && isAbsolute(profileFile), "NATIVE_PROFILE_ABSOLUTE_FILE_REQUIRED");
  need(isAbsolute(sessionDirectory) && inside(resolve(profileFile), resolve(sessionDirectory)), "NATIVE_PROFILE_FILE_OUTSIDE_PRIVATE_DIRECTORY");
  const stat = lstatSync(profileFile);
  need(stat.isFile() && !stat.isSymbolicLink() && stat.size > 0 && stat.size <= 65536, "NATIVE_PROFILE_REGULAR_BOUNDED_FILE_REQUIRED");
  need(inside(realpathSync(profileFile), realpathSync(sessionDirectory)), "NATIVE_PROFILE_LINK_ESCAPE");
  const raw = readFileSync(profileFile);
  need(raw.length <= 65536, "NATIVE_PROFILE_FILE_GREW");
  let parsed: unknown;
  try { parsed = JSON.parse(raw.toString("utf8")); } catch { throw Error("NATIVE_PROFILE_INVALID_JSON"); }
  const profile = validateNativeRuntimeProfile(parsed, sessionDirectory, activationRequested);
  return { profile, fingerprint: createHash("sha256").update(raw).digest("hex"), status: profile.enabled ? "ENABLED" : "STAGED_OFF" };
}

export function loadStagedNativeProfile(profileFile: string | undefined, sessionDirectory: string): LoadedStagedNativeProfile | null {
  const loaded = loadNativeRuntimeProfile(profileFile, sessionDirectory, false);
  return loaded === null ? null : { ...loaded, profile: loaded.profile as Readonly<StagedNativeProfile>, status: "STAGED_OFF" };
}
