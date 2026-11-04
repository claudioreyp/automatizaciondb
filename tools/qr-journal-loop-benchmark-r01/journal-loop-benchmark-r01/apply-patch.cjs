/* Docker build only. One exact allowlist addition; no application module load. */
'use strict';
const fs=require('node:fs');const path=require('node:path');const {sha,need}=require('./node/workload.cjs');
const pins=JSON.parse(fs.readFileSync(path.join(__dirname,'pins.json'),'utf8'));
const verify=process.argv.length===3&&process.argv[2]==='--verify';need(verify||process.argv.length===2,'PATCH_EXPLICIT_MODE');
for(const p of Object.values(pins.packages))need(sha(fs.readFileSync(p.package_json))===p.sha256,'BUILD_PACKAGE_PIN');
for(const p of pins.files)need(sha(fs.readFileSync(p.resolved))===(verify&&p.resolved===pins.patch.path?pins.patch.after_sha256:p.sha256),'BUILD_SOURCE_PIN');
if(verify){process.stdout.write(JSON.stringify({status:'VERIFIED_PATCHED_66_FILES',base_image:pins.base_image,after_sha256:pins.patch.after_sha256})+'\n');process.exit(0);}
const raw=fs.readFileSync(pins.patch.path,'utf8');need(raw.split(pins.patch.before).length===2,'ONE_LITERAL_ALLOWLIST');const patched=raw.replace(pins.patch.before,pins.patch.after);
need(sha(Buffer.from(patched))===pins.patch.after_sha256&&patched.replace(pins.patch.after,pins.patch.before)===raw,'PATCH_EXACT_INVERSE');fs.writeFileSync(pins.patch.path,patched);
process.stdout.write(JSON.stringify({schema:'escalarai.journal-allowlist-build.v1',path:pins.patch.path,before_sha256:pins.patch.before_sha256,after_sha256:pins.patch.after_sha256,only_added_type:'CUSTOM.escalarJournalLoop',captured_files_verified:pins.files.length})+'\n');
