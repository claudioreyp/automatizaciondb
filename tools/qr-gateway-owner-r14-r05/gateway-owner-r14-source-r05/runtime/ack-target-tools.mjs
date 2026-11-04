import {createHash} from 'node:crypto';

/** Pure selection metadata. This reference never grants dispatch/CAS authority. */
export function canonicalJSON(value){
  if(value===null||['string','boolean'].includes(typeof value))return JSON.stringify(value);
  if(typeof value==='number'&&Number.isFinite(value))return JSON.stringify(value);
  if(!value||typeof value!=='object')throw Error('ACK_TARGET_JSON');
  if(Array.isArray(value))return '['+value.map(canonicalJSON).join(',')+']';
  const descriptors=Object.getOwnPropertyDescriptors(value);
  if(Object.getOwnPropertySymbols(value).length||!Object.values(descriptors).every(d=>Object.hasOwn(d,'value')&&d.enumerable))throw Error('ACK_TARGET_JSON');
  return '{'+Object.keys(descriptors).sort().map(k=>JSON.stringify(k)+':'+canonicalJSON(descriptors[k].value)).join(',')+'}';
}
const sha256=text=>createHash('sha256').update(text).digest('hex');

/** Self-contained factory, also inlined verbatim into the two ACK graphs. */
export function createAckTargetTools({sha256,canonicalJSON,binding}){
  const need=(ok,code)=>{if(!ok)throw Error(code);};
  const exact=(x,keys)=>x&&typeof x==='object'&&!Array.isArray(x)&&Object.keys(x).sort().join('|')===[...keys].sort().join('|');
  const id=x=>typeof x==='string'&&/^[A-Za-z0-9_.:-]{1,200}$/.test(x);
  const delivery=x=>typeof x==='string'&&/^[A-Za-z0-9_-]{1,64}$/.test(x);
  const hash=x=>typeof x==='string'&&/^[a-f0-9]{64}$/.test(x);
  const positive=x=>Number.isSafeInteger(x)&&x>0;
  const same=(a,b)=>canonicalJSON(a)===canonicalJSON(b);
  need(typeof sha256==='function'&&typeof canonicalJSON==='function'&&exact(binding,['tenant','scope','main_workflow_id','main_manifest','mailbox_workflow_id','mailbox_manifest','mailbox_table_id'])
    &&id(binding.tenant)&&exact(binding.scope,['business_id','branch_id'])&&positive(binding.scope.business_id)&&positive(binding.scope.branch_id)
    &&[binding.main_workflow_id,binding.mailbox_workflow_id,binding.mailbox_table_id].every(id)&&[binding.main_manifest,binding.mailbox_manifest].every(hash),'ACK_TARGET_BINDING');
  binding=JSON.parse(canonicalJSON(binding));
  function targetOf(value){
    need(exact(value,['version','delivery_id','message_id','sender','envelope_digest','source_row_id','source_run_id'])&&value.version===1
      &&delivery(value.delivery_id)&&id(value.message_id)&&typeof value.sender==='string'&&/^(?:\d{8,15}|\d{6,20}@lid)$/.test(value.sender)
      &&hash(value.envelope_digest)&&positive(value.source_row_id)&&id(value.source_run_id),'ACK_TARGET_SHAPE');
    return JSON.parse(canonicalJSON(value));
  }
  function parseBody(body){
    need(body&&typeof body==='object'&&!Array.isArray(body),'ACK_TARGET_BODY');
    if(Object.keys(body).length===0)return null;
    need(exact(body,['ack_target']),'ACK_TARGET_BODY');return targetOf(body.ack_target);
  }
  const conditions=entries=>({matchType:'allConditions',conditions:entries.map(([keyName,keyValue])=>({keyName,condition:'eq',keyValue}))});
  function mailboxPlan(raw){
    const t=targetOf(raw),run='qr-mailbox:'+sha256(JSON.stringify([binding.tenant,binding.scope.business_id,binding.scope.branch_id,t.delivery_id]));
    return {operation:'get',table_id:binding.mailbox_table_id,filters:conditions([['workflow_id',binding.mailbox_workflow_id],['code_manifest_sha256',binding.mailbox_manifest],
      ['business_id',binding.scope.business_id],['branch_id',binding.scope.branch_id],['state','sent'],['run_id',run]]),returnAll:false,limit:2};
  }
  function sourcePlan(raw,tableId){
    const t=targetOf(raw);need(id(tableId),'ACK_TARGET_SOURCE_TABLE');
    return {operation:'get',table_id:tableId,filters:conditions([['id',t.source_row_id],['workflow_id',binding.main_workflow_id],['code_manifest_sha256',binding.main_manifest],
      ['business_id',binding.scope.business_id],['branch_id',binding.scope.branch_id],['run_id',t.source_run_id]]),returnAll:false,limit:2};
  }
  function unique(rows,code){need(Array.isArray(rows)&&rows.length===1,code);return rows[0];}
  function validateSource(raw,rows){
    const t=targetOf(raw),r=unique(rows,'ACK_TARGET_SOURCE_NOT_UNIQUE');
    need(r.id===t.source_row_id&&r.run_id===t.source_run_id&&r.workflow_id===binding.main_workflow_id&&r.code_manifest_sha256===binding.main_manifest
      &&r.business_id===binding.scope.business_id&&r.branch_id===binding.scope.branch_id,'ACK_TARGET_SOURCE_BINDING');return r;
  }
  function validateMailbox(raw,rows){
    const t=targetOf(raw),r=unique(rows,'ACK_TARGET_SENT_NOT_UNIQUE'),plan=mailboxPlan(t);
    need(plan.filters.conditions.every(c=>r[c.keyName]===c.keyValue),'ACK_TARGET_SENT_SCOPE');
    const b=JSON.parse(r.body_json),e=b.envelope,s=b.source;
    need(e&&e.delivery_id===t.delivery_id&&e.message_id===t.message_id&&e.sender===t.sender&&b.envelope_digest===t.envelope_digest
      &&s?.row_id===t.source_row_id&&s.run_id===t.source_run_id&&s.workflow_id===binding.main_workflow_id&&s.code_manifest_sha256===binding.main_manifest
      &&s.business_id===binding.scope.business_id&&s.branch_id===binding.scope.branch_id,'ACK_TARGET_SENT_BINDING');return r;
  }
  return {targetOf,parseBody,mailboxPlan,sourcePlan,validateSource,validateMailbox};
}

