'use strict';
const fs=require('node:fs');
const path=require('node:path');
const {createRequire}=require('node:module');
const W=require('./workload.cjs');
let loaded;
function installed(){if(loaded)return loaded;const pins=JSON.parse(fs.readFileSync(path.join(__dirname,'..','pins.json'),'utf8'));
  W.need(pins.schema==='escalarai.journal-benchmark-installed-pins.v1','INSTALLED_PINS_SCHEMA');
  for(const p of Object.values(pins.packages))W.need(W.sha(fs.readFileSync(p.package_json))===p.sha256,'INSTALLED_PACKAGE_PIN');
  for(const p of pins.files){const expected=p.resolved===pins.patch.path?pins.patch.after_sha256:p.sha256;W.need(W.sha(fs.readFileSync(p.resolved))===expected,'INSTALLED_SOURCE_PIN_'+p.relative);}
  const req=createRequire(pins.packages.n8n.package_json);
  const {DataTable}=req(pins.data_table_node);
  const {DataTableProxyService}=req(pins.patch.path);
  loaded={router:DataTable.prototype.execute,pins,provider:()=>req('@n8n/di').Container.get(DataTableProxyService)};return loaded;
}
function config(ctx){const node=ctx.getNode();W.need(ctx.getWorkflow().id===W.WORKFLOW&&node.type===W.TYPE&&node.typeVersion===1.1,'FIXED_OWN_WORKFLOW_NODE');
  const c=JSON.parse(node.parameters.config||'{}');W.need(c&&typeof c==='object'&&!Array.isArray(c),'STATIC_CONFIG');W.spec(c.caseId);return c;}
function nominal(ctx,name){W.need(/^(?:Plan|Merged|Update)[0-9]{3}$|^Create fixture table$|^Begin measured$/.test(name),'FIXED_NOMINAL_NODE');return ctx.getWorkflowDataProxy(0).$(name).first().json;}
function rowInput(ctx){return W.one(ctx.getInputData());}
function transfer(ctx,c){const s=nominal(ctx,c.planName);const row=W.equalRow(rowInput(ctx),s.row);return {caseId:c.caseId,tableId:c.tableId,row,trace:s.trace,started_ns:s.started_ns};}
function makePlan(state,c,index){const s=W.step(state.row,c.caseId,index,c.tableId);return {...state,row:s.row,cas:s.cas,trace:[...state.trace,W.record(s)]};}
function finish(state,c){const wanted=W.expected(c.caseId);W.equalRow(state.row,wanted.row);W.need(W.sha(JSON.stringify(state.trace))===wanted.trace_sha256,'COMPLETE_ORDERED_OPERATION_HISTORY');
  return {schema:'escalarai.native-journal-benchmark-result.v1',case_id:c.caseId,variant:c.variant,effects:W.spec(c.caseId).effects,operations:W.spec(c.caseId).effects*6,final_row:state.row,trace_sha256:wanted.trace_sha256,proxy_observation:state.proxy_observation||null,measured_ms:Number(process.hrtime.bigint()-BigInt(state.started_ns))/1e6,external_effects:0,models:0};}
function observedContext(ctx){const counts={getDataTableProxy:0,getColumns:0,updateRows:0,getManyRowsAndCount:0};
  const helpers=new Proxy(ctx.helpers,{get(t,k){const fn=Reflect.get(t,k,t);if(k!=='getDataTableProxy')return typeof fn==='function'?fn.bind(t):fn;
    return async(...args)=>{counts.getDataTableProxy++;const proxy=await fn.apply(t,args);return new Proxy(proxy,{get(p,method){const value=Reflect.get(p,method,p);if(typeof value!=='function')return value;return(...a)=>{if(Object.hasOwn(counts,method))counts[method]++;return value.apply(p,a);};}});};}});
  return {counts,context:new Proxy(ctx,{get(t,k){if(k==='helpers')return helpers;const v=Reflect.get(t,k,t);return typeof v==='function'?v.bind(t):v;}})};}
async function owned(ctx,c){W.need(typeof c.tableId==='string'&&/^[A-Za-z0-9]{8,32}$/.test(c.tableId),'FRESH_TABLE_ID');W.need(typeof ctx.helpers?.getDataTableAggregateProxy==='function'&&typeof ctx.helpers?.getDataTableProxy==='function','ACTUAL_CUSTOM_PROXY_HELPERS_REQUIRED');const aggregate=await ctx.helpers.getDataTableAggregateProxy();
  const found=await aggregate.getManyAndCount({filter:{id:c.tableId},take:2});W.need(found.count===1&&found.data.length===1&&found.data[0].id===c.tableId&&found.data[0].name===W.TABLE_NAME,'FRESH_OWNED_TABLE');
  const project=aggregate.getProjectId();W.need(typeof project==='string'&&project.length>0,'ACTUAL_OWN_PROJECT');return project;}
