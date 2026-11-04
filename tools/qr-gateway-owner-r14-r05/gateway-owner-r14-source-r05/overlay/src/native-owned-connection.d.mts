import type { DirectNativeConfig, DirectNativeSocket, DirectNativeTransport } from './direct-native-transport.js';
export function createNativeOwnedConnection(options?: {enabled?: false} | {
  enabled:true; drainAck?:(input:{target:Record<string,unknown>;isCurrent:()=>boolean;signal:AbortSignal})=>Promise<unknown>; config:DirectNativeConfig; directory:string;
  readCredential():{header_name:string;header_value:string}|Promise<{header_name:string;header_value:string}>;
  createMessageId():string; isCurrentSocket(socket:DirectNativeSocket):boolean;
  installed:{Agent:new(...args:any[])=>any;getMediaKeys:(...args:any[])=>any;fetch:(...args:any[])=>any};
  callbacks:{enqueue(input:any):unknown;recordUnsupported(input:any):unknown};
  requestHTTPS:{mailbox(input:any):Promise<any>;reserve(input:any):Promise<any>;validate(input:any):Promise<any>;complete(input:any):Promise<any>};
}):DirectNativeTransport;