export function bindingFromConfig(config){
  return Object.fromEntries(['tenant','scope','main_workflow_id','main_manifest','mailbox_workflow_id','mailbox_manifest','mailbox_table_id'].map(k=>[k,config[k]]));
}

/** Validate the owner's saved client state; do not return tokens, nonces or authority. */
export function buildAckTargetFromMetadata({metadata,config,sender,message_id,delivery_id}){
  const need=(ok,code)=>{if(!ok)throw Error(code);};
  const exact=(x,keys)=>x&&typeof x==='object'&&!Array.isArray(x)&&Object.keys(x).sort().join('|')===[...keys].sort().join('|');
  const hash=x=>typeof x==='string'&&/^[a-f0-9]{64}$/.test(x);
  const id=x=>typeof x==='string'&&/^[A-Za-z0-9_.:-]{1,200}$/.test(x);
  const uuid=x=>typeof x==='string'&&/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(x);
  const integer=x=>Number.isSafeInteger(x)&&x>=0;
  const same=(a,b)=>canonicalJSON(a)===canonicalJSON(b);
  const tools=createAckTargetTools({sha256,canonicalJSON,binding:bindingFromConfig(config)});
  need(exact(config,['https_origin','tenant','scope','gateway_id','account_jid','main_workflow_id','main_manifest','mailbox_workflow_id','mailbox_manifest','mailbox_table_id']),'ACK_TARGET_CONFIG');
  need(exact(metadata,['schema','config_sha256','pull','deliveries','checksum']),'ACK_TARGET_METADATA');
  const {checksum,...body}=metadata;
  need(hash(checksum)&&checksum===sha256(canonicalJSON(body))&&metadata.schema===1&&metadata.config_sha256===sha256(canonicalJSON(config))
    &&metadata.pull===null&&Array.isArray(metadata.deliveries)&&metadata.deliveries.length>=1&&metadata.deliveries.length<=10000,'ACK_TARGET_METADATA_INTEGRITY');
  const matches=metadata.deliveries.filter(r=>r.sender===sender&&r.message_id===message_id&&(delivery_id===undefined||r.delivery_id===delivery_id));
  need(matches.length===1,'ACK_TARGET_DELIVERY_NOT_UNIQUE');const r=matches[0],s=r.source;
  need(exact(r,['delivery_id','message_id','sender','source','claim_nonce','envelope_digest','action_count','created_at_ms','status','actions'])
    &&r.status==='ack_durable'&&uuid(r.claim_nonce)&&integer(r.created_at_ms)&&r.created_at_ms>0&&Number.isSafeInteger(r.action_count)&&r.action_count>=1&&r.action_count<=12
    &&Array.isArray(r.actions)&&r.actions.length===r.action_count,'ACK_TARGET_NOT_DURABLE');
  need(exact(s,['row_id','workflow_id','code_manifest_sha256','business_id','branch_id','run_id','effect_id','request_hash','owner_execution','nonce'])
    &&Number.isSafeInteger(s.row_id)&&s.row_id>0&&s.workflow_id===config.main_workflow_id&&s.code_manifest_sha256===config.main_manifest
    &&s.business_id===config.scope.business_id&&s.branch_id===config.scope.branch_id&&[s.run_id,s.owner_execution].every(id)
    &&[s.effect_id,s.request_hash].every(hash)&&uuid(s.nonce),'ACK_TARGET_METADATA_SOURCE');
  for(const [index,a] of r.actions.entries()){
    need(exact(a,['descriptor','action_nonce','issued_at_ms','expires_at_ms','evidence','ack_durable'])&&a.ack_durable===true&&uuid(a.action_nonce)
      &&integer(a.issued_at_ms)&&a.issued_at_ms>0&&integer(a.expires_at_ms)&&a.expires_at_ms>a.issued_at_ms&&a.expires_at_ms-a.issued_at_ms<=2000,'ACK_TARGET_ACTION_NOT_DURABLE');
    const d=a.descriptor,e=a.evidence;
    need(exact(d,['tenant','scope','delivery_id','message_id','sender','recipient_jid','account_jid','envelope_digest','action_digest','index','provider_message_id','claim_nonce'])
      &&d.tenant===config.tenant&&same(d.scope,config.scope)&&d.delivery_id===r.delivery_id&&d.message_id===message_id&&d.sender===sender&&d.envelope_digest===r.envelope_digest
      &&d.index===index&&d.account_jid===config.account_jid&&d.recipient_jid===(sender.endsWith('@lid')?sender:sender+'@s.whatsapp.net')
      &&hash(d.action_digest)&&id(d.provider_message_id)&&d.claim_nonce===r.claim_nonce,'ACK_TARGET_ACTION_BINDING');
    need(exact(e,['mode','descriptor_sha256','action_nonce','provider','provider_message_id','index','recipient_jid','account_jid','ack','observed_at_ms'])
      &&e.mode==='authenticated_baileys_ack'&&e.descriptor_sha256===sha256(canonicalJSON(d))&&e.action_nonce===a.action_nonce&&e.provider==='baileys'
      &&e.provider_message_id===d.provider_message_id&&e.index===index&&e.recipient_jid===d.recipient_jid&&e.account_jid===config.account_jid
      &&Number.isInteger(e.ack)&&e.ack>=2&&e.ack<=5&&integer(e.observed_at_ms)&&e.observed_at_ms>=a.issued_at_ms,'ACK_TARGET_EVIDENCE');
  }
  return tools.targetOf({version:1,delivery_id:r.delivery_id,message_id:r.message_id,sender:r.sender,envelope_digest:r.envelope_digest,source_row_id:s.row_id,source_run_id:s.run_id});
}
