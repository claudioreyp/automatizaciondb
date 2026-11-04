import type { NativeProfileEndpoints } from './native-profile-fetch.mjs';
export interface NativeProfileRequestGroup {
  text(input: any): Promise<any>; mailbox(input: any): Promise<any>;
  reserve(input: any): Promise<any>; validate(input: any): Promise<any>; complete(input: any): Promise<any>;
  unsupported(input: any): Promise<any>;
  close(): Promise<void>;
  inspectMetadata(): { enabled: boolean; status: string; active: number; wire_pending: number };
}
export function createNativeProfileRequests(options?: { enabled?: false } | {
  enabled: true; endpoints: NativeProfileEndpoints;
  fetchImpl?: (...args: any[]) => Promise<any>; cleanupTimeoutMs?: number;
}): NativeProfileRequestGroup;
