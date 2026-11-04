/** SOURCE-ONLY adapter for ROOT's server-assigned credential-ID decision.
 * Calls the unchanged reviewed eval builder with its synthetic sentinel, then
 * binds only exact credential-ID sites with a complete inverse proof. No I/O
 * except the existing pure builder's local source reads; no native execution.
 */
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {buildEvalNotifier,EVAL_API_BASE_URL} from '../../gateway/notifications/eval-r01/build-eval-notifier.mjs';

const SENTINEL='QrEvalNoticeR0720261104';
const NAME='QR API isolated notifier QrEvalNoticeR0720261104';
const SCOPES=['events:deliver','events:read','menu:read','orders:read'];
const clone=value=>JSON.parse(JSON.stringify(value));
const hash=value=>createHash('sha256').update(value).digest('hex');
const marker='\nconst POLICY=(()=>{const exports={};\n';

export function buildAssignedNotifier({workflowId,credentialReference,activatedAt,nowMs}){
  const ref=credentialReference;
  assert(ref?.schema==='escalarai.notifier-r07-created-reference.v1'&&ref.state==='CREATED_NEW_ASSIGNED_ID_VERIFIED_PROJECT');
  assert(ref.project_id==='Wz76dpTUvZYQaRdl'&&ref.credential_type==='httpHeaderAuth'&&ref.credential_name===NAME);
  assert.deepEqual(ref.scope,{business_id:2,branch_id:2,profile:'generic',scopes:SCOPES});
  const actual=ref.credential_id;
  assert(typeof actual==='string'&&/^[A-Za-z0-9]{8,40}$/.test(actual));
  assert(![SENTINEL,'isSVaRTGTA42G88Z','QrEvalPos5499111580ba','QrEvalCtl577034d91271','r6usoZc9HKfkgluB'].includes(actual));
  assert(typeof workflowId==='string'&&/^[A-Za-z0-9]{8,40}$/.test(workflowId)&&workflowId!=='9IDkhRko8wx9lmOH');
  const bindings=JSON.parse(readFileSync(new URL('../../n8n/joint-bindings.json',import.meta.url),'utf8'));
  const protectedIds=Object.values(bindings.profiles).flatMap(profile=>Object.values(profile.workflow_ids));
  assert.equal(protectedIds.length,14);
  assert(![...protectedIds,'pNYNOdf96YGoffVb'].includes(workflowId));
  const base=buildEvalNotifier({enabled:false,api_base_url:EVAL_API_BASE_URL,business_id:2,branch_id:2,
    activated_at:activatedAt,workflow_id:workflowId,credential_id:SENTINEL,credential_name:NAME,
    batch_size:1,allowed_senders:['519990900001']},{now_ms:nowMs});
  const result=clone(base),oldHash=base.meta.notice_contract_sha256;
  let httpSites=0,codeSites=0,codeNodes=0;
  const before=JSON.stringify(SENTINEL),after=JSON.stringify(actual);
  for(const node of result.nodes){
    if(node.credentials){
      assert(node.type==='n8n-nodes-base.httpRequest');
      assert.deepEqual(node.credentials,{httpHeaderAuth:{id:SENTINEL,name:NAME}});
      node.credentials.httpHeaderAuth.id=actual;httpSites++;
    }
    if(typeof node.parameters?.jsCode==='string'){
      const source=node.parameters.jsCode,count=source.split(before).length-1;
      assert.equal(count,node.id==='notice-0'?2:1);
      node.parameters.jsCode=source.split(before).join(after);codeSites+=count;codeNodes++;
    }
  }
  assert.equal(httpSites,4);assert.equal(codeNodes,5);assert.equal(codeSites,6);
  assert.equal(result.meta.eval_only.credential_id,SENTINEL);
  result.meta.eval_only.credential_id=actual;
  const config=result.nodes.find(node=>node.id==='notice-0').parameters.jsCode;
  assert.equal(config.split(marker).length,2);
  result.meta.notice_contract_sha256=hash(config.split(marker)[0]);
  // The exact inverse restores every Code byte, credential ref, setting,
  // connection, CFG, recipient allowlist and original manifest metadata.
  const inverse=clone(result);
  for(const node of inverse.nodes){
    if(node.credentials)node.credentials.httpHeaderAuth.id=SENTINEL;
    if(typeof node.parameters?.jsCode==='string')node.parameters.jsCode=node.parameters.jsCode.split(after).join(before);
  }
  inverse.meta.eval_only.credential_id=SENTINEL;inverse.meta.notice_contract_sha256=oldHash;
  assert.deepEqual(inverse,base);
  assert.equal(result.active,false);
  assert(result.nodes.filter(node=>node.type.endsWith('Trigger')).every(node=>node.disabled===true));
  return {graph:result,proof:{state:'OFF_ASSIGNED_CREDENTIAL_ADAPTER_ONLY_NOT_STAGED',
    namespace:SENTINEL,credential_id:actual,workflow_id:workflowId,exact_inverse:true,
    changed_http_reference_sites:httpSites,changed_code_id_literal_sites:codeSites,
    changed_meta_id_sites:1,contract_hash_recomputed:true,activation:false,models:0,network:0}};
}
