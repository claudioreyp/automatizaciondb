import {createHash} from 'node:crypto';
import {createAckTargetTools,canonicalJSON,bindingFromConfig} from './ack-target-tools.mjs';
import {recoverAckWithinOriginalLimits} from './native-ack-quiet.mjs';

const ACK_URL='https://n8n.srv1547104.hstgr.cloud/webhook/escalar-native-baileys-c450/ack-once';
const need=(ok,code)=>{if(!ok)throw Error(code);};
const sha=text=>createHash('sha256').update(text).digest('hex');
const cancellableWait=(ms,signal)=>new Promise((resolve,reject)=>{
  let timer;const stop=()=>{clearTimeout(timer);signal.removeEventListener('abort',stop);reject(Error('ACK_OWNER_INVALIDATED'));};
  if(signal.aborted)return stop();signal.addEventListener('abort',stop,{once:true});
  timer=setTimeout(()=>{signal.removeEventListener('abort',stop);resolve();},ms);
});

/** Private owner orchestration; no provider/model/sink dispatch and no authority. */
export function createNativeAckOwnerDrain(options={}){
  if(options.enabled!==true)return {run:async()=>{throw Error('ACK_OWNER_OFF');},close:async()=>{},inspectMetadata:()=>({enabled:false,status:'OFF',active:false})};
  const {config,readCredential,requestHTTPS}=options;
  need(config?.https_origin==='https://n8n.srv1547104.hstgr.cloud'&&typeof readCredential==='function'&&typeof requestHTTPS==='function','ACK_OWNER_BINDINGS');
  const tools=createAckTargetTools({sha256:sha,canonicalJSON,binding:bindingFromConfig(config)});
  const now=options.readClock??Date.now,wait=options.wait??cancellableWait;
  need(typeof now==='function'&&typeof wait==='function','ACK_OWNER_CLOCK_PORTS');
  let closed=false,active=null,controller=null,status='IDLE',lastCompleted=null;
  function run({target,isCurrent,signal}={}){
    need(!closed&&typeof isCurrent==='function'&&isCurrent(),'ACK_OWNER_INVALIDATED');
    need(signal&&typeof signal.addEventListener==='function'&&typeof signal.removeEventListener==='function'&&!signal.aborted,'ACK_OWNER_SIGNAL');
    need(active===null,'ACK_OWNER_SERIAL');
    const reference=tools.targetOf(target),key=sha(canonicalJSON(reference));
    if(lastCompleted===key)return Promise.resolve({status:'ACK_RECOVERY_COMPLETE_DURABLE',ticks:0,provider_calls:0,model_calls:0});
    const started=now();need(Number.isSafeInteger(started)&&started>0,'ACK_OWNER_CLOCK');
    const deadline=started+600000,localController=new AbortController();controller=localController;status='DRAINING';
    const abort=()=>localController.abort();signal.addEventListener('abort',abort,{once:true});
    const fence=()=>need(!closed&&!localController.signal.aborted&&isCurrent()&&now()>=started&&now()<deadline,'ACK_OWNER_INVALIDATED');
    const work=Promise.resolve().then(async()=>{
      const row={started_at_ms:started,latency:{}};
      await recoverAckWithinOriginalLimits({row,limits:{ack_ticks:3,total_ms:600000},
        spec:{complete_statuses:['ACK_RECOVERY_COMPLETE_DURABLE'],idle_statuses:['ACK_RECOVERY_NO_CANDIDATE']},
        control:async role=>{
          fence();need(role==='ack','ACK_OWNER_ROLE');const credential=await readCredential();fence();
          const reply=await requestHTTPS({url:ACK_URL,method:'POST',body:{ack_target:reference},credential,
            timeoutMs:Math.min(30000,deadline-now()),maxResponseBytes:65536,redirect:'error',signal:localController.signal});
          fence();need(reply?.statusCode===200&&reply.body&&typeof reply.body.status==='string','ACK_OWNER_RESPONSE');
          return {value:{status:reply.body.status}};
        },wait:ms=>wait(ms,localController.signal),now,deadline,fence,spacingMs:1000});
      fence();lastCompleted=key;status='DURABLE';return {status:'ACK_RECOVERY_COMPLETE_DURABLE',ticks:row.ack_ticks.length,provider_calls:0,model_calls:0};
    });
    const clear=()=>{signal.removeEventListener('abort',abort);if(active===work){active=null;controller=null;}};
    active=work;void work.then(clear,()=>{if(!closed)status='STOPPED_NO_RETRY';clear();});
    return work;
  }
  function close(){closed=true;status='CLOSED';controller?.abort();return Promise.resolve(active).then(()=>{},()=>{});}
  return {run,close,inspectMetadata:()=>({enabled:true,status,active:active!==null})};
}
