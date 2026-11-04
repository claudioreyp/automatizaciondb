// Local pure validation of the ACTUAL materialized prepare-node body.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const HERE=path.dirname(fileURLToPath(import.meta.url));
const {code,contracts}=JSON.parse(fs.readFileSync(path.join(HERE,'local-run-r01/controller-prepare-test-input.json'),'utf8'));
const script=new vm.Script('(function(){'+code+'\n})()');
const run=body=>script.runInNewContext({$input:{first:()=>({json:{body}})}},{timeout:100});
let checks=0;
for(const row of contracts){
  const result=run({operation:'start-original-case',case_key:row.canonical_key,fixture_contract_sha256:row.fixture_contract_sha256});
  assert.equal(result.length,1);assert.equal(result[0].json.url,'http://impulsa-qr-api-eval:8769/control/start-original-case');
  assert.equal(result[0].json.method,'POST');assert.equal(JSON.stringify(result[0].json.payload),JSON.stringify({case_key:row.canonical_key,fixture_contract_sha256:row.fixture_contract_sha256}));checks++;
}
for(const body of [
  {operation:'start-original-case',case_key:'OTHER',fixture_contract_sha256:'0'.repeat(64)},
  {operation:'start-original-case',case_key:contracts[0].canonical_key,fixture_contract_sha256:'0'.repeat(64)},
  {operation:'start-original-case',case_key:contracts[0].canonical_key,fixture_contract_sha256:contracts[0].fixture_contract_sha256,url:'https://example.invalid'},
  {operation:'visual-snapshot',case_id:'OTHER',sender:'519990120001'},
  {operation:'visual-snapshot',case_id:'SMOKE01-GREETING-PIZZA-CASH',sender:'51987654321'},
]){assert.throws(()=>run(body),/EVAL_CONTROL_OPERATION/);checks++;}
const image=run({operation:'visual-snapshot',case_id:'SMOKE01-GREETING-PIZZA-CASH',sender:'519990120001'});
assert.equal(image[0].json.url,'http://impulsa-qr-api-eval:8769/control/visual-snapshot');checks++;
for(const case_id of ['SMOKE08-CURRENT-YAPE-INFORMATION','SMOKE09-CASH-ADDITION-ONLY-KITCHEN',contracts[0].canonical_key]){
  const result=run({operation:'visual-snapshot',case_id,sender:'519990120001'});
  assert.equal(result[0].json.url,'http://impulsa-qr-api-eval:8769/control/visual-snapshot');
  assert.equal(result[0].json.payload.case_id,case_id);checks++;
}
console.log(JSON.stringify({status:'PASS_LOCAL_CONTROLLER_ONLY',checks,contracts:15,models:0,network:0,originals_executed:0}));
