"""Inert R06 controller graph proposal. No deployment client is imported.

The historical build/readback guard is intentionally excluded. ROOT must obtain
a fresh controller metadata/readback and review a new stager before registration.
"""
import copy
import json
import pathlib

HERE=pathlib.Path(__file__).resolve().parents[2]
BASE='https://n8n.srv1547104.hstgr.cloud'
ENTRY={'id': 'entry', 'name': 'QR privado', 'type': 'n8n-nodes-base.webhook', 'typeVersion': 2.1, 'position': [0, 0], 'webhookId': 'impulsa-qr-2-2-health', 'parameters': {'httpMethod': 'GET', 'path': 'escalar-qr-2-2-v1/qr/health', 'authentication': 'headerAuth', 'responseMode': 'responseNode', 'options': {}}, 'credentials': {'httpHeaderAuth': {'id': 'r6usoZc9HKfkgluB', 'name': 'Escalar ingreso privado'}}}

def graph():
    bindings = json.loads((HERE / 'n8n/eval-bindings.json').read_text(encoding='utf-8'))
    canonical = json.loads((HERE / 'n8n/joint-bindings.json').read_text(encoding='utf-8'))['profiles']['test']
    for kind in ('private', 'control'):
        assert bindings['credential_refs'][kind]['id'] == canonical['credential_refs'][kind]['id']
    entry = copy.deepcopy(ENTRY)
    entry.update(name='Control sintético privado', webhookId='qr-api11-eval-control-20261104')
    entry['parameters'].update(httpMethod='POST', path='qr-api11-eval-20261104/control')
    entry['credentials']['httpHeaderAuth'] = copy.deepcopy(canonical['credential_refs']['private'])
    prepare = {'id': 'prepare', 'name': 'Validar operación fija', 'type': 'n8n-nodes-base.code',
        'typeVersion': 2, 'position': [220, 0], 'parameters': {'jsCode': '''
const body=$input.first().json.body;
const ORIGINAL_CASES={"I:SUP-01-GENERIC-PIZZA":"08af645a92a1f7e264230d1b96469c42cb0d3057aa2a93dbbbde02bfffd8afdf","I:SUP-02-MINIMUM-ONE-FLAVOR":"3110d7e62ad1065ed382cdddb2aef34af2f374f21c062c007d4381522d70153a","M:SEC-05":"750a937b13f1e6b7c7b97c6d5cd4d39d5cd4c08e32b29cc322571b4b3910e1bb","M:SEC-COMPLAINT":"5e445f6e35159f9da99d439ae932142fca36723cd4cb1d680ed93f69faf11fb4","M:SEC-01":"ea6a744e9914fbb6bf4b34da59b8a9f3247023d0d4b4fe6545ccc581ab830376","M:SEC-02":"722a38cf5c96a55366da3b149565290f14798b77b511cbcc62f7310f37b57be4","M:SEC-03":"5b19b52ddf34a950021589db46050589d9aa2a89d251abfbdfad00f2b7dabd53","M:SEC-04":"2354730f243818a0527f7aafb414ab6c63fd71279018387fdb63c9f6be2c9851","M:SEC-06":"d4083e1879306c14a5854c4942e7bb0debe1b551a1fd9e3439afb1268d70bae6","M:SEC-07":"fde9ecd615ed8692d8b8817f29b982bf3e0df0d15ac2e9a92a26bf1e676bf9c1","M:SEC-08":"153d45c740aa605727615d916073564db30530f87ffaf166423fcbd5f56f257b","M:SEC-09":"b0aae99edcb8da3fb2a8964d343f30fcab779aacddcfd4c43734a61f077c700a","M:SEC-11":"56d9164d739cae54dc5427724fa1aee7ccb519264b1e289b4adfe4fb00760835","M:SEC-13":"4009acea7ad59597964e6184c76710afc6530ff015a89ef6dc761aa9ea965426","M:SEC-19":"f534624363f348678808cfe6c1e258ceab96b034c62a8c273194c4b1a2d66a78"};
const VISUAL_CASES=["SMOKE01-GREETING-PIZZA-CASH","SMOKE02-PIZZA-MISSING-REQUIREMENTS","SMOKE03-MINIMUM-TWO-FLAVORS","SMOKE04-FOUR-FLAVORS-REJECTED","SMOKE05-WINGS-REQUIRED-SAUCE","SMOKE06-EXHAUSTED-PRODUCT","SMOKE07-CASH-YAPE-ONLY","SMOKE08-CURRENT-YAPE-INFORMATION","SMOKE09-CASH-ADDITION-ONLY-KITCHEN","SMOKE10-HUMAN-DELIVERY-QUOTE","SMOKE11-SAME-MESSAGE-ID-REDELIVERY","SMOKE12-QUANTITY-CORRECTION","I:SUP-01-GENERIC-PIZZA","I:SUP-02-MINIMUM-ONE-FLAVOR","M:SEC-05","M:SEC-COMPLAINT","M:SEC-01","M:SEC-02","M:SEC-03","M:SEC-04","M:SEC-06","M:SEC-07","M:SEC-08","M:SEC-09","M:SEC-11","M:SEC-13","M:SEC-19"];
if(!body||typeof body!=='object'||Array.isArray(body))throw Error('EVAL_CONTROL_BODY');
let url,method,payload={};
if(body.operation==='start-case'&&Object.keys(body).sort().join('|')==='case_id|operation'&&/^SMOKE[0-9]{2}-[A-Z0-9-]{1,80}$/.test(body.case_id)){
  url='http://impulsa-qr-api-eval:8769/control/start-case';method='POST';payload={case_id:body.case_id};
}else if(body.operation==='snapshot'&&Object.keys(body).sort().join('|')==='business_id|operation|sender'&&[2,3].includes(body.business_id)&&/^[1-9][0-9]{7,14}$/.test(body.sender)){
  url='http://impulsa-qr-api-eval:8769/control/snapshot?business_id='+body.business_id+'&sender='+body.sender;method='GET';
}else if(body.operation==='start-original-case'&&Object.keys(body).sort().join('|')==='case_key|fixture_contract_sha256|operation'&&Object.hasOwn(ORIGINAL_CASES,body.case_key)&&ORIGINAL_CASES[body.case_key]===body.fixture_contract_sha256){
  url='http://impulsa-qr-api-eval:8769/control/start-original-case';method='POST';payload={case_key:body.case_key,fixture_contract_sha256:body.fixture_contract_sha256};
}else if(body.operation==='visual-snapshot'&&Object.keys(body).sort().join('|')==='case_id|operation|sender'&&VISUAL_CASES.includes(body.case_id)&&/^519990[0-9]{5,9}$/.test(body.sender)){
  url='http://impulsa-qr-api-eval:8769/control/visual-snapshot';method='POST';payload={case_id:body.case_id,sender:body.sender};
}else if(['notice-ready','notice-snapshot'].includes(body.operation)&&Object.keys(body).sort().join('|')==='operation|sender'&&body.sender==='519990900001'){
  url='http://impulsa-qr-api-eval:8769/control/'+body.operation;method=body.operation==='notice-ready'?'POST':'GET';
  if(method==='POST')payload={sender:body.sender};else url+='?sender='+body.sender;
}else if(!(body.operation==='ack-once'&&Object.keys(body).join('|')==='operation'))throw Error('EVAL_CONTROL_OPERATION');
return [{json:{operation:body.operation,url,method,payload}}];
'''.strip()}}
    condition = {'id': 'ack-if', 'name': 'Sólo ACK de ensayo', 'type': 'n8n-nodes-base.if',
        'typeVersion': 2.2, 'position': [440, 0], 'parameters': {'conditions': {
            'options': {'caseSensitive': True, 'leftValue': '', 'typeValidation': 'strict', 'version': 2},
            'conditions': [{'id': 'is-ack', 'leftValue': '={{ $json.operation }}', 'rightValue': 'ack-once',
                'operator': {'type': 'string', 'operation': 'equals'}}], 'combinator': 'and'}, 'options': {}}}
    ack = {'id': 'ack', 'name': 'ACK clon una vez', 'type': 'n8n-nodes-base.httpRequest',
        'typeVersion': 4.4, 'position': [660, -160], 'parameters': {'method': 'POST',
            'url': BASE + '/webhook/' + bindings['path_prefix'] + '/ack-once',
            'authentication': 'genericCredentialType', 'genericAuthType': 'httpHeaderAuth',
            'sendBody': True, 'contentType': 'json', 'specifyBody': 'json', 'jsonBody': '{}',
            'options': {'timeout': 70000, 'redirect': {'redirect': {'followRedirects': False}},
                'response': {'response': {'responseFormat': 'json', 'fullResponse': True, 'neverError': True}}}},
        'credentials': {'httpHeaderAuth': copy.deepcopy(canonical['credential_refs']['private'])}, 'retryOnFail': False}
    http = {'id': 'api', 'name': 'API aislada', 'type': 'n8n-nodes-base.httpRequest', 'typeVersion': 4.4,
        'position': [880, 100], 'parameters': {'method': 'POST', 'url': '={{ $json.url }}',
            'authentication': 'genericCredentialType', 'genericAuthType': 'httpHeaderAuth',
            'sendBody': True, 'contentType': 'json', 'specifyBody': 'json',
            'jsonBody': '={{ JSON.stringify($json.payload) }}', 'options': {'timeout': 10000,
                'redirect': {'redirect': {'followRedirects': False}}, 'response': {'response': {
                    'responseFormat': 'json', 'fullResponse': True, 'neverError': True}}}},
        'credentials': {'httpHeaderAuth': copy.deepcopy(canonical['credential_refs']['control'])}, 'retryOnFail': False}
    post_if = {'id': 'api-post-if', 'name': 'Sólo inicio con cuerpo JSON', 'type': 'n8n-nodes-base.if',
        'typeVersion': 2.2, 'position': [660, 160], 'parameters': {'conditions': {
            'options': {'caseSensitive': True, 'leftValue': '', 'typeValidation': 'strict', 'version': 2},
            'conditions': [{'id': 'is-post', 'leftValue': '={{ $json.method }}', 'rightValue': 'POST',
                'operator': {'type': 'string', 'operation': 'equals'}}], 'combinator': 'and'}, 'options': {}}}
    get = {'id': 'api-get', 'name': 'API aislada · consulta', 'type': 'n8n-nodes-base.httpRequest',
        'typeVersion': 4.4, 'position': [880, 280], 'parameters': {'method': 'GET', 'url': '={{ $json.url }}',
            'authentication': 'genericCredentialType', 'genericAuthType': 'httpHeaderAuth',
            'sendBody': False, 'options': copy.deepcopy(http['parameters']['options'])},
        'credentials': {'httpHeaderAuth': copy.deepcopy(canonical['credential_refs']['control'])}, 'retryOnFail': False}
    reply = {'id': 'reply', 'name': 'Resultado de ensayo', 'type': 'n8n-nodes-base.respondToWebhook',
        'typeVersion': 1.4, 'position': [900, 0], 'parameters': {'respondWith': 'json',
            'responseBody': '={{ $json.body || $json }}', 'options': {'responseCode': '={{ $json.statusCode || 200 }}'}}}
    def edge(node): return {'node': node['name'], 'type': 'main', 'index': 0}
    return {'name': 'Escalar QR API1.1 isolated · fixed control 20261104',
        'nodes': [entry, prepare, condition, ack, http, reply, post_if, get], 'connections': {
            entry['name']: {'main': [[edge(prepare)]]}, prepare['name']: {'main': [[edge(condition)]]},
            condition['name']: {'main': [[edge(ack)], [edge(post_if)]]},
            ack['name']: {'main': [[edge(reply)]]}, http['name']: {'main': [[edge(reply)]]},
            post_if['name']: {'main': [[edge(http)], [edge(get)]]}, get['name']: {'main': [[edge(reply)]]}},
        'settings': {'executionOrder': 'v1', 'saveDataErrorExecution': 'none',
            'saveDataSuccessExecution': 'none', 'saveManualExecutions': False,
            'callerPolicy': 'workflowsFromSameOwner', 'availableInMCP': False}}

def build_proposal():
    generated=graph()
    return {'schema':'escalarai.notifier-controller-r06-proposal.v1',
        'state':'OFF_LOCAL_PROPOSAL_NOT_REGISTERED', 'workflow':{**generated,'active':False},
        'expected_existing_controller_id':'pNYNOdf96YGoffVb',
        'requires_fresh_ROOT_metadata_and_readback':True,
        'historical_build_guard_reused':False,'remote_operations':0,'models':0}

def build(*args,**kwargs):
    raise RuntimeError('R06_LEGACY_BUILD_DISABLED_USE_BUILD_PROPOSAL_AND_NEW_ROOT_STAGER')

if __name__=='__main__':
    print(json.dumps({'state':'INERT_CONTROLLER_PROPOSAL_SOURCE_ONLY',
        'registration_pending':True,'remote_operations':0,'models':0}))
