"""Explicit future offline Docker proof. Inert on import; never deploys n8n.

Uses four fresh tmpfs SQLite containers, one per case. All provisioning, seed,
CAS reset and measured row I/O go through the official DataTable node. Nothing
opens a host/container production database or an existing container.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import statistics
import subprocess
import time

HERE=Path(__file__).resolve().parent
BASE='sha256:4f448824ec99e1160e49eeb1c5bf2130a5d244fe9029e871a9f4d9f126dbfc98'
WORKFLOW='JournalBenchR01Workflow'
TYPE='CUSTOM.escalarJournalLoop'
VARIANTS=['r12-shaped','pure-merge','native-loop']
CASES=[f'effects{n}-bytes{b}' for n in (4,13) for b in (7168,204800)]
FIELDS=['run_id','workflow_id','code_manifest_sha256','business_id','branch_id','head','state','owner_execution','nonce','body_json','body_hash']
sha=lambda raw:hashlib.sha256(raw).hexdigest()
compact=lambda obj:json.dumps(obj,separators=(',',':'),ensure_ascii=False)
def require(ok,code):
    if not ok: raise RuntimeError(code)
def execution(raw):
    text=raw.decode('utf8'); decoder=json.JSONDecoder(); found=[]
    for m in re.finditer(r'(?m)^\{',text):
        try:
            obj,_=decoder.raw_decode(text,m.start())
            if isinstance(obj,dict) and isinstance(obj.get('data',{}).get('resultData'),dict):found.append(obj)
        except json.JSONDecodeError:pass
    require(len(found)==1,'ONE_ACTUAL_CLI_EXECUTION');return found[0]
def item(result,name):
    visits=result['data']['resultData'].get('runData',{}).get(name)
    require(isinstance(visits,list) and len(visits)==1 and not visits[0].get('error'),'ONE_SUCCESSFUL_VISIT_'+name)
    data=visits[0].get('data',{}).get('main')
    require(isinstance(data,list) and len(data)==1 and len(data[0])==1 and not data[0][0].get('error'),'ONE_SUCCESSFUL_ITEM_'+name)
    return data[0][0]['json']
def verify_final(row,seed,effects):
    body=json.loads(seed['body_json'])
    for effect in range(effects):
        for phase in ('checkpoint','intent','result'):
            body['history'].append({'effect':effect,'phase':phase,'kind':'NO_HTTP_SYNTHETIC_INTENT' if phase=='intent' else 'SYNTHETIC_RESULT' if phase=='result' else 'SYNTHETIC_CHECKPOINT'})
    body_json=compact(body); expected={**seed,'head':3*effects,'state':'result','nonce':f"{seed['run_id'].split(':',1)[1]}:{effects-1}:result",'body_json':body_json,'body_hash':sha(body_json.encode())}
    actual={k:row.get(k) for k in ['id',*FIELDS]}
    require(actual==expected,'INDEPENDENT_COMPLETE_ROW_HISTORY_HASH');return expected
def audit_graph(g):
    require(g['id']==WORKFLOW and g['active'] is False and g['pinData']=={} and g['staticData']=={},'OFF_OWN_GRAPH')
    require(g['settings']['saveExecutionProgress'] is False and g['settings']['callerPolicy']=='none' and g['settings']['executionTimeout']==120,'EXACT_GRAPH_SETTINGS')
    allowed={'n8n-nodes-base.manualTrigger','n8n-nodes-base.dataTable',TYPE}
    require(0<len(g['nodes'])<600 and all(n['type'] in allowed and not n.get('credentials') and not n.get('retryOnFail') and not n.get('disabled') for n in g['nodes']),'OFFLINE_NODES_ONLY')
    require(sum(n['type']=='n8n-nodes-base.manualTrigger' for n in g['nodes'])==1,'MANUAL_ONLY')
def absent(result):
    return result.returncode==1 and (b'No such' in result.stderr or b'no such' in result.stderr)

def run(args):
    require(args.run and re.fullmatch('[a-f0-9]{64}',args.manifest_sha256),'EXPLICIT_RUN_AND_PIN')
    raw=(HERE/'manifest.json').read_bytes();require(sha(raw)==args.manifest_sha256,'OUTER_SOURCE_MANIFEST_PIN')
    manifest=json.loads(raw);require(manifest['schema']=='escalarai.journal-benchmark-source-pack.v1' and manifest['base_image']==BASE,'PACK_SCHEMA_BASE')
    actual={str(p.relative_to(HERE)).replace('\\','/') for p in HERE.rglob('*') if p.is_file()}
    require(actual==set(manifest['files'])|{'manifest.json'} and not any(p.is_symlink() for p in HERE.rglob('*')),'EXACT_SOURCE_PACK_NO_LINKS')
    for name,pin in manifest['files'].items():
        require('..' not in Path(name).parts and not Path(name).is_absolute(),'PACK_PATH')
        content=(HERE/name).read_bytes();require(len(content)==pin['bytes'] and sha(content)==pin['sha256'],'PACK_FILE_'+name)
    output=Path(args.output).resolve();require(not output.exists(),'FRESH_OUTPUT');output.mkdir(parents=True,mode=0o700)
    start=time.monotonic();deadline=start+1800;nonce=secrets.token_hex(12);tag='escalar-journal-bench-r01:'+nonce
    sequence=0;created=[];image_id=None;failure=None;cleanup_errors=[];metrics=[];components=[]
    def save(name,value):
        require('/' not in name and '\\' not in name,'RESULT_BASENAME');(output/name).write_bytes((json.dumps(value,indent=2,ensure_ascii=False)+'\n').encode())
    def command(label,argv,seconds=30,stdin=None,cleanup=False,env=None):
        nonlocal sequence
        sequence+=1;require(re.fullmatch('[a-z0-9-]+',label),'COMMAND_LABEL');stem=f'{sequence:03d}-{label}'
        timeout=min(seconds,deadline-time.monotonic()-(0 if cleanup else 90));require(timeout>0,'TOTAL_DEADLINE')
        save(stem+'.command.json',{'argv':argv,'timeout':timeout,'stdin_sha256':sha(stdin) if stdin else None,'cleanup':cleanup,'buildkit_disabled':env is not None and env.get('DOCKER_BUILDKIT')=='0'})
        began=time.monotonic()
        try:r=subprocess.run(argv,input=stdin,capture_output=True,timeout=timeout,env=env)
        except subprocess.TimeoutExpired as e:
            (output/(stem+'.stdout.raw')).write_bytes(e.stdout or b'');(output/(stem+'.stderr.raw')).write_bytes(e.stderr or b'');save(stem+'.exit.json',{'exit':'TIMEOUT_UNKNOWN_NO_RETRY','wall_ms':(time.monotonic()-began)*1000});raise
        wall=(time.monotonic()-began)*1000;(output/(stem+'.stdout.raw')).write_bytes(r.stdout);(output/(stem+'.stderr.raw')).write_bytes(r.stderr);save(stem+'.exit.json',{'exit':r.returncode,'wall_ms':wall})
        require(len(r.stdout)<=256*1024*1024 and len(r.stderr)<=4*1024*1024,'RAW_OUTPUT_BOUND');return r,wall
    def checked(label,argv,seconds=30,stdin=None,env=None):
        r,wall=command(label,argv,seconds,stdin,env=env);require(r.returncode==0,'COMMAND_FAILED_'+label);return r.stdout,wall
    def docker_exec(cid,*argv,seconds=30,stdin=None):
        return checked('exec-'+str(sequence+1),['docker','exec',*(['-i'] if stdin is not None else []),cid,*argv],seconds,stdin)
    def verify_owned(cid,info):
        require(info['Id']==cid and info['Image']==image_id and info['Config']['Labels'].get('escalar.journal-nonce')==nonce,'OWN_CONTAINER_ID_IMAGE_NONCE')
    def cleanup_container(target):
        r,_=command('cleanup-inspect',['docker','container','inspect',target],10,cleanup=True)
        if absent(r):return
        require(r.returncode==0,'CLEANUP_INSPECT');info=json.loads(r.stdout)[0];cid=info['Id'];verify_owned(cid,info)
        r,_=command('cleanup-owned-container',['docker','rm','-f',cid],15,cleanup=True);require(r.returncode==0,'CLEANUP_REMOVE')
        r,_=command('cleanup-confirm-absence',['docker','container','inspect',cid],10,cleanup=True);require(absent(r),'CLEANUP_ABSENCE')
    try:
        base=json.loads(checked('inspect-pinned-base',['docker','image','inspect',BASE])[0])[0];require(base['Id']==BASE,'EXACT_LOCAL_BASE_IMAGE')
        missing,_=command('fresh-image-tag',['docker','image','inspect',tag]);require(absent(missing),'FRESH_DERIVATIVE_TAG')
        checked('build-one-literal-derivative',['docker','build','--network=none','--pull=false','--build-arg','BASE_IMAGE='+BASE,'--label','escalar.journal-nonce='+nonce,'--label','escalar.journal-pack='+args.manifest_sha256,'--tag',tag,str(HERE)],90,env={**os.environ,'DOCKER_BUILDKIT':'0'})
        image=json.loads(checked('inspect-derivative',['docker','image','inspect',tag])[0])[0];image_id=image['Id'];require(re.fullmatch('sha256:[a-f0-9]{64}',image_id),'DERIVATIVE_ID')
        require(image['Config']['Labels'].get('escalar.journal-nonce')==nonce and image['RootFS']['Layers'][:len(base['RootFS']['Layers'])]==base['RootFS']['Layers'] and len(image['RootFS']['Layers'])==len(base['RootFS']['Layers'])+2,'DERIVATIVE_EXACT_BASE_PLUS_COPY_PATCH')
        save('derivative-image.json',{'base':BASE,'derived':image_id,'patch':manifest['patch'],'nonce':nonce})
        for case_index,case_id in enumerate(CASES):
            name='escalar-journal-bench-r01-'+nonce+'-'+str(case_index)
            env={'DB_TYPE':'sqlite','DB_SQLITE_DATABASE':'/home/node/.n8n/database.sqlite','N8N_USER_FOLDER':'/home/node','N8N_CUSTOM_EXTENSIONS':'/opt/escalar-journal-bench/node','N8N_RUNNERS_ENABLED':'true','N8N_RUNNERS_MODE':'internal','N8N_RUNNERS_INSECURE_MODE':'false','N8N_RUNNERS_MAX_CONCURRENCY':'1','N8N_DIAGNOSTICS_ENABLED':'false','N8N_VERSION_NOTIFICATIONS_ENABLED':'false','N8N_PERSONALIZATION_ENABLED':'false','N8N_LICENSE_AUTO_RENEW_ENABLED':'false','NODE_FUNCTION_ALLOW_EXTERNAL':'','NODE_OPTIONS':''}
            argv=['docker','create','--name',name,'--label','escalar.journal-nonce='+nonce,'--network','none','--read-only','--user','1000:1000','--memory','1536m','--cpus','1','--pids-limit','128','--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/home/node/.n8n:rw,uid=1000,gid=1000,mode=700','--tmpfs','/tmp:rw,uid=1000,gid=1000,mode=700']
            for k,v in env.items():argv+=['--env',k+'='+v]
            argv+=['--entrypoint','node',image_id,'-e','setInterval(()=>{},1000)']
            # Register the unique intended name before dispatch. A timeout after
            # Docker creation but before its ID response still has owned cleanup.
            created.append(name);cid=checked('create-fresh-sqlite-'+str(case_index),argv)[0].decode().strip();require(re.fullmatch('[a-f0-9]{64}',cid),'FRESH_CONTAINER_ID');created[created.index(name)]=cid
            info=json.loads(checked('inspect-isolation-'+str(case_index),['docker','container','inspect',cid])[0])[0];verify_owned(cid,info);host=info['HostConfig']
            require(host['NetworkMode']=='none' and host['ReadonlyRootfs'] and not host['PortBindings'] and not host.get('VolumesFrom'),'OFFLINE_READ_ONLY')
            require(info['Config']['User']=='1000:1000' and host['Memory']==1536*1024*1024 and host['NanoCpus']==1000000000 and host['PidsLimit']==128 and host['CapDrop']==['ALL'] and 'no-new-privileges:true' in host['SecurityOpt'],'RESOURCE_SECURITY')
            require(set(host['Tmpfs'])=={'/tmp','/home/node/.n8n'} and all(m['Type']=='tmpfs' and m['Destination'] in {'/tmp','/home/node/.n8n'} for m in info['Mounts']),'NO_EXISTING_VOLUMES_OR_BINDS')
            actual_env=dict(v.split('=',1) for v in info['Config']['Env']);require(all(actual_env.get(k)==v for k,v in env.items()),'EXACT_PROOF_ENV')
            checked('start-'+str(case_index),['docker','start',cid]);docker_exec(cid,'node','/opt/escalar-journal-bench/apply-patch.cjs','--verify');docker_exec(cid,'n8n','user-management:reset',seconds=45)
            graph_sequence=0
            def execute_graph(mode,input_arg,final_name,variant=None):
                nonlocal graph_sequence
                graph_sequence+=1;remote='/tmp/graph-'+str(graph_sequence)+'.json'
                gen=['node','/opt/escalar-journal-bench/generate.cjs',mode,input_arg,remote]
                if variant:gen.append(variant)
                docker_exec(cid,*gen);source=json.loads(docker_exec(cid,'cat',remote)[0]);audit_graph(source)
                save(f'case{case_index}-graph{graph_sequence:02d}.json',source)
                docker_exec(cid,'n8n','import:workflow','--input='+remote,seconds=40)
                # Exact readback before every measured variant. Other runs are
                # setup/components/reset, whose actual outputs are also checked.
                if mode=='bench':
                    exported='/tmp/export-'+str(graph_sequence)+'.json';docker_exec(cid,'n8n','export:workflow','--id='+WORKFLOW,'--output='+exported,seconds=40)
                    loaded=json.loads(docker_exec(cid,'cat',exported)[0]);require(len(loaded)==1,'ONE_OWN_EXPORTED_WORKFLOW')
                    for k in ('id','name','active','nodes','connections','settings','pinData','staticData'):require(loaded[0].get(k)==source.get(k),'EXACT_IMPORTED_'+k)
                stdout,wall=docker_exec(cid,'n8n','execute','--id='+WORKFLOW,'--rawOutput',seconds=135)
                result=execution(stdout);require(not result['data']['resultData'].get('error'),'EXECUTION_ERROR_NO_RETRY');final=item(result,final_name)
                visits=result['data']['resultData']['runData'];require(set(visits)=={n['name'] for n in source['nodes']},'ALL_AND_ONLY_EXPECTED_NODE_VISITS')
                require(all(len(v)==1 and not v[0].get('error') for v in visits.values()),'NO_NODE_ERRORS_OR_REEXECUTION')
                return final,source,result,wall
            binding,_,_,_=execute_graph('setup',case_id,'Bind fresh fixture');require(binding['schema']=='escalarai.journal-fresh-fixture.v1' and binding['case_id']==case_id and binding['workflow_id']==WORKFLOW,'OFFICIAL_FRESH_FIXTURE_RESULT')
            binding_path='/tmp/binding.json';docker_exec(cid,'node','-e',"const fs=require('fs');fs.writeFileSync('/tmp/binding.json',fs.readFileSync(0),{flag:'wx'});",stdin=(compact(binding)+'\n').encode())
            save('case'+str(case_index)+'-binding.json',binding)
            comp,_,_,_=execute_graph('component',binding_path,'Native components');require(comp['unapproved_rejected'] and comp['wrong_project_rejected'] and comp['stale_cas_zero'] and comp['filter_validation'] and comp['corrupt_readback_rejected'] and comp['cancel_before_operation'] and sum(comp['concurrency']['accepted'])==1,'ACTUAL_NATIVE_COMPONENTS')
            require([x['seam'] for x in comp['injected_seams']]==['abort_after_committed_update','lost_update_result','empty_readback','duplicate_readback'] and all(x['official_primary_calls']=={'update':1,'get':1} and x['retained_head']==1 for x in comp['injected_seams']) and comp['intervening_official_writer']['rejected'] and comp['intervening_official_writer']['primary_calls']=={'update':1,'get':1},'REAL_OPERATION_SYNTHETIC_FAULT_SEAMS')
            components.append(comp);prior=comp['final_row'];effects=int(re.search(r'effects([0-9]+)',case_id).group(1));trace_pin=None
            # Rotate order to reduce systematic warm-cache/order bias. SQLite is
            # reset outside the measured bracket, only after verified completion.
            for repetition in range(3):
                for variant in VARIANTS[repetition:]+VARIANTS[:repetition]:
                    reset_path='/tmp/reset-'+str(graph_sequence)+'.json';payload=compact({'binding':binding,'prior':prior}).encode()
                    docker_exec(cid,'node','-e',"const fs=require('fs');fs.writeFileSync(process.argv[1],fs.readFileSync(0),{flag:'wx'});",reset_path,stdin=payload)
                    reset_result,_,_,_=execute_graph('reset',reset_path,'Begin measured');require(reset_result['row']==binding['row'],'EXACT_RESET_SEED')
                    final,source,result,wall=execute_graph('bench',binding_path,'Finish measured',variant)
                    require(final['schema']=='escalarai.native-journal-benchmark-result.v1' and final['case_id']==case_id and final['variant']==variant and final['effects']==effects and final['operations']==effects*6 and final['external_effects']==final['models']==0,'FIXED_BENCHMARK_RESULT')
                    prior=verify_final(final['final_row'],binding['row'],effects);require(isinstance(final['measured_ms'],(int,float)) and final['measured_ms']>0,'ACTUAL_MEASURED_CLOCK')
                    trace_pin=trace_pin or final['trace_sha256'];require(final['trace_sha256']==trace_pin,'IDENTICAL_OPERATION_TRACE_ALL_VARIANTS')
                    if variant=='native-loop':require(final['proxy_observation']=={'getDataTableProxy':12*effects,'getColumns':6*effects,'updateRows':3*effects,'getManyRowsAndCount':3*effects},'NATIVE_OFFICIAL_PROXY_COUNTS')
                    else:
                        updates=[n for n in source['nodes'] if n['name'].startswith('Update')];reads=[n for n in source['nodes'] if n['name'].startswith('Readback ')];require(len(updates)==len(reads)==3*effects,'EXACT_SIX_DT_OPS_PER_EFFECT')
                        for n in updates+reads:
                            row=item(result,n['name']);require(row['id']==1 and row['run_id']==binding['row']['run_id'] and row['body_hash']==sha(row['body_json'].encode()),'OFFICIAL_DT_ROW_OUTPUT')
                    run_data=result['data']['resultData']['runData'];measured_names=[n['name'] for n in source['nodes'] if n['name'] not in {'Manual benchmark','Read initial fixture','Begin measured','Finish measured'}]
                    expected_hops=37*effects if variant=='r12-shaped' else 9*effects+1 if variant=='pure-merge' else 1;require(len(measured_names)==expected_hops,'EXACT_SHAPE_HOPS')
                    metrics.append({'case_id':case_id,'variant':variant,'repetition':repetition,'measured_ms':final['measured_ms'],'cli_wall_ms':wall,'node_time_sum_ms':sum(run_data[n][0]['executionTime'] for n in measured_names),'measured_hops':expected_hops,'operations':6*effects,'definition_bytes':len(compact(source).encode()),'initial_body_bytes':len(binding['row']['body_json'].encode()),'final_body_sha256':prior['body_hash'],'trace_sha256':trace_pin})
                    save('benchmarks-progress.json',metrics)
            cleanup_container(cid);created.remove(cid)
    except Exception as e:failure={'type':type(e).__name__,'message':str(e)}
    finally:
        for cid in list(created):
            try:cleanup_container(cid);created.remove(cid)
            except Exception as e:cleanup_errors.append({'container':cid,'error':str(e)})
        # Resolve even after a failed build. The unique nonce label is mandatory
        # before image removal; no force removal or shared image/tag deletion.
        try:
            r,_=command('cleanup-image-inspect',['docker','image','inspect',tag],10,cleanup=True)
            if not absent(r):
                require(r.returncode==0,'IMAGE_CLEANUP_INSPECT');owned_image=json.loads(r.stdout)[0];require(owned_image['Config']['Labels'].get('escalar.journal-nonce')==nonce,'OWN_DERIVATIVE_ONLY')
                r,_=command('cleanup-owned-image',['docker','image','rm',tag],15,cleanup=True);require(r.returncode==0,'OWN_IMAGE_REMOVAL')
                r,_=command('cleanup-image-absence',['docker','image','inspect',owned_image['Id']],10,cleanup=True);require(absent(r),'OWN_IMAGE_ABSENCE')
        except Exception as e:cleanup_errors.append({'image':tag,'error':str(e)})
        medians=[]
        for case_id in CASES:
            group={v:[m['measured_ms'] for m in metrics if m['case_id']==case_id and m['variant']==v] for v in VARIANTS}
            if all(len(v)==3 for v in group.values()):
                row={'case_id':case_id,'median_measured_ms':{v:statistics.median(times) for v,times in group.items()},'median_cli_wall_ms':{v:statistics.median(m['cli_wall_ms'] for m in metrics if m['case_id']==case_id and m['variant']==v) for v in VARIANTS},'median_node_time_sum_ms':{v:statistics.median(m['node_time_sum_ms'] for m in metrics if m['case_id']==case_id and m['variant']==v) for v in VARIANTS}}
                row['native_over_r12']=row['median_measured_ms']['native-loop']/row['median_measured_ms']['r12-shaped'];row['merge_over_r12']=row['median_measured_ms']['pure-merge']/row['median_measured_ms']['r12-shaped'];medians.append(row)
        status='PASS' if failure is None and not cleanup_errors and len(metrics)==36 and len(components)==4 else 'FAIL'
        final={'schema':'escalarai.native-journal-benchmark-proof.v1','status':status,'failure':failure,'cleanup_errors':cleanup_errors,'remaining_owned_containers':created,'components':components,'metrics':metrics,'medians':medians,'native_median_faster_than_r12_all_cases':status=='PASS' and len(medians)==4 and all(m['native_over_r12']<1 for m in medians),'base_image':BASE,'derived_image':image_id,'source_manifest_sha256':args.manifest_sha256,'wall_ms':(time.monotonic()-start)*1000,'network':'none','models':0,'external_effects':0,'production_changes':0,'scope':'SYNTHETIC_COMPONENT_ONLY','whole_agent_acceptance':False,'agent_latency_acceptance':False}
        save('result.json',final);print(json.dumps(final,ensure_ascii=False))
    return 0 if final['status']=='PASS' else 1
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',action='store_true');parser.add_argument('--manifest-sha256',required=True);parser.add_argument('--output',required=True);raise SystemExit(run(parser.parse_args()))
