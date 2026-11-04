export interface NativeProfileEndpoints {
  origin: string; text: string; pull: string; authorize: string; observe: string;
  reserve: string; validate: string; complete: string;
  unsupported?: string;
}
export function createNativeProfileFetch(options?: { enabled?: false } | {
  enabled: true; endpoints: NativeProfileEndpoints; fetchImpl: (...args: any[]) => Promise<any>;
}): (...args: any[]) => Promise<any>;
