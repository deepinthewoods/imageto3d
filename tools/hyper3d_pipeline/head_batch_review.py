"""Small evidence helpers for supervised head-sheet conversion."""
from pathlib import Path
import csv,json,time
from PIL import Image,ImageDraw
import workflow as w

def captures(runner,tag):
    folder=w.ROOT/'runs'/'heads';folder.mkdir(parents=True,exist_ok=True)
    paths=[]
    for handle,job in runner.active.items():
        runner.driver.switch_to.window(handle)
        if w.observe(runner.driver)['stage']!='geometry_preview' or 'geometry_confirm' in job.data['actions']:continue
        time.sleep(3)
        path=folder/(Path(job.data['input']['path']).parent.name+'_'+Path(job.data['input']['path']).stem+'_'+tag+'.png')
        runner.driver.save_screenshot(str(path));paths.append((job,path))
    for start in range(0,len(paths),4):
        board=Image.new('RGB',(1500,1640),'white');draw=ImageDraw.Draw(board)
        for i,(job,path) in enumerate(paths[start:start+4]):
            im=Image.open(path);im=im.crop((400,240,1110,965));im.thumbnail((745,775))
            x=(i%2)*750;y=(i//2)*820
            board.paste(im,(x,y+35));draw.text((x+8,y+8),str(Path(job.data['input']['path']).relative_to(w.ROOT.parent/'heads')),fill='black')
        out=folder/(tag+'_'+str(start//4)+'.jpg');board.save(out);print(out,flush=True)
    return paths

def catalog(runner):
    root=w.ROOT.parent/'heads';rows=[]
    for image in sorted(root.rglob('*.png')):
        from job_state import Job
        j=Job.create(w.ROOT/'jobs',image).data
        rows.append({'image':str(image.relative_to(root)),'stage':j['stage'],'model_url':j['model_url'],'zip':j.get('output',{}).get('path',''),'free_redos':j.get('free_redos',0),'quality_note':j.get('quality_note',''),'error':j.get('last_error','')})
    with (root/'conversion_catalog.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,list(rows[0]));writer.writeheader();writer.writerows(rows)
    return rows
