/** Tripo Studio Firefox multi-view submit/collect. Requires a signed-in ordinary Firefox profile. */
import { firefox } from 'playwright';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { gatedPaidClick } from './paid_click.mjs';

const [command, sourceArg, outputArg, topologyArg] = process.argv.slice(2);
if (!['submit', 'collect', 'retry', 'balance'].includes(command)) throw new Error('Usage: node studio.mjs submit <views_dir> <output_dir> [Triangle|Quad] | collect <output_dir> | retry <previous_output_dir> <output_dir> | balance');
if (['submit','retry'].includes(command) && (!sourceArg || !outputArg)) throw new Error(`${command} needs two directories`);
if (command === 'collect' && !sourceArg) throw new Error('Collect needs output_dir');
const DEFAULT_TOPOLOGY = 'Triangle';
const source = command === 'submit' ? path.resolve(sourceArg) : null;
const output = command === 'balance' ? null : path.resolve(command === 'collect' ? sourceArg : outputArg);
const topology = topologyArg ?? DEFAULT_TOPOLOGY;
if (command === 'submit' && !['Triangle','Quad'].includes(topology)) throw new Error('Topology must be Triangle or Quad');
const views = ['front.png','front_three_quarter.png','back_three_quarter.png','back.png'];
// Studio upload slots by label, in the same order as `views`.
const slotLabels = {'front.png':'Front','front_three_quarter.png':'Left','back_three_quarter.png':'Right','back.png':'Back'};
// Three-view modules: <views_dir>/views_omitted.json {"omitted":["back"],"reason":..,"date":..} (tools/tripo_pipeline/views_omitted.py
// has the same rules): front is always required, exactly one other view (or exactly both three-quarter views: a
// two-view front + back module) may be omitted and an omitted view's PNG must not exist.
function readOmitted(dir){
 const file=path.join(dir,'views_omitted.json');
 if(!fs.existsSync(file))return {omitted:[],marker:null};
 const data=JSON.parse(fs.readFileSync(file,'utf8'));
 if(!data||!Array.isArray(data.omitted))throw new Error('views_omitted.json needs {"omitted":["<view>"],"reason":..,"date":..}');
 const names=data.omitted.map(n=>String(n).replace(/\.png$/,''));
 if(names.includes('front'))throw new Error('views_omitted.json: front is always required and cannot be omitted');
 const known=views.map(v=>v.replace(/\.png$/,''));
 const unknown=names.filter(n=>!known.includes(n));
 if(unknown.length)throw new Error(`views_omitted.json: unknown view ${unknown.join(',')}`);
 const twoView=names.length===2&&names.includes('front_three_quarter')&&names.includes('back_three_quarter');
 if(names.length!==1&&!twoView)throw new Error(`views_omitted.json: omit exactly one non-front view, or both three-quarter views; got ${JSON.stringify(names)}`);
 for(const n of names)if(fs.existsSync(path.join(dir,n+'.png')))throw new Error(`views_omitted.json omits ${n} but ${n}.png exists`);
 return {omitted:names,marker:{omitted:names,reason:data.reason??null,date:data.date??null}};
}
const recordPath = output && path.join(output,'studio_submission.json');
const profile = path.join(process.env.XDG_DATA_HOME ?? path.join(os.homedir(),'.local','share'),'tripo-studio-playwright-firefox');
const write = record => fs.writeFileSync(recordPath,JSON.stringify(record,null,2)+'\n');
const cookieScript = fileURLToPath(new URL('./import_firefox_cookies.py',import.meta.url));
// Windows (2026-11-08): `python` (python3 there is a different interpreter); no ordinary Firefox profile to copy cookies from,
// so a missing profile means: rely on the persistent Playwright profile's own Tripo login (log in once in that window).
const PY = process.platform==='win32' ? 'python' : 'python3';
// Profile reservation (phase 5 review, phase-4 finding 8; tripo_browser_lock.py): the profile above is shared by every checkout, so
// this script never opens it unless the reservation of that RESOLVED profile is held. A python caller (convert_job, convert_queue)
// holds it and passes IMAGETO3D_TRIPO_PROFILE_LOCK / _TOKEN, verified here; without them (a manual run, the preview app's balance
// check, an older worker) the command is re-run under `tripo_browser_lock.py run`, which takes the reservation first (waits up to
// $IMAGETO3D_TRIPO_PROFILE_WAIT s, default 600, then exits 75). A reservation that fails verification is refused (exit 1).
const LOCK_TOOL = fileURLToPath(new URL('./tripo_browser_lock.py',import.meta.url));
const LOCK_ENV = 'IMAGETO3D_TRIPO_PROFILE_LOCK', TOKEN_ENV = 'IMAGETO3D_TRIPO_PROFILE_TOKEN';
if(!process.env[LOCK_ENV]){
 const run=spawnSync(PY,['-X','utf8',LOCK_TOOL,'run','--profile',profile,'--',process.execPath,fileURLToPath(import.meta.url),...process.argv.slice(2)],{stdio:'inherit'});
 if(run.error)console.error(`cannot reserve the Tripo profile (${run.error.message}); not opening it`);
 process.exit(run.status ?? 1);
}
// Browser lifetime (recheck-before-cutover finding 18): the reservation belongs to the python holder; if it dies, this process and
// its Firefox still have the profile open. So `verify --register-pid` records this node process beside the profile (pid + create
// time, <profile>.imageto3d.pid; it prints the path) and the next acquirer treats the profile as busy while this process, or a
// Firefox started on the profile, still runs. The file is removed only after the command ended normally (context closed); after
// an error it stays and the next acquirer clears it once nothing uses the profile.
// Final-verify row 18: the check and the registration are ONE handshake with acquisition (tripo_browser_lock.register_browser): the
// one-time token of the holder that spawned this process is compared with the live reservation file and the pid is published under
// the same handshake lock an acquirer holds, so this process cannot register once its holder is gone and a replacement took the
// profile (it is refused here and never opens the profile). Never verify first and register in a second call.
let browserPidFile=null;
{
 const check=spawnSync(PY,['-X','utf8',LOCK_TOOL,'verify','--profile',profile,'--lock',process.env[LOCK_ENV],'--token',process.env[TOKEN_ENV]??'','--register-pid',String(process.pid)],{encoding:'utf8',stdio:['ignore','pipe','pipe']});
 if(check.status!==0){
  console.error(`Tripo profile reservation refused: ${((check.stdout||'')+(check.stderr||'')).trim().split('\n')[0]||check.error?.message||`verify exit ${check.status}`}; not opening ${profile}`);
  process.exit(1);
 }
 browserPidFile=(check.stdout||'').trim().split('\n').pop()||null;
}
function unregisterBrowser(){
 if(!browserPidFile)return;
 try{if(JSON.parse(fs.readFileSync(browserPidFile,'utf8')).pid===process.pid)fs.unlinkSync(browserPidFile);}catch{}
}
// Click-time paid gate (DESIGN_PHASE4 I.8, tools/paid_gate.py): with IMAGETO3D_PAID_GATE set (convert_job passes it to a paid submit)
// the gate is checked right before the Generate click; null = open (or no gate: a manual run), else the reason it is closed.
// Recheck finding 6: "right before" means AFTER Playwright's actionability wait (paid_click.mjs gatedPaidClick: trial click first,
// then this gate, then the intent and the dispatch), never before an auto-waiting click(). Final-verify row 6: the dispatch itself
// does not auto-wait either (a forced click on the element pinned before the gate: delivered at once or failed), and the gate is
// asked again after it: a hold that took effect meanwhile makes the outcome `uncertain` (needs review), never `clicked`.
const GATE_TOOL = fileURLToPath(new URL('../paid_gate.py',import.meta.url));
const EXIT_GATE_CLOSED = 4;
function paidGateClosed(){
 if(!process.env.IMAGETO3D_PAID_GATE)return null;
 const run=spawnSync(PY,['-X','utf8',GATE_TOOL,'check'],{encoding:'utf8',stdio:['ignore','pipe','pipe']});
 if(run.status===0)return null;
 return (run.stdout||'').trim().split('\n')[0]||(run.stderr||'').trim().split('\n').pop()||run.error?.message||`gate check exit ${run.status}`;
}
function getCookies(){
 let cookies;
 try{
  const run=spawnSync(PY,['-X','utf8',cookieScript],{encoding:'utf8',stdio:['ignore','pipe','pipe']});
  if(run.status!==0)throw Object.assign(new Error((run.stderr||'').trim().split('\n').pop()||`exit ${run.status}`),{status:run.status});
  cookies=JSON.parse(run.stdout);
 }catch(err){
  if(process.platform!=='win32')throw err;
  console.error(`Tripo cookie import failed (${String(err.message).slice(0,200)}); using the Playwright profile login`);return [];
 }
 if(process.platform==='win32'&&!cookies.length)console.error('Tripo cookie import found no unexpired Tripo cookies in the ordinary Firefox profile (sign in to Tripo there, or set TRIPO_FIREFOX_PROFILE); using the Playwright profile login');
 return cookies;
}
function pngInfo(file){
 const bytes=fs.readFileSync(file).subarray(0,26);
 if(bytes.length<26||!bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))||bytes.toString('ascii',12,16)!=='IHDR')throw new Error(`Expected PNG: ${file}`);
 if(bytes[24]!==8||bytes[25]!==6)throw new Error(`Expected 8-bit RGBA PNG: ${file}`);
 return [bytes.readUInt32BE(16),bytes.readUInt32BE(20)];
}
// Windows: no private display, so the browser runs headless (PIPELINE_SHOW_WINDOWS=1 shows it). Linux hides it on Xvfb instead.
const HEADLESS=process.platform==='win32'&&process.env.PIPELINE_SHOW_WINDOWS!=='1';
// Run the browser on a private X display so no window pops up (PIPELINE_SHOW_WINDOWS=1 shows it); see tools/hidden_display.py.
function browserEnv(){
 if(process.platform==='win32')return undefined;
 const display=execFileSync(PY,[fileURLToPath(new URL('../hidden_display.py',import.meta.url)),'start','--parent',String(process.pid)],{encoding:'utf8'}).trim();
 return display?{...process.env,DISPLAY:display}:undefined;
}
async function openStudio(url){
 const context=await firefox.launchPersistentContext(profile,{headless:HEADLESS,env:browserEnv(),viewport:{width:1440,height:900},acceptDownloads:true});
 try{
  await context.addCookies(getCookies());const page=context.pages()[0]??await context.newPage();
  await page.goto(url,{waitUntil:'domcontentloaded',timeout:60000});
  await page.getByRole('button',{name:'Reject All'}).first().click({timeout:1500}).catch(()=>{});
  return {context,page};
 }catch(err){await context.close();throw err;}
}
// Close promo/announcement dialogs whose backdrop would block every click (seen 2026-10-24).
async function dismissDialogs(page,shotDir){
 await page.waitForTimeout(2500);
 for(let i=0;i<4;i++){
  const overlay=page.locator('div.fixed.inset-0[data-state="open"]');
  if(!(await overlay.count().catch(()=>0)))return;
  if(shotDir&&i===0)await page.screenshot({path:path.join(shotDir,'dialog_dismissed.png')}).catch(()=>{});
  await page.keyboard.press('Escape').catch(()=>{});
  await page.locator('[role="dialog"] button[aria-label*="lose" i], [role="dialog"] button:has-text("Close"), [role="dialog"] button:has-text("Got it"), [role="dialog"] button:has-text("Skip"), [role="dialog"] button:has-text("Later")').first().click({timeout:1000}).catch(()=>{});
  await page.waitForTimeout(800);
 }
}
async function submit(){
 if(fs.existsSync(recordPath))throw new Error(`Submission record already exists; collect or inspect before retrying: ${recordPath}`);
 const {omitted,marker}=readOmitted(source);
 const present=views.filter(name=>!omitted.includes(name.replace(/\.png$/,'')));
 for(const name of present)if(!fs.existsSync(path.join(source,name)))throw new Error(`Missing ${name}`);
 const dimensions=present.map(name=>pngInfo(path.join(source,name)));
 if(dimensions.some(([width,height])=>width!==dimensions[0][0]||height!==dimensions[0][1]))throw new Error(`Multi-view inputs have different native dimensions: ${JSON.stringify(dimensions)}`);
 fs.mkdirSync(output,{recursive:true});
 const polycountMax=topology==='Quad'?50000:2000000;
 const record={source,images:present,...(omitted.length?{views_omitted:marker}:{}),input_resolution:dimensions[0],settings:{workflow:'HD Model / Multi Images to 3D',model:'H3.0',texture:'2K',pbr:false,topology,remove_lighting:true,polycount_max:polycountMax},status:'preparing',started_at:new Date().toISOString()};write(record);
 const {context,page}=await openStudio('https://studio.tripo3d.ai/workspace/generate');
 try{
  await dismissDialogs(page,output);
  await page.locator('div[class*="i-tripo:multi-view"]').click();
  if(await page.getByText('H3.1 – Best Quality').count()){
   await page.getByText('H3.1 – Best Quality').click();await page.getByText('H3.0 – Fast & Balanced').click();
  }else if(!await page.getByText('H3.0 – Fast & Balanced').count())throw new Error('Cannot verify H3.0 model');
  await page.getByText('Geometry & Texture').first().click();
  const panel=page.getByRole('dialog',{name:'Geometry & Texture'});
  const pbr=panel.getByText('PBR',{exact:true}).locator('xpath=..').getByRole('switch');
  const lighting=panel.getByText('Remove Lighting',{exact:true}).locator('xpath=..').getByRole('switch');
  if(await pbr.getAttribute('aria-checked')==='true')await pbr.click();
  if(await lighting.getAttribute('aria-checked')==='false')await lighting.click();
  await panel.getByText('2K',{exact:true}).click();await panel.getByText(topology,{exact:true}).click();
  const poly=panel.locator('input[type=text]');
  if(await poly.count()!==1)throw new Error('Cannot locate polygon ceiling control');
  await poly.fill(String(polycountMax));await poly.press('Tab');
  if(Number(await poly.inputValue())!==polycountMax)throw new Error('Polygon ceiling did not stick');
  if(await pbr.getAttribute('aria-checked')!=='false')throw new Error('PBR did not disable');
  if(await lighting.getAttribute('aria-checked')!=='true')throw new Error('Remove Lighting did not enable');
  const files=page.locator('input[type=file][accept^="image/"]');
  if(await files.count()!==4)throw new Error('Expected four image upload slots');
  // A filled slot loses its input, so the remaining inputs are looked up by their Front/Left/Right/Back label each time;
  // an omitted view's slot simply stays empty.
  const labelsNow=()=>page.evaluate(()=>[...document.querySelectorAll('input[type=file][accept^="image/"]')].map(el=>{let a=el;for(let k=0;k<3&&a.parentElement;k++)a=a.parentElement;return (a.innerText.match(/Front|Left|Right|Back/)??[''])[0];}));
  const pending=async()=>{const now=await labelsNow();return present.filter(name=>now.includes(slotLabels[name]));};
  // Studio uploads asynchronously and a failed upload ("Upload failed. Please try again.") empties its slot again some
  // seconds later, while Generate stays enabled. So wait until every slot has stayed filled for 8 s, re-uploading any
  // slot that came back empty (at most 6 re-uploads), and check again right before the click.
  let stable=0,reuploads=0;
  for(let tick=0;tick<90&&stable<8;tick++){
   const todo=await pending();
   if(todo.length){
    stable=0;
    if((reuploads+=todo.length)>present.length+6)throw new Error(`Studio did not keep the uploads for: ${todo.join(', ')}`);
    for(const name of todo){
     const index=(await labelsNow()).indexOf(slotLabels[name]);
     await files.nth(index).setInputFiles(path.join(source,name));await page.waitForTimeout(1500);
    }
   }else stable++;
   await page.waitForTimeout(1000);
  }
  if(stable<8)throw new Error('Studio uploads did not settle');
  const left=await labelsNow();const expectLeft=views.filter(v=>!present.includes(v)).map(v=>slotLabels[v]).sort().join();
  if(left.slice().sort().join()!==expectLeft)throw new Error(`Upload slots left empty ${JSON.stringify(left)}, expected ${expectLeft||'none'}`);
  await page.waitForTimeout(2000);
  const cost=topology==='Quad'?30:25;
  const generate=page.getByRole('button',{name:new RegExp(`Generate\\s*${cost}`)}).first();
  await generate.waitFor({timeout:15000});
  record.displayed_cost_credits=cost;record.status='ready_to_submit';record.ready_at=new Date().toISOString();write(record);
  await page.screenshot({path:path.join(output,'before_submit.png')});
  if(process.env.TRIPO_DRY_RUN==='1'){
   record.status='dry_run';write(record);console.log(JSON.stringify({status:record.status,cost,screenshot:path.join(output,'before_submit.png')}));return;
  }
  const responses=[];
  page.on('response',async response=>{
   if(!response.url().includes('/v2/studio/operation/multiview_to_model'))return;
   try{const j=await response.json();const d=j.data??{};responses.push({http_status:response.status(),code:j.code,message:j.message,task_id:d.task_id,project_id:d.project_id,operation_id:d.operation_id});}catch{}
  });
  // Studio disables Generate while another model of this account is generating; wait it out
  // rather than failing (a failed click is never charged, but leaves a stale record).
  const queueDeadline=Date.now()+Number(process.env.TRIPO_QUEUE_WAIT_MIN??30)*60000;
  // Finding 6: every wait (the Studio queue, the upload slots, Playwright's actionability: visible, stable, enabled, not covered)
  // ends BEFORE the paid gate is asked; the gate, the intent and the dispatch then follow at once (gatedPaidClick).
  let click;
  for(let attempt=1;;attempt++){
   while(!(await generate.isEnabled())){
    if(Date.now()>queueDeadline)throw new Error('Generate stayed disabled; another Studio generation may be stuck');
    record.status='waiting_for_studio_queue';write(record);await page.waitForTimeout(15000);
   }
   click=await gatedPaidClick({control:generate,gateClosed:paidGateClosed,
    stillReady:async()=>{const empty=await pending();return empty.length?`Upload slot emptied before Generate: ${empty.join(', ')}`:null;},
    writeIntent:()=>{record.status='submission_attempted';record.submit_clicked_at=new Date().toISOString();write(record);}});
   if(click.outcome!=='not_actionable')break;
   if(attempt>=3){
    // never clicked, no intent written: the record is set aside so a resume submits fresh
    record.status='not_clicked';record.not_clicked=click.error;write(record);
    fs.renameSync(recordPath,recordPath.replace(/\.json$/,`.not_clicked_${Date.now()}.json`));
    throw new Error(`Generate was not clicked (${click.error})`);
   }
   record.status='waiting_for_generate_control';record.generate_not_actionable=click.error;write(record);await page.waitForTimeout(5000);
  }
  if(click.outcome==='gate_closed'){
   // nothing was clicked: the record is set aside, so the resumed submit starts fresh; the caller waits for its gate (exit 4)
   const gate=click.gate;
   record.status='gate_closed';record.gate_closed=gate;write(record);
   fs.renameSync(recordPath,recordPath.replace(/\.json$/,`.gate_closed_${Date.now()}.json`));
   console.error(`PAID GATE CLOSED: ${gate}`);process.exitCode=EXIT_GATE_CLOSED;return;
  }
  // 'clicked', or 'uncertain': the dispatch failed / timed out after the intent was written, or the gate closed while it was under way, so the click MAY have happened.
  await page.waitForTimeout(7000);
  record.response=responses[0]??null;record.project_id=record.response?.project_id??page.url().match(/[a-f0-9-]{36}$/)?.[0]??null;
  const delivered=record.response?.code===0&&Boolean(record.project_id);
  const uncertain=click.outcome==='uncertain'&&!delivered;
  if(click.outcome==='uncertain'){record.click_delivery=delivered?'confirmed by the Studio response':'uncertain';record.click_error=click.error;}
  if(uncertain)record.review='The Generate click may or may not have happened (the intent is kept): check the Studio workspace and the credit balance. This record is never re-clicked automatically.';
  record.status=delivered?'submitted':'submission_needs_review';record.after_submit_at=new Date().toISOString();write(record);
  await page.screenshot({path:path.join(output,'after_submit.png')}).catch(()=>{});
  if(uncertain)throw new Error(`Generate click delivery is uncertain (${click.error}); ${recordPath} needs review and is not re-clicked`);
  console.log(JSON.stringify({status:record.status,project_id:record.project_id,task_id:record.response?.task_id,cost}));
 }catch(err){
  // never clicked Generate: move the record aside so a resume submits fresh instead of stopping on it
  if(record.status==='preparing'&&fs.existsSync(recordPath))fs.renameSync(recordPath,recordPath.replace(/\.json$/,`.preparing_failed_${Date.now()}.json`));
  throw err;
 }finally{await context.close();}
}
async function downloadFormat(page,format,record){
 for(let attempt=1;;attempt++){
  try{return await downloadOnce(page,format,record);}
  catch(err){
   // The first export's toast/dialog sometimes blocks the Export button; clear it and retry.
   if(attempt>=3)throw err;
   await page.keyboard.press('Escape').catch(()=>{});
   await page.reload({waitUntil:'domcontentloaded'});
   await page.getByRole('button',{name:'Export',exact:true}).first().waitFor({timeout:60000});
  }
 }
}
async function downloadOnce(page,format,record){
 await page.getByRole('button',{name:'Export',exact:true}).first().click();
 const dialog=page.getByRole('dialog',{name:'Export'});await dialog.waitFor();
 const current=dialog.getByText(/^(FBX|OBJ|GLB)$/).first();await current.click();await page.getByText(format,{exact:true}).last().click();
 const promise=page.waitForEvent('download',{timeout:120000});await dialog.getByRole('button',{name:'Export'}).click();
 const download=await promise;const filename=download.suggestedFilename();const destination=path.join(output,`${format.toLowerCase()}_${filename}`);
 if(fs.existsSync(destination))throw new Error(`Export destination exists: ${destination}`);
 await download.saveAs(destination);
 record.exports??={};record.exports[format]={path:destination,bytes:fs.statSync(destination).size,downloaded_at:new Date().toISOString()};write(record);
 await page.keyboard.press('Escape').catch(()=>{});await page.waitForTimeout(1500);
}
// Studio's "Free Retry (×N)" regenerates a project from the same inputs and settings without
// charging credits (verified 2026-10-22: balance unchanged, counter 3 -> 2; the previous version
// stays in the project's model history). The click starts the retry at once (no confirmation).
// The new version is collected into a separate output dir with the ordinary collect command.
async function retry(){
 const previous=JSON.parse(fs.readFileSync(path.join(path.resolve(sourceArg),'studio_submission.json')));
 if(!previous.project_id)throw new Error('Previous submission has no project ID');
 if(fs.existsSync(recordPath))throw new Error(`Record already exists: ${recordPath}`);
 fs.mkdirSync(output,{recursive:true});
 const record={...previous,status:'retry_preparing',retry_of:path.resolve(sourceArg),started_at:new Date().toISOString()};
 for(const key of ['exports','studio_result','response','submit_clicked_at','after_submit_at'])delete record[key];
 write(record);
 const {context,page}=await openStudio(`https://studio.tripo3d.ai/workspace/generate/${previous.project_id}`);
 try{
  const button=page.locator('[data-trace="studio.workspace_generate.free_retry_button"]').first();
  await button.waitFor({timeout:90000});
  const left=Number((await button.innerText()).match(/×\s*(\d+)/)?.[1]??0);
  const credits=async()=>Number((await page.locator('body').innerText()).match(/(\d{1,7})\s*\n\s*Upgrade/)?.[1]);
  record.free_retries_left_before=left;record.credits_before=await credits();write(record);
  if(left<1){record.status='no_free_retries';write(record);throw new Error('No free retries left on this project');}
  if(process.env.TRIPO_DRY_RUN==='1'){record.status='dry_run';write(record);console.log(JSON.stringify({status:record.status,left}));return;}
  record.status='retry_attempted';record.submit_clicked_at=new Date().toISOString();write(record);
  await button.click();
  await page.getByText(/Generating/).first().waitFor({timeout:60000});
  await page.screenshot({path:path.join(output,'after_retry.png')});
  record.status='submitted';record.after_submit_at=new Date().toISOString();write(record);
  console.log(JSON.stringify({status:record.status,project_id:record.project_id,free_retries_left:left-1}));
 }finally{await context.close();}
}
// A slow reload (seen headless on Windows, 2026-11-09: 30 s domcontentloaded timeout while the model was generating)
// must not abort the collect poll: wait longer, then just poll the page again.
async function reloadPage(page){
 await page.reload({waitUntil:'domcontentloaded',timeout:120000}).catch(err=>console.error(`reload slow (${err.message.split('\n')[0]}); polling again`));
}
async function collect(){
 const record=JSON.parse(fs.readFileSync(recordPath));
 if(!record.project_id)throw new Error('Submission has no project ID; review record manually');
 const {context,page}=await openStudio(`https://studio.tripo3d.ai/workspace/generate/${record.project_id}`);
 try{
  // Poll until Studio shows the finished model (TRIPO_COLLECT_WAIT_MIN, default 30).
  const readyDeadline=Date.now()+Number(process.env.TRIPO_COLLECT_WAIT_MIN??30)*60000;
  let top,faces,vertices;
  for(;;){
   // after a free retry the old version can stay on screen until the new one is generated
   if(record.retry_of&&await page.getByText(/Generating/).count()){
    if(Date.now()>readyDeadline)throw new Error('Retry still generating');
    await page.waitForTimeout(30000);await reloadPage(page);continue;
   }
   await page.getByRole('button',{name:'Export',exact:true}).first().waitFor({timeout:60000}).catch(()=>{});
   const body=await page.locator('body').innerText();top=body.match(/Topology\s+(Quad|Triangle)/)?.[1];faces=Number(body.match(/Faces\s+(\d+)/)?.[1]);vertices=Number(body.match(/Vertices\s+(\d+)/)?.[1]);
   if(top&&faces)break;
   if(Date.now()>readyDeadline)throw new Error('Model is not ready or topology counts unavailable');
   await page.waitForTimeout(30000);await reloadPage(page);
  }
  record.studio_result={topology:top,faces,vertices,inspected_at:new Date().toISOString()};write(record);
  await page.screenshot({path:path.join(output,'studio_result.png')});
  for(const format of ['OBJ','GLB'])if(!record.exports?.[format])await downloadFormat(page,format,record);
  record.status='exported';write(record);console.log(JSON.stringify({status:record.status,project_id:record.project_id,...record.studio_result,exports:record.exports}));
 }finally{await context.close();}
}
// Read-only: print the account's credit balance as JSON ({"credits": N or null}). A balance it could not read ({"credits": null})
// exits 2, so no caller mistakes "unknown" for a reading (review_v2_phase4 finding 4; a browser error exits 1 as before).
async function balance(){
 const {context,page}=await openStudio('https://studio.tripo3d.ai/workspace/generate');
 try{
  let credits=null;
  for(let i=0;i<20&&credits===null;i++){await page.waitForTimeout(1000);credits=Number((await page.locator('body').innerText()).match(/(\d{1,7})\s*\n\s*Upgrade/)?.[1]??NaN);if(Number.isNaN(credits))credits=null;}
  console.log(JSON.stringify({credits}));
  if(credits===null){console.error('Tripo balance not found on the page');process.exitCode=2;}
 }finally{await context.close();}
}
if(command==='submit')await submit();else if(command==='retry')await retry();else if(command==='balance')await balance();else await collect();
unregisterBrowser();   // reached only when the command ended normally: its browser context is closed
