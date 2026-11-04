// Original-case binding through the SAME run-native-qr transport factories and
// authenticated private controller. Inert on import; no credentials or model IO.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {assertSnapshot,evaluateOriginalUnits} from './evaluators.mjs';
const HERE=path.dirname(fileURLToPath(import.meta.url));
const sha=v=>createHash('sha256').update(v).digest('hex');
const json=f=>JSON.parse(fs.readFileSync(f,'utf8').replace(/^\uFEFF/,''));
const need=(v,c)=>{if(!v)throw Error(c);};
const canonical=v=>JSON.stringify(v);
const ownSHA=()=>sha(fs.readFileSync(fileURLToPath(import.meta.url)));
function registry(config){
  const f=path.join(HERE,'manifest.json');need(sha(fs.readFileSync(f))===config.original_manifest_sha256,'ORIGINAL_ADAPTER_MANIFEST_PIN_REQUIRED');
  const m=json(f);
  for(const p of [...m.dependencies,m.corpus_manifest,...m.candidate_pins,...m.factory_pins])need(sha(fs.readFileSync(p.file))===p.sha256,'ORIGINAL_CURRENT_DEPENDENCY_PIN_REQUIRED');
  return m;
}
function originalRow(caseDefinition,config){
  const m=registry(config),row=m.cases.find(c=>c.canonical_key===caseDefinition.canonical_key);
  need(row,'UNSUPPORTED_ORIGINAL_CASE_REQUIRED');
  need(sha(fs.readFileSync(row.file))===row.sha256&&canonical(json(row.file))===canonical(caseDefinition),'ORIGINAL_CASE_OR_ORACLE_CHANGED');
  need(caseDefinition.steps.every(s=>s.kind==='consumer'&&typeof s.text==='string'&&s.text.trim()&&!s.text.includes('{{')),'ORIGINAL_TEXT_ONLY_ADAPTER_REQUIRED');
  return row;
}
const unwrap=response=>{
  need(response.http_status===200,'FIXED_ORIGINAL_CONTROL_HTTP');
  const body=response.body?.synthetic_only===true?response.body:response.body?.result;
  need(body?.synthetic_only===true&&body.database==='sqlite_memory','ORIGINAL_CONTROL_SYNTHETIC_PROOF_REQUIRED');
  return {...body,evidence_refs:[response.evidence_ref]};
};
function verifyFixture(proof,row){
  need(proof.schema==='escalarai.original-fixture-proof.v1'&&proof.case_key===row.canonical_key&&proof.case_id===row.canonical_key
    &&proof.fixture_contract_sha256===row.fixture_contract_sha256
    &&canonical(proof.execution_scope)===canonical(row.fixture_contract.execution_scope)
    &&canonical(proof.original_scope)===canonical(row.fixture_contract.original_scope)
    &&proof.foreign_catalog_unchanged===true&&Object.values(proof.commercial_counts??{}).length>=4
    &&Object.values(proof.commercial_counts).every(v=>v===0),'ORIGINAL_FIXTURE_CONTRACT_PROOF_REQUIRED');
  const expected=row.fixture_contract.product;
  if(expected){
    const products=proof.observed_catalog?.products;
    const product=Array.isArray(products)&&products.find(p=>p.id===expected.id);
    need(product&&product.name===expected.name&&product.price===expected.price,'ORIGINAL_PRODUCT_PRICE_ID_REQUIRED');
    const variants=product.variants??[];
    need(expected.variants.every(e=>variants.some(v=>v.name===e.name&&v.price_delta===e.price_delta)),'ORIGINAL_VARIANTS_REQUIRED');
    const groups=product.modifier_groups??[],group=groups.find(g=>g.name===expected.required_group.name),e=expected.required_group;
    need(group&&group.minimum===e.minimum&&group.maximum===e.maximum&&group.allow_repeats===e.allow_repeats
      &&group.required===true&&e.options.every(n=>(group.modifiers??[]).some(m=>m.name===n&&m.available===true)),'ORIGINAL_REQUIRED_CHOICES_REQUIRED');
  }
}
const snapshotFor=async(bound,api)=>{
  const s=assertSnapshot(unwrap(await api.request('POST','',{operation:'snapshot',business_id:bound.business_id,sender:bound.sender})));
  need(s.business_id===bound.business_id&&s.sender===bound.sender&&s.case_id===bound.fixture_id,'ORIGINAL_SNAPSHOT_SCOPE_REQUIRED');return s;
};
export async function bindCase({caseDefinition,config,runId,api}){
  const row=originalRow(caseDefinition,config),scope=row.fixture_contract.execution_scope;
  need(config.control_capabilities?.includes('start-original-case-v1')&&config.allow_scope_fixture_mapping===true,'ORIGINAL_CONTROL_EXTENSION_AND_SCOPE_MAPPING_REQUIRED');
  need(config.business_id===scope.business_id&&config.branch_id===scope.branch_id&&config.max_model_posts_per_turn===3
    &&Number.isSafeInteger(config.max_worker_units_per_turn)&&config.max_worker_units_per_turn>0,'ORIGINAL_FRESH_CURRENT_SCOPE_LIMITS_REQUIRED');
  const proof=unwrap(await api.request('POST','',{operation:'start-original-case',case_key:row.canonical_key,fixture_contract_sha256:row.fixture_contract_sha256}));
  verifyFixture(proof,row);
  const suffix=String(parseInt(runId.replaceAll('-','').slice(0,10),16)%100000000).padStart(8,'0');
  const bound={synthetic_only:true,fixture_id:row.canonical_key,business_id:scope.business_id,branch_id:scope.branch_id,
    sender:config.sender??'519990'+suffix,profile:config.profile??'Consumidor ORIGINAL sintético',clientConfig:config.clientConfig,
    max_worker_units_per_turn:config.max_worker_units_per_turn,max_model_posts_per_turn:3,source_config:config,fixture_proof:proof,
    steps:caseDefinition.steps.map((s,i)=>({...s,source_index:i})),
    resolutions:[{field:'/execution_binding',reason:row.fixture_contract.mapping_reason,source_refs:proof.evidence_refs}]};
  const initial=await snapshotFor(bound,api);need(['orders','conversations','payments','tickets','payment_evidence','deliveries','model_usage'].every(k=>initial[k].length===0),'ORIGINAL_FRESH_SENDER_REQUIRED');
  return bound;
}
export async function snapshot({bound,api}){return snapshotFor(bound,api);}
export async function runScenario({bound,helpers,entry}){
  entry.effects_history=[];
  for(const step of bound.steps){
    const before=await snapshotFor(bound,helpers.api),row=await helpers.enqueue(step);row.effects_before=before;row.worker_units=[];
    let ready=false;
    for(let i=0;i<bound.max_worker_units_per_turn;i++){
      const r=await helpers.worker();row.worker_units.push(r);
      if(r.value.status==='AWAITING_MAILBOX'){ready=true;break;}
      need(['NATIVE_RUN_COMPLETE_WITH_DURABLE_QR_TRANSPORT','NATIVE_RUN_COMPLETE'].includes(r.value.status),'ORIGINAL_WORKER_STATUS_REQUIRED');
    }
    need(ready,'ORIGINAL_MAILBOX_READY_WITHIN_DECLARED_LIMIT_REQUIRED');
    await helpers.deliver(row);await helpers.ack(row);
    const after=await snapshotFor(bound,helpers.api);row.effects_after=after;
    row.model_usage=after.model_usage.filter(m=>m.message_id===row.message_id);
    need(row.model_usage.length<=3&&row.model_usage.every(m=>m.status==='settled'&&m.usage_known===true),'ORIGINAL_KNOWN_USAGE_THREE_CALLS_REQUIRED');
    need(!after.deliveries.some(d=>d.message_id===row.message_id&&d.status!=='sent'),'ORIGINAL_DURABLE_DELIVERY_REQUIRED');
    entry.effects_history.push({source_index:step.source_index,before,after,evidence_refs:[...before.evidence_refs,...after.evidence_refs]});
  }
}
export async function trace({record}){
  const history=record.effects_history??[],usage=history.at(-1)?.after.model_usage??[];
  return {model_records:usage.map(m=>({request_id:m.id,model_requested:m.model_id,model_actual:null,
    model_actual_observation:'NOT_PUBLISHED_BY_API_USAGE_LEDGER',role:m.role,message_id:m.message_id,status:m.status,usage_known:m.usage_known,
    input_tokens:m.input_tokens,output_tokens:m.output_tokens,cache_creation_input_tokens:m.cache_creation_input_tokens,cache_read_input_tokens:m.cache_read_input_tokens,
    started_at_ms:m.created_at?Date.parse(m.created_at):null,finished_at_ms:m.settled_at?Date.parse(m.settled_at):null})),
    tool_records:history.map(h=>({source_index:h.source_index,before_refs:h.before.evidence_refs,after_refs:h.after.evidence_refs})),evidence_refs:history.flatMap(h=>h.evidence_refs)};
}
export async function evaluate({caseDefinition,bound,record,evidence_refs}){
  const row=originalRow(caseDefinition,bound.source_config);
  return evaluateOriginalUnits({caseDefinition,record,nativeExpected:row.native_expected?.value??null,evaluatorSha256:ownSHA(),
    evidenceRefs:[evidence_refs.before,evidence_refs.after,...(record.effects_history??[]).flatMap(h=>h.evidence_refs)]});
}
export {verifyFixture};
