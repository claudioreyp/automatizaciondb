/** Trusted, hash-bound component POC. No arbitrary Code, I/O or ambient fallback. */
'use strict';
const vm=require('node:vm');
const {createHash}=require('node:crypto');
const SHA=/^[a-f0-9]{64}$/;
const sha=value=>createHash('sha256').update(value).digest('hex');
const need=(ok,code)=>{if(!ok)throw Error(code);};
const PACKAGE='@escalar/static-code-body-poc-r01';
const R03='@escalar/qr-native-r03';
const FIXTURES='@escalar/native-static-node-poc-fixtures-r01';
const LIBRARIES=new Set([PACKAGE,R03]);
const MODES=new Set(['runOnceForAllItems']);
const NULL_RESULT=Symbol('runner-null-result');
const JSON_TEXT_KEYS=Object.freeze({object:Object.freeze({singular:'object',plural:'objects'})});
function checkedLibraryPins(libraries){
  need(Array.isArray(libraries)&&libraries.length===2,'NATIVE_TWO_LIBRARY_PINS');
  need(new Set(libraries.map(lib=>lib.name)).size===2,'NATIVE_DUPLICATE_LIBRARY_PIN');
  return libraries.map(lib=>{need(LIBRARIES.has(lib.name)&&SHA.test(lib.sha256),'NATIVE_LIBRARY_PIN_DENIED');return Object.freeze({...lib});});
}

// TaskRunner.send/receive use JSON.stringify/parse. DataProxy values are data,
// not a writable view into n8n's live runData. Cache each root per invocation.
function createIsolation(){
  const cache=new WeakMap(),active=new WeakSet();
  return function isolate(value){
    if(value===null)return null;
    if(typeof value==='number')return Number.isFinite(value)?value:null;
    if(typeof value==='bigint')throw TypeError('Do not know how to serialize a BigInt');
    if(typeof value==='function'||typeof value==='symbol')return undefined;
    if(typeof value!=='object')return value;
    if(active.has(value))throw TypeError('Converting circular structure to JSON');
    if(cache.has(value))return cache.get(value);
    if(typeof value.toJSON==='function')return isolate(value.toJSON());
    const copy=Array.isArray(value)?[]:{};cache.set(value,copy);active.add(value);
    try{
      if(Array.isArray(value)){for(let i=0;i<value.length;i++)copy[i]=isolate(value[i])??null;}
      else for(const key of Object.keys(value)){const item=isolate(value[key]);if(item!==undefined)Object.defineProperty(copy,key,{value:item,writable:true,enumerable:true,configurable:true});}
      return copy;
    }finally{active.delete(value);}
  };
}
function proxyFacade(proxy,isolate){
  const nominalIsolation=createIsolation(),metadataIsolation=createIsolation();
  const wrap=(target,copy)=>new Proxy(Object.create(null),{
    get(_unused,key){
      if(typeof key==='symbol')return undefined;
      const value=Reflect.get(target,key,target);
      return typeof value==='function'?(...args)=>copy(Reflect.apply(value,target,args)):copy(value);
    },
    set(){throw Error('NATIVE_PROXY_READ_ONLY');},
    defineProperty(){throw Error('NATIVE_PROXY_READ_ONLY');},deleteProperty(){throw Error('NATIVE_PROXY_READ_ONLY');},
  });
  return {
    $input:wrap(proxy.$input,isolate),
    $:name=>{need(typeof name==='string','NATIVE_NOMINAL_NAME');return wrap(proxy.$(name),nominalIsolation);},
    $workflow:wrap(proxy.$workflow,metadataIsolation),$execution:wrap(proxy.$execution,metadataIsolation),$mode:proxy.$mode,
  };
}

// This runs in a fresh VM, never in the n8n process realm. Do not call the
// runner's preventPrototypePollution() against the host process constructors.
const HARDEN=[
  'globalThis.global=globalThis;var module={exports:{}};',
  'Object.getPrototypeOf=()=>({});Reflect.getPrototypeOf=()=>({});Object.setPrototypeOf=()=>false;Reflect.setPrototypeOf=()=>false;',
  'delete Error.prepareStackTrace;delete Error.captureStackTrace;',
  'Object.defineProperty(Error,"prepareStackTrace",{configurable:false,writable:false,value:undefined});',
  'Object.defineProperty(Error,"captureStackTrace",{configurable:false,writable:false,value:undefined});',
  'Object.defineProperty=()=>({});Object.defineProperties=()=>({});',
  '[Object,Function,Array,String,Number,Boolean,RegExp,Error,TypeError,RangeError,SyntaxError,ReferenceError,Promise,Symbol,Map,Set,WeakMap,WeakSet,Date,JSON,Math,Reflect,ArrayBuffer,DataView,Int8Array,Uint8Array,Float32Array,Float64Array].forEach(c=>{try{Object.freeze(c);}catch{}});',
].join('\n');

