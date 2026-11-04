async function body_ecc99cc78ad5af018717cda2153926d37a5a12b8d4ca5e388b9f93642a5905aa(__POC_ENV,__POC_DATA,__POC_CAPTURE){
const input=(__POC_ENV.reads["$input"]()).first(),outcome=fn=>{try{return {kind:'return',type:typeof fn()};}catch(e){return {kind:'throw',name:e.name,message:e.message};}};
if(input.json.case_name==='null')return null;
if(input.json.case_name==='malformed')return [{json:1}];
if(input.json.case_name==='isolation'){input.json.payload.deep=999;const rows=__POC_ENV.lookups[0]();rows[0].json.payload.deep=888;return [{json:{case_name:'isolation',input_changed:input.json.payload.deep,query_changed:rows[0].json.payload.deep},pairedItem:{item:0}}];}
const callback=()=>({text:input.json.text,workflow_id:(__POC_ENV.reads["String"]())((__POC_ENV.reads["$workflow"]()).id),execution_id:(__POC_ENV.reads["String"]())((__POC_ENV.reads["$execution"]()).id)});
return [{json:{case_name:input.json.case_name,result:await (__POC_ENV.reads["Promise"]()).resolve().then(callback),types:{URL:(__POC_ENV.types["URL"]()),fetch:(__POC_ENV.types["fetch"]()),AbortSignal:(__POC_ENV.types["AbortSignal"]())},reads:{URL:outcome(()=>(__POC_ENV.reads["URL"]())),fetch:outcome(()=>(__POC_ENV.reads["fetch"]())),AbortSignal:outcome(()=>(__POC_ENV.reads["AbortSignal"]()))}}}];
}

async function body_7bc1108c934b5c81dc6e3cde459312e567aa917ee9fd698f5b4607e7d62d3470(__POC_ENV,__POC_DATA,__POC_CAPTURE){
const bytes=(__POC_ENV.reads["Buffer"]()).from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==",'base64');if(bytes.length!==70)throw (__POC_ENV.reads["Error"]())('PNG70');return [{json:(__POC_ENV.reads["$input"]()).first().json,binary:{image:await this.helpers.prepareBinaryData(bytes,'native-static.png','image/png')}}];
}

async function body_3682e61578c18c275e67b8c6b715d5f4091af932761cfc9957f62cd9f1a1accd(__POC_ENV,__POC_DATA,__POC_CAPTURE){
const item=(__POC_ENV.reads["$input"]()).first(),bytes=await this.helpers.getBinaryDataBuffer(0,'image');return [{json:{bytes:bytes.length,base64:bytes.toString('base64'),descriptor:item.binary.image,execution_id:(__POC_ENV.reads["String"]())((__POC_ENV.reads["$execution"]()).id),workflow_id:(__POC_ENV.reads["String"]())((__POC_ENV.reads["$workflow"]()).id)}}];
}

async function body_b1921e75210e5e97f23879d8ed7fcc72592c78f3fb3a1bccb838aaf4bdc6c001(__POC_ENV,__POC_DATA,__POC_CAPTURE){
const current=(__POC_ENV.reads["$input"]()).first(),linked=__POC_ENV.lookups[0](),first=__POC_ENV.lookups[1](),all=__POC_ENV.lookups[2]();return [{json:{slot:current.json.slot,linked_slot:linked.json.slot,first_slot:first.json.slot,all_slots:all.map(row=>row.json.slot)},pairedItem:{item:0}}];
}

async function body_2ca161777ef07b2e08bddbddfe7ec46ad70083d68e59b8fc516dade98545d8a9(__POC_ENV,__POC_DATA,__POC_CAPTURE){
const input=(__POC_ENV.reads["$input"]()).first().json,history=__POC_ENV.lookups[0]().json;if(history.schema!=='PUBLIC_SYNTHETIC_HISTORY'||history.padding.length!==204541||input.hop>=180)throw (__POC_ENV.reads["Error"]())('FINITE_BENCHMARK');return [{json:{hop:input.hop+1,public_input:input.public_input,history_bytes:history.padding.length},pairedItem:{item:0}}];
}

module.exports=Object.freeze({body_ecc99cc78ad5af018717cda2153926d37a5a12b8d4ca5e388b9f93642a5905aa,body_7bc1108c934b5c81dc6e3cde459312e567aa917ee9fd698f5b4607e7d62d3470,body_3682e61578c18c275e67b8c6b715d5f4091af932761cfc9957f62cd9f1a1accd,body_b1921e75210e5e97f23879d8ed7fcc72592c78f3fb3a1bccb838aaf4bdc6c001,body_2ca161777ef07b2e08bddbddfe7ec46ad70083d68e59b8fc516dade98545d8a9});
