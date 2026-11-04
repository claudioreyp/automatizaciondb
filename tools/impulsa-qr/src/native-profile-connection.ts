import { isAbsolute } from 'node:path';
import { createDirectNativeTransport, type DirectNativeConfig, type DirectNativeSocket, type DirectNativeTransport } from './direct-native-transport.js';
import { createNativeOwnedConnection } from './native-owned-connection.mjs';
import { createNativeProfileRequests } from './native-profile-requests.mjs';
import { createNativeTextIngressClient } from './native-text-ingress-client.js';
import { createNativeUnsupportedClient } from './native-unsupported-client.mjs';
import { createNativeQueuedUnsupportedClient } from './native-queued-unsupported-client.mjs';
import type { NativeProfileEndpoints } from './native-profile-fetch.mjs';

type Credential = { header_name: string; header_value: string };
export type NativeProfileConnectionOptions = {enabled?:false} | {
  enabled:true; config:DirectNativeConfig; endpoints:NativeProfileEndpoints & {unsupported:string}; directory:string;
  readCredential():Credential|Promise<Credential>;createMessageId():string;isCurrentSocket(socket:DirectNativeSocket):boolean;
  installed:{Agent:new(...args:any[])=>any;getMediaKeys:(...args:any[])=>any;fetch:(...args:any[])=>any};
  fetchImpl?:(...args:any[])=>Promise<any>;
};
function need(v:unknown,c:string):asserts v{if(!v)throw Error(c);}
// The factories are module dependencies, never request fields. This seam tests
// ownership/close behavior without starting the closed mailbox ACK scenario.
export function createNativeProfileConnectionComposer(factories: {
  requests:typeof createNativeProfileRequests; text:typeof createNativeTextIngressClient;
  unsupported:typeof createNativeUnsupportedClient; owner:typeof createNativeOwnedConnection;
  queuedUnsupported?:typeof createNativeQueuedUnsupportedClient;
}) {
  return function createProfileConnection(options:NativeProfileConnectionOptions={}):DirectNativeTransport {
    if(options.enabled!==true)return createDirectNativeTransport();
    const cfg=structuredClone(options.config),directory=options.directory;
    need(cfg&&cfg.https_origin==='https://n8n.srv1547104.hstgr.cloud'&&typeof directory==='string'&&isAbsolute(directory)
      && typeof options.endpoints?.unsupported==='string'&&[options.readCredential,options.createMessageId,options.isCurrentSocket].every(f=>typeof f==='function')
      && options.installed&&[options.installed.Agent,options.installed.getMediaKeys,options.installed.fetch].every(f=>typeof f==='function'),'PROFILE_CONNECTION_PRIVATE_BINDINGS');
    const readCredential=options.readCredential;
    let requests!:ReturnType<typeof createNativeProfileRequests>,text!:ReturnType<typeof createNativeTextIngressClient>,
      unsupported!:ReturnType<typeof createNativeUnsupportedClient>,owner!:DirectNativeTransport;
    try {
      requests=factories.requests({enabled:true,endpoints:options.endpoints,...(options.fetchImpl?{fetchImpl:options.fetchImpl}:{})});
      text=factories.text({enabled:true,config:{https_origin:cfg.https_origin,tenant:cfg.tenant,gateway_id:cfg.gateway_id,account_jid:cfg.account_jid},readCredential,requestHTTPS:requests.text});
      const queued=options.endpoints.unsupported.endsWith('/private-unsupported-queue');
      if(queued)need(typeof factories.queuedUnsupported==='function','PROFILE_CONNECTION_QUEUE_CLIENT_REQUIRED');
      unsupported=queued
        ? factories.queuedUnsupported!({enabled:true,config:{https_origin:cfg.https_origin,tenant:cfg.tenant,account_jid:cfg.account_jid,scope:cfg.scope},readCredential,requestHTTPS:requests.unsupported})
        : factories.unsupported({enabled:true,config:{https_origin:cfg.https_origin,tenant:cfg.tenant,account_jid:cfg.account_jid},readCredential,requestHTTPS:requests.unsupported});
      owner=factories.owner({enabled:true,config:cfg,directory,readCredential,createMessageId:options.createMessageId,isCurrentSocket:options.isCurrentSocket,
        installed:options.installed,callbacks:{enqueue:input=>text.enqueue(input),recordUnsupported:input=>unsupported.recordUnsupported(input)},
        requestHTTPS:{mailbox:requests.mailbox,reserve:requests.reserve,validate:requests.validate,complete:requests.complete}});
    } catch(error) {
      // All these constructors are dormant: no socket, credential reads or wire
      // has started. Still invalidate every successfully constructed dependency.
      const invoke=(fn:()=>unknown)=>{try{return Promise.resolve(fn());}catch(e){return Promise.reject(e);}};
      void Promise.allSettled([invoke(()=>text?.close()),invoke(()=>unsupported?.close()),invoke(()=>requests?.close())]); throw error;
    }
    let closing=false,unconfirmed=false,closed=false,closePromise:Promise<void>|undefined;
    function close():Promise<void> {
      if(closePromise)return closePromise;
      let resolve!:()=>void,reject!:(error:Error)=>void;
      closePromise=new Promise<void>((done,fail)=>{resolve=done;reject=fail;});
      void closePromise.catch(()=>{});closing=true;
      const invoke=(fn:()=>unknown)=>{try{return Promise.resolve(fn());}catch(error){return Promise.reject(error);}};
      // Epoch invalidation and cancellation happen synchronously, before await.
      const work=Promise.allSettled([invoke(()=>owner.close()),invoke(()=>text.close()),invoke(()=>unsupported.close()),invoke(()=>requests.close())]);
      let timer:ReturnType<typeof setTimeout>|undefined;
      void Promise.race([work,new Promise<never>((_,fail)=>{timer=setTimeout(()=>fail(Error('PROFILE_CONNECTION_CLOSE_UNCONFIRMED')),5000);})])
        .then(results=>{if(results.some(r=>r.status==='rejected'))throw Error('PROFILE_CONNECTION_CLOSE_UNCONFIRMED');closed=true;resolve();})
        .catch(()=>{unconfirmed=true;reject(Error('PROFILE_CONNECTION_CLOSE_UNCONFIRMED'));}).finally(()=>clearTimeout(timer));
      return closePromise;
    }
    return {
      observeSocket(socket){if(!closing)owner.observeSocket(socket);},
      observeConnection(socket,update){if(!closing)owner.observeConnection(socket,update);},close,
      inspectMetadata:()=>({...owner.inspectMetadata(),status:unconfirmed?'CLOSE_UNCONFIRMED':closed?'CLOSED':closing?'CLOSING':owner.inspectMetadata().status}),
    };
  };
}
export const createNativeProfileConnection=createNativeProfileConnectionComposer({requests:createNativeProfileRequests,text:createNativeTextIngressClient,unsupported:createNativeUnsupportedClient,queuedUnsupported:createNativeQueuedUnsupportedClient,owner:createNativeOwnedConnection});
