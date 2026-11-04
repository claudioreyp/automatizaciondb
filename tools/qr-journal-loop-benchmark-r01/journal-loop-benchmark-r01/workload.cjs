/* Synthetic benchmark contract. No production journal, SQL, network or models. */
'use strict';
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const sha=v=>crypto.createHash('sha256').update(v).digest('hex');
const clone=v=>JSON.parse(JSON.stringify(v));
const WORKFLOW='JournalBenchR01Workflow';
const TYPE='CUSTOM.escalarJournalLoop';
const TABLE_NAME='EscalarSyntheticJournalBenchmarkR01';
const FIELDS=Object.freeze(['run_id','workflow_id','code_manifest_sha256','business_id','branch_id','head','state','owner_execution','nonce','body_json','body_hash']);
const PHASES=Object.freeze(['checkpoint','intent','result']);
const CASES=Object.freeze([4,13].flatMap(effects=>[7168,204800].map(bytes=>({id:`effects${effects}-bytes${bytes}`,effects,bytes}))));
const need=(v,c)=>{if(!v)throw Error(c);};
function spec(id){const found=CASES.find(c=>c.id===id);need(found,'FIXED_SYNTHETIC_CASE');return found;}
function seed(id){const c=spec(id),body={schema:'escalarai.synthetic-journal-benchmark.v1',padding:'',history:[]};
  body.padding='x'.repeat(c.bytes-Buffer.byteLength(JSON.stringify(body)));
  const body_json=JSON.stringify(body);need(Buffer.byteLength(body_json)===c.bytes,'SEED_BYTES');
  return {id:1,run_id:'JournalBenchR01:'+id,workflow_id:WORKFLOW,code_manifest_sha256:sha('journal-benchmark-r01'),business_id:2,branch_id:2,head:0,state:'seed',owner_execution:'SYNTHETIC_NO_EXTERNAL_EFFECTS',nonce:'seed',body_json,body_hash:sha(body_json)};}
function project(row){need(row&&typeof row==='object'&&!Array.isArray(row),'ROW_OBJECT');return Object.fromEntries(['id',...FIELDS].map(k=>[k,row[k]]));}
function equalRow(row,expected){const actual=project(row);assert.deepEqual(actual,expected,'EXACT_JOURNAL_READBACK');need(sha(actual.body_json)===actual.body_hash,'READBACK_HASH');return clone(actual);}
function filter(values){return {conditions:values.map(([keyName,keyValue])=>({keyName,condition:'eq',keyValue}))};}
function getParameters(tableId,row){return {resource:'row',operation:'get',dataTableId:{__rl:true,mode:'id',value:tableId},matchType:'allConditions',filters:filter([['id',1],['run_id',row.run_id]]),returnAll:false,limit:2,orderBy:false};}
function step(previous,id,index,tableId){const c=spec(id),row=project(previous),effect=Math.floor(index/3),phase=PHASES[index%3];
  need(Number.isInteger(index)&&index>=0&&index<c.effects*3,'FIXED_PHASE');
  need(row.head===index&&row.id===1&&row.run_id===seed(id).run_id&&row.workflow_id===WORKFLOW&&row.business_id===2&&row.branch_id===2,'PRIOR_SCOPE_HEAD');
  need(row.body_hash===sha(row.body_json),'PRIOR_HASH');const body=JSON.parse(row.body_json);
  need(body.schema==='escalarai.synthetic-journal-benchmark.v1'&&body.history.length===index,'PRIOR_HISTORY');
  body.history.push({effect,phase,kind:phase==='intent'?'NO_HTTP_SYNTHETIC_INTENT':phase==='result'?'SYNTHETIC_RESULT':'SYNTHETIC_CHECKPOINT'});
  const expected={...row,head:index+1,state:phase,nonce:`${id}:${effect}:${phase}`,body_json:JSON.stringify(body)};expected.body_hash=sha(expected.body_json);
  const update={resource:'row',operation:'update',dataTableId:{__rl:true,mode:'id',value:tableId},matchType:'allConditions',filters:filter([['id',1],['head',row.head],['state',row.state],['body_hash',row.body_hash]]),columns:{mappingMode:'defineBelow',value:Object.fromEntries(FIELDS.map(k=>[k,expected[k]])),matchingColumns:[],schema:[],attemptToConvertTypes:false,convertFieldsToString:false},options:{dryRun:false}};
  return {row:expected,cas:{id:1,head:row.head,state:row.state,body_hash:row.body_hash},update,get:getParameters(tableId,row)};
}
function record(s){const normalize=p=>({...p,dataTableId:{...p.dataTableId,value:'OWN_FRESH_TABLE'}});return {head:s.row.head,update_sha256:sha(JSON.stringify(normalize(s.update))),get_sha256:sha(JSON.stringify(normalize(s.get))),body_hash:s.row.body_hash};}
function expected(id){let row=seed(id);const trace=[];for(let i=0;i<spec(id).effects*3;i++){const s=step(row,id,i,'OWN_FRESH_TABLE');trace.push(record(s));row=s.row;}return {row,trace,trace_sha256:sha(JSON.stringify(trace))};}
function facade(context,parameters,input){need(context.getNode().type===TYPE&&context.getNode().typeVersion===1.1,'ACTUAL_CUSTOM_IDENTITY');
  return new Proxy(context,{get(t,k){if(k==='getInputData')return()=>clone(input);if(k==='continueOnFail')return()=>true;
    if(k==='getNodeParameter')return(name,index,fallback,options)=>{need(index===0,'ONE_ITEM_INDEX');let v=parameters;for(const part of name.split('.')){need(!['__proto__','prototype','constructor'].includes(part),'PARAMETER_PATH');if(!v||!Object.hasOwn(v,part))return fallback;v=v[part];}return options?.extractValue&&v?.__rl===true?v.value:v===undefined?fallback:clone(v);};
    const v=Reflect.get(t,k,t);return typeof v==='function'?v.bind(t):v;}});}
async function official(context,parameters,router){const result=await router.call(facade(context,parameters,[{json:{synthetic_request:true,operation:parameters.operation}}]));need(Array.isArray(result)&&result.length===1&&Array.isArray(result[0]),'OFFICIAL_SINGLE_OUTPUT');return result[0];}
function one(items){need(Array.isArray(items)&&items.length===1&&!items[0].error&&items[0].json&&typeof items[0].json==='object','ONE_READBACK_ITEM');return items[0].json;}
async function loop({context,caseId,tableId,row,router,signal}){let current=equalRow(row,seed(caseId));const trace=[];
  for(let i=0;i<spec(caseId).effects*3;i++){need(!signal?.aborted,'CANCELLED_NO_RETRY');const s=step(current,caseId,i,tableId);
    let thrown;let updated=[];try{updated=await official(context,s.update,router);}catch(e){thrown=e;}
    // Always retain the independent readback. The returned update array is not
    // an affected-row count: the installed SQLite repository preselects IDs.
    const read=await official(context,s.get,router);current=equalRow(one(read),s.row);
    need(!thrown&&!updated.some(x=>x.error||x.json?.synthetic_request),'UPDATE_ERROR_AFTER_READBACK');trace.push(record(s));}
  return {row:current,trace};}
module.exports={WORKFLOW,TYPE,TABLE_NAME,FIELDS,PHASES,CASES,need,sha,clone,spec,seed,project,equalRow,filter,getParameters,step,record,expected,facade,official,one,loop};
