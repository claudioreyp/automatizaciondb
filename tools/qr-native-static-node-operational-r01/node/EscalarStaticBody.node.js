/** Native node POC; load only from a reviewed read-only, SHA-pinned source pack. */
'use strict';
const fs=require('node:fs');
const path=require('node:path');
const {createRuntime,checkedLibraryPins,sha}=require('./trusted-runtime.cjs');
const {createCodeValidation}=require('./code-output.cjs');
let cached;
function runtime(){
  if(cached)return cached;
  const registry=JSON.parse(fs.readFileSync(path.join(__dirname,'registry.json'),'utf8'));
  if(registry.schema!=='escalarai.native-static-node-registry.v1')throw Error('NATIVE_REGISTRY_SCHEMA');
  const libraries=Object.create(null);
  for(const lib of checkedLibraryPins(registry.libraries)){
    const filename=require.resolve(lib.name);
    if(sha(fs.readFileSync(filename))!==lib.sha256)throw Error('NATIVE_MODULE_FILE_PIN');
    libraries[lib.name]=require(lib.name);
  }
  cached=createRuntime({entries:registry.entries,libraries,validation:createCodeValidation(require),taskTimeoutMs:registry.task_timeout_ms});
  return cached;
}
class EscalarStaticBody{
  constructor(){this.description={displayName:'Escalar pinned static body POC',name:'escalarStaticBody',group:['transform'],version:1,
    description:'Run one exact reviewed static wrapper without TaskRunner transport',defaults:{name:'Escalar pinned static body'},inputs:['main'],outputs:['main'],
    properties:[{displayName:'Mode',name:'mode',type:'options',noDataExpression:true,options:[{name:'Run Once for All Items',value:'runOnceForAllItems'}],default:'runOnceForAllItems'},
      {displayName:'Pinned wrapper',name:'jsCode',type:'string',noDataExpression:true,default:'',typeOptions:{rows:4}}]};}
  async execute(){
    if(this.continueOnFail())throw Error('NATIVE_CONTINUE_ON_FAIL_NOT_PINNED');
    const node=this.getNode(),mode=node.parameters.mode||'runOnceForAllItems',input=this.getInputData();
    const result=await runtime().execute({workflowId:this.getWorkflow().id,nodeId:node.id,wrapper:node.parameters.jsCode,mode,
      proxyFactory:index=>this.getWorkflowDataProxy(index),inputCount:input.length,inputItems:input,helpers:this.helpers,signal:this.getExecutionCancelSignal()});
    return [result];
  }
}
module.exports={EscalarStaticBody};