function createRuntime({entries,libraries,validation,taskTimeoutMs}){
  need(Array.isArray(entries)&&entries.length>0&&entries.length<=256,'NATIVE_REGISTRY_BOUND');
  need(Number.isSafeInteger(taskTimeoutMs)&&taskTimeoutMs>=1&&taskTimeoutMs<=300000,'NATIVE_ORIGINAL_TASK_TIMEOUT_PIN');
  need(typeof validation?.validateRunCodeAllItems==='function'&&typeof validation?.normalizeItems==='function','NATIVE_OFFICIAL_VALIDATOR');
  const pins=new Map(),scripts=new Map();
  for(const input of entries){
    const entry=Object.freeze({...input});
    need(typeof entry.workflow_id==='string'&&typeof entry.node_id==='string'&&SHA.test(entry.wrapper_sha256)&&MODES.has(entry.mode),'NATIVE_REGISTRY_ENTRY');
    need(LIBRARIES.has(entry.package_name)&&/^body_[a-f0-9]{64}$/.test(entry.body_key),'NATIVE_BODY_PIN');
    const key=entry.workflow_id+'\0'+entry.node_id;need(!pins.has(key),'NATIVE_DUPLICATE_BINDING');pins.set(key,entry);
  }
  const cached=Object.create(null);
  for(const [name,library]of Object.entries(libraries)){
    need(LIBRARIES.has(name)&&library&&typeof library==='object'&&Object.isFrozen(library),'NATIVE_LIBRARY_MUST_BE_FROZEN');
    cached[name]=library;
  }
  Object.freeze(cached);
  for(const e of pins.values())need(typeof cached[e.package_name]?.[e.body_key]==='function','NATIVE_BODY_EXPORT_MISSING');

  async function execute({workflowId,nodeId,wrapper,mode='runOnceForAllItems',proxyFactory,inputCount,inputItems=[],helpers={},signal}){
    const entry=pins.get(String(workflowId)+'\0'+String(nodeId));
    need(entry&&entry.wrapper_sha256===sha(wrapper)&&entry.mode===mode,'NATIVE_WRAPPER_BINDING');
    need(typeof wrapper==='string'&&wrapper.startsWith('return await require('+JSON.stringify(entry.package_name)+')['+JSON.stringify(entry.body_key)+']'),'NATIVE_EXACT_STATIC_WRAPPER');
    need(typeof proxyFactory==='function'&&Number.isSafeInteger(inputCount)&&inputCount>=0&&inputCount<=1000&&Array.isArray(inputItems),'NATIVE_INPUT_BOUND');
    const check=()=>{if(signal?.aborted)throw Error('NATIVE_EXECUTION_CANCELLED');};check();
    const run=async itemIndex=>{
      check();const isolate=createIsolation(),proxy=proxyFacade(proxyFactory(itemIndex),isolate),timers=new Set();
      let disposed=false,abortListener,deadlineTimer;
      const alive=()=>{check();need(!disposed,'NATIVE_INVOCATION_FINISHED');};
      const timer=(fn,ms,...args)=>{alive();need(typeof fn==='function','NATIVE_TIMER_CALLBACK');const handle=setTimeout(()=>{timers.delete(handle);if(!disposed&&!signal?.aborted)fn(...args);},ms);timers.add(handle);return handle;};
      const facade=Object.freeze({helpers:Object.freeze(Object.fromEntries(['getBinaryDataBuffer','prepareBinaryData'].map(name=>[name,async(...args)=>{
        alive();need(typeof helpers[name]==='function','NATIVE_BINARY_HELPER_UNAVAILABLE');
        const value=await Reflect.apply(helpers[name],helpers,args);alive();return Buffer.isBuffer(value)?Buffer.from(value):isolate(value);
      }])))});
      const restrictedRequire=name=>{alive();need(typeof name==='string'&&(name===entry.package_name||name===R03),'NATIVE_REQUIRE_DENIED');need(cached[name],'NATIVE_LIBRARY_UNAVAILABLE');return cached[name];};
      const context=vm.createContext({__isExecutionContext:true,...proxy,items:isolate(inputItems),
        Buffer,require:restrictedRequire,setTimeout:timer,clearTimeout:handle=>{clearTimeout(handle);timers.delete(handle);},__nativeThis:facade},
        {name:'Escalar pinned static component',codeGeneration:{strings:false,wasm:false}});
      let script=scripts.get(entry.wrapper_sha256);
      if(!script){script=new vm.Script(HARDEN+'\nmodule.exports=(async function(){'+wrapper+'\n}).call(__nativeThis);',
        {filename:'pinned-static-'+entry.wrapper_sha256+'.js'});scripts.set(entry.wrapper_sha256,script);}
      const cancellation=new Promise((_,reject)=>{
        abortListener=()=>reject(Error('NATIVE_EXECUTION_CANCELLED'));
        signal?.addEventListener('abort',abortListener,{once:true});
        deadlineTimer=setTimeout(()=>reject(Error('NATIVE_TASK_TIMEOUT')),taskTimeoutMs);
      });
      try{
        check();const result=await Promise.race([script.runInContext(context,{timeout:taskTimeoutMs}),cancellation]);check();
        // Match runner null→[] and its JSON wire boundary before Code validation.
        return result===null?NULL_RESULT:result===undefined?undefined:JSON.parse(JSON.stringify(result));
      }finally{
        disposed=true;clearTimeout(deadlineTimer);signal?.removeEventListener('abort',abortListener);
        for(const handle of timers)clearTimeout(handle);timers.clear();
      }
    };
    const result=await run(0);
    return result===NULL_RESULT?[]:validation.validateRunCodeAllItems(result,JSON_TEXT_KEYS,validation.normalizeItems);
  }
  return Object.freeze({execute,diagnostics:()=>Object.freeze({compiled_scripts:scripts.size,bound_wrappers:pins.size})});
}
module.exports=Object.freeze({createRuntime,checkedLibraryPins,sha,PACKAGE,R03,FIXTURES});
