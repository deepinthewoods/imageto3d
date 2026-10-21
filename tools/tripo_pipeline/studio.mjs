/** Tripo Studio Firefox multi-view submit/collect. Requires a signed-in ordinary Firefox profile. */
import { firefox } from 'playwright';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const [command, sourceArg, outputArg, topologyArg] = process.argv.slice(2);
if (!['submit', 'collect'].includes(command)) throw new Error('Usage: node studio.mjs submit <views_dir> <output_dir> [Triangle|Quad] | collect <output_dir>');
if (command === 'submit' && (!sourceArg || !outputArg)) throw new Error('Submit needs views_dir and output_dir');
if (command === 'collect' && !sourceArg) throw new Error('Collect needs output_dir');
const DEFAULT_TOPOLOGY = 'Triangle';
const source = command === 'submit' ? path.resolve(sourceArg) : null;
const output = path.resolve(command === 'submit' ? outputArg : sourceArg);
const topology = topologyArg ?? DEFAULT_TOPOLOGY;
if (command === 'submit' && !['Triangle','Quad'].includes(topology)) throw new Error('Topology must be Triangle or Quad');
const views = ['front.png','front_three_quarter.png','back_three_quarter.png','back.png'];
const recordPath = path.join(output,'studio_submission.json');
const profile = path.join(process.env.XDG_DATA_HOME ?? path.join(os.homedir(),'.local','share'),'tripo-studio-playwright-firefox');
const write = record => fs.writeFileSync(recordPath,JSON.stringify(record,null,2)+'\n');
const cookieScript = fileURLToPath(new URL('./import_firefox_cookies.py',import.meta.url));
function getCookies(){return JSON.parse(execFileSync('python3',[cookieScript],{encoding:'utf8'}));}
function pngInfo(file){
 const bytes=fs.readFileSync(file).subarray(0,26);
 if(bytes.length<26||!bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))||bytes.toString('ascii',12,16)!=='IHDR')throw new Error(`Expected PNG: ${file}`);
 if(bytes[24]!==8||bytes[25]!==6)throw new Error(`Expected 8-bit RGBA PNG: ${file}`);
 return [bytes.readUInt32BE(16),bytes.readUInt32BE(20)];
}
async function openStudio(url){
 const context=await firefox.launchPersistentContext(profile,{headless:false,viewport:{width:1440,height:900},acceptDownloads:true});
 try{
  await context.addCookies(getCookies());const page=context.pages()[0]??await context.newPage();
  await page.goto(url,{waitUntil:'domcontentloaded',timeout:60000});
  await page.getByRole('button',{name:'Reject All'}).first().click({timeout:1500}).catch(()=>{});
  return {context,page};
 }catch(err){await context.close();throw err;}
}
async function submit(){
 if(fs.existsSync(recordPath))throw new Error(`Submission record already exists; collect or inspect before retrying: ${recordPath}`);
 for(const name of views)if(!fs.existsSync(path.join(source,name)))throw new Error(`Missing ${name}`);
 const dimensions=views.map(name=>pngInfo(path.join(source,name)));
 if(dimensions.some(([width,height])=>width!==dimensions[0][0]||height!==dimensions[0][1]))throw new Error(`Multi-view inputs have different native dimensions: ${JSON.stringify(dimensions)}`);
 fs.mkdirSync(output,{recursive:true});
 const polycountMax=topology==='Quad'?50000:2000000;
 const record={source,images:views,input_resolution:dimensions[0],settings:{workflow:'HD Model / Multi Images to 3D',model:'H3.0',texture:'2K',pbr:false,topology,remove_lighting:true,polycount_max:polycountMax},status:'preparing',started_at:new Date().toISOString()};write(record);
 const {context,page}=await openStudio('https://studio.tripo3d.ai/workspace/generate');
 try{
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
  for(const name of views){await files.first().setInputFiles(path.join(source,name));await page.waitForTimeout(700);}
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
  record.status='submission_attempted';record.submit_clicked_at=new Date().toISOString();write(record);
  await generate.click();await page.waitForTimeout(7000);
  record.response=responses[0]??null;record.project_id=record.response?.project_id??page.url().match(/[a-f0-9-]{36}$/)?.[0]??null;
  record.status=record.response?.code===0&&record.project_id?'submitted':'submission_needs_review';record.after_submit_at=new Date().toISOString();write(record);
  await page.screenshot({path:path.join(output,'after_submit.png')});
  console.log(JSON.stringify({status:record.status,project_id:record.project_id,task_id:record.response?.task_id,cost}));
 }finally{await context.close();}
}
async function downloadFormat(page,format,record){
 await page.getByRole('button',{name:'Export',exact:true}).first().click();
 const dialog=page.getByRole('dialog',{name:'Export'});await dialog.waitFor();
 const current=dialog.getByText(/^(FBX|OBJ|GLB)$/).first();await current.click();await page.getByText(format,{exact:true}).last().click();
 const promise=page.waitForEvent('download',{timeout:120000});await dialog.getByRole('button',{name:'Export'}).click();
 const download=await promise;const filename=download.suggestedFilename();const destination=path.join(output,`${format.toLowerCase()}_${filename}`);
 if(fs.existsSync(destination))throw new Error(`Export destination exists: ${destination}`);
 await download.saveAs(destination);
 record.exports??={};record.exports[format]={path:destination,bytes:fs.statSync(destination).size,downloaded_at:new Date().toISOString()};write(record);
}
async function collect(){
 const record=JSON.parse(fs.readFileSync(recordPath));
 if(!record.project_id)throw new Error('Submission has no project ID; review record manually');
 const {context,page}=await openStudio(`https://studio.tripo3d.ai/workspace/generate/${record.project_id}`);
 try{
  await page.getByRole('button',{name:'Export',exact:true}).first().waitFor({timeout:60000});
  const body=await page.locator('body').innerText();const top=body.match(/Topology\s+(Quad|Triangle)/)?.[1];const faces=Number(body.match(/Faces\s+(\d+)/)?.[1]);const vertices=Number(body.match(/Vertices\s+(\d+)/)?.[1]);
  if(!top||!faces)throw new Error('Model is not ready or topology counts unavailable');
  record.studio_result={topology:top,faces,vertices,inspected_at:new Date().toISOString()};write(record);
  await page.screenshot({path:path.join(output,'studio_result.png')});
  for(const format of ['OBJ','GLB'])if(!record.exports?.[format])await downloadFormat(page,format,record);
  record.status='exported';write(record);console.log(JSON.stringify({status:record.status,project_id:record.project_id,...record.studio_result,exports:record.exports}));
 }finally{await context.close();}
}
if(command==='submit')await submit();else await collect();