async function components(ctx,c,router,provider){const projectId=await owned(ctx,c);let row=W.equalRow(rowInput(ctx),W.seed(c.caseId));
  let rejected=0;try{await provider.getDataTableProxy(ctx.getWorkflow(),{...ctx.getNode(),type:'CUSTOM.unapprovedJournalBench'},c.tableId);}catch(e){W.need(e.message==='This proxy is only available for Data table nodes','UNAPPROVED_EXACT_REJECTION');rejected++;}
  try{const foreign=await provider.getDataTableProxy(ctx.getWorkflow(),ctx.getNode(),c.tableId,projectId+'-not-owner');await foreign.getColumns();}catch(e){W.need(e.constructor.name==='DataTableNotFoundError','WRONG_PROJECT_EXACT_REJECTION');rejected++;}
  W.need(rejected===2,'PROXY_AND_PROJECT_NEGATIVES');
  const first=W.step(row,c.caseId,0,c.tableId);const injected=[];
  async function resetSeed(){const p=W.clone(first.update);p.filters=W.filter([['id',1],['head',row.head],['state',row.state],['body_hash',row.body_hash]]);p.columns.value=Object.fromEntries(W.FIELDS.map(k=>[k,W.seed(c.caseId)[k]]));await W.official(ctx,p,router);row=W.equalRow(W.one(await W.official(ctx,first.get,router)),W.seed(c.caseId));}
  // These faults are injected only after real official operations complete.
  // They model lost output/cancellation/corrupt readback, not a real outage.
  for(const seam of ['abort_after_committed_update','lost_update_result','empty_readback','duplicate_readback']){
    if(row.head)await resetSeed();const signal={aborted:false},calls={update:0,get:0};let caught;
    const injectedRouter=async function(){const op=this.getNodeParameter('operation',0);calls[op]++;const actual=await router.call(this);
      if(op==='update'&&seam==='abort_after_committed_update')signal.aborted=true;
      if(op==='update'&&seam==='lost_update_result')throw Error('SYNTHETIC_RESULT_LOST_AFTER_OFFICIAL_COMMIT');
      if(op==='get'&&seam==='empty_readback')return [[]];
      if(op==='get'&&seam==='duplicate_readback')return [[actual[0][0],actual[0][0]]];return actual;};
    try{await W.loop({context:ctx,caseId:c.caseId,tableId:c.tableId,row:W.seed(c.caseId),router:injectedRouter,signal});}catch(e){caught=e;}
    const expected=seam==='abort_after_committed_update'?'CANCELLED_NO_RETRY':seam==='lost_update_result'?'UPDATE_ERROR_AFTER_READBACK':'ONE_READBACK_ITEM';
    W.need(caught?.message===expected&&calls.update===1&&calls.get===1,'INJECTED_FAULT_NO_RETRY_'+seam);
    row=W.equalRow(W.one(await W.official(ctx,first.get,router)),first.row);injected.push({seam,label:'SYNTHETIC_INJECTION_AFTER_REAL_OFFICIAL_OPERATION',error:caught.message,official_primary_calls:calls,retained_head:row.head,retained_body_hash:row.body_hash});
  }
  const stale=await W.official(ctx,first.update,router);W.need(stale.length===0,'STALE_CAS_ZERO');W.equalRow(W.one(await W.official(ctx,first.get,router)),row);
  const malformed=W.clone(first.get);malformed.filters.conditions[0].keyName='not_a_column';const bad=await W.official(ctx,malformed,router);W.need(bad.length===1&&!!bad[0].error,'OFFICIAL_FILTER_VALIDATION_RETAINED');
  let mismatch=false;try{W.equalRow({...row,body_json:row.body_json+' '},row);}catch{mismatch=true;}W.need(mismatch,'CORRUPT_READBACK_REJECTED');
  await resetSeed();const intervening=W.step(first.row,c.caseId,1,c.tableId);let raceError;const raceCalls={update:0,get:0};
  const racingRouter=async function(){const op=this.getNodeParameter('operation',0);raceCalls[op]++;const actual=await router.call(this);if(op==='update')await W.official(ctx,intervening.update,router);return actual;};
  try{await W.loop({context:ctx,caseId:c.caseId,tableId:c.tableId,row:W.seed(c.caseId),router:racingRouter});}catch(e){raceError=e;}
  W.need(raceError&&raceCalls.update===1&&raceCalls.get===1,'INTERVENING_WRITER_REJECTED_NO_RETRY');row=W.equalRow(W.one(await W.official(ctx,first.get,router)),intervening.row);
  // Two actual official-router calls overlap, with the same CAS predicate and
  // distinguishable payloads. No update-array count is treated as ownership.
  const priorHead=row.head;const a=W.step(row,c.caseId,priorHead,c.tableId),b=W.clone(a);
  for(const [candidate,label] of [[a,'A'],[b,'B']]){const body=JSON.parse(candidate.row.body_json);body.history.at(-1).contender=label;candidate.row.body_json=JSON.stringify(body);candidate.row.body_hash=W.sha(candidate.row.body_json);candidate.row.nonce+=':'+label;candidate.update.columns.value=Object.fromEntries(W.FIELDS.map(k=>[k,candidate.row[k]]));}
  const outcomes=await Promise.allSettled([a,b].map(s=>W.official(ctx,s.update,router)));
  const observed=W.project(W.one(await W.official(ctx,a.get,router)));const accepted=[a,b].map(s=>{try{W.equalRow(observed,s.row);return true;}catch{return false;}});
  W.need(accepted.filter(Boolean).length===1&&observed.head===priorHead+1&&JSON.parse(observed.body_json).history.length===priorHead+1,'ONE_CONCURRENT_CAS_OWNER');
  const loser=accepted[0]?b:a;const late=await W.official(ctx,loser.update,router);W.need(late.length===0,'CONFLICT_REPLAY_CAS_ZERO');W.equalRow(W.one(await W.official(ctx,a.get,router)),observed);
  let cancelled=false;try{await W.loop({context:ctx,caseId:c.caseId,tableId:c.tableId,row:W.seed(c.caseId),router:async()=>{throw Error('CANCEL_MUST_NOT_CALL_ROUTER');},signal:{aborted:true}});}catch(e){cancelled=e.message==='CANCELLED_NO_RETRY';}W.need(cancelled,'CANCEL_BEFORE_FIRST_OPERATION');
  return {schema:'escalarai.journal-native-components.v1',case_id:c.caseId,project_id:projectId,unapproved_rejected:true,wrong_project_rejected:true,stale_cas_zero:true,filter_validation:true,corrupt_readback_rejected:true,cancel_before_operation:true,injected_seams:injected,intervening_official_writer:{rejected:true,primary_calls:raceCalls,retained_head:row.head,retained_body_hash:row.body_hash},concurrency:{accepted,outcomes:outcomes.map(o=>o.status==='rejected'?{status:o.status,error:String(o.reason?.message||o.reason)}:{status:o.status,output_rows:o.value.length,errors:o.value.filter(x=>x.error).map(x=>String(x.error.message))})},final_row:observed,external_effects:0,models:0};
}
class EscalarJournalLoop{constructor(){this.description={displayName:'Escalar synthetic journal benchmark',name:'escalarJournalLoop',group:['transform'],version:1.1,description:'Offline fixed-fixture benchmark; no external effects',defaults:{name:'Escalar journal benchmark'},inputs:['main'],outputs:['main'],properties:[{displayName:'Mode',name:'mode',type:'options',noDataExpression:true,options:['seed','bound','begin','plan','guard','verify','merged','loop','finish','components'].map(value=>({name:value,value})),default:'guard'},{displayName:'Fixed config',name:'config',type:'json',noDataExpression:true,default:'{}'}]};}
  async execute(){const c=config(this),mode=this.getNode().parameters.mode;let result;
    if(mode==='seed'){const table=rowInput(this);W.need(table.name===W.TABLE_NAME&&typeof table.id==='string','CREATED_FIXTURE_TABLE');const s=W.seed(c.caseId);delete s.id;result=s;}
    else if(mode==='bound'){const table=nominal(this,'Create fixture table');W.equalRow(rowInput(this),W.seed(c.caseId));const project_id=await owned(this,{...c,tableId:table.id});result={schema:'escalarai.journal-fresh-fixture.v1',workflow_id:W.WORKFLOW,case_id:c.caseId,table_id:table.id,table_name:W.TABLE_NAME,project_id,row:W.seed(c.caseId)};}
    else if(mode==='begin'){await owned(this,c);const row=W.equalRow(rowInput(this),W.seed(c.caseId));result={caseId:c.caseId,tableId:c.tableId,row,trace:[],started_ns:String(process.hrtime.bigint())};}
    else if(mode==='plan'){result=makePlan(rowInput(this),c,c.index);}
    else if(mode==='guard'){result=rowInput(this);W.need(result.caseId===c.caseId&&result.tableId===c.tableId,'CARRY_SCOPE');W.need(result.row.body_hash===W.sha(result.row.body_json)&&JSON.parse(result.row.body_json).history.length===result.row.head,'CARRY_HISTORY_HASH');result=W.clone(result);}
    else if(mode==='verify'){result=transfer(this,c);}
    else if(mode==='merged'){const state=transfer(this,c);result=c.index===W.spec(c.caseId).effects*3?state:makePlan(state,c,c.index);}
    else if(mode==='loop'){const start=rowInput(this);const {router}=installed();const o=observedContext(this);const state=await W.loop({context:o.context,caseId:c.caseId,tableId:c.tableId,row:start.row,router,signal:this.getExecutionCancelSignal()});const n=W.spec(c.caseId).effects;W.need(o.counts.getDataTableProxy===12*n&&o.counts.getColumns===6*n&&o.counts.updateRows===3*n&&o.counts.getManyRowsAndCount===3*n,'EXACT_OFFICIAL_PROXY_OPERATION_COUNTS');result={...start,...state,proxy_observation:o.counts};}
    else if(mode==='finish'){result=finish(rowInput(this),c);}
    else if(mode==='components'){const {router,provider}=installed();result=await components(this,c,router,provider());}
    else throw Error('FIXED_MODE_REQUIRED');return [[{json:result,pairedItem:{item:0}}]];
  }}
module.exports={EscalarJournalLoop,installed,config,owned,components,finish};
