export function createNativeQueuedUnsupportedClient(options?: {enabled?: false} | {
  enabled:true;config:{https_origin:string;tenant:string;account_jid:string;scope:{business_id:number;branch_id:number}};
  readCredential():unknown|Promise<unknown>;requestHTTPS(input:any):Promise<any>;timeoutMs?:number;
}): {recordUnsupported(input:any):Promise<any>;close():Promise<void>};
