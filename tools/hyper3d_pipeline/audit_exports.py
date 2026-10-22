"""Read-only export validation and CSV catalog for all image inputs in a folder."""
import argparse,csv,json,zipfile
from pathlib import Path
from job_state import Job,sha256,verify_zip
ROOT=Path(__file__).resolve().parent

def audit(folder):
    folder=Path(folder).resolve();rows=[]
    for image in sorted(p for p in folder.rglob('*') if p.suffix.lower() in ('.png','.jpg','.jpeg','.webp')):
        record=ROOT/'jobs'/sha256(image)/'job.json'
        if not record.exists():
            rows.append({'image':str(image.relative_to(folder)),'stage':'not_submitted'});continue
        j=Job(record).data;out=j.get('output',{}).get('path','')
        if j['stage']=='complete':
            assert Path(out)==image.with_suffix('.zip'),out
            details=verify_zip(out)
            assert details['sha256']==j['output']['sha256'],out
            with zipfile.ZipFile(out) as z:
                for name in z.namelist():
                    if name.lower().endswith('.fbx'):assert z.open(name).read(23).startswith(b'Kaydara FBX Binary'),name
                    if name.lower().endswith('.png'):assert z.open(name).read(8)==b'\x89PNG\r\n\x1a\n',name
        rows.append({'image':str(image.relative_to(folder)),'stage':j['stage'],'model_url':j['model_url'],'zip':out,'free_redos':j.get('free_redos',0),'quality_note':j.get('quality_note',''),'error':j.get('last_error') if j['stage']!='complete' else ''})
    urls=[r['model_url'] for r in rows if r.get('model_url')]
    assert len(urls)==len(set(urls)),'Model URL reused across inputs'
    fields=['image','stage','model_url','zip','free_redos','quality_note','error']
    with (folder/'conversion_catalog.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fields);writer.writeheader();writer.writerows(rows)
    report={'inputs':len(rows),'complete':sum(r['stage']=='complete' for r in rows),'unique_model_urls':len(urls),'free_redos':sum(r.get('free_redos',0) for r in rows),'zip_crc_fbx_and_png_checks':'passed for completed exports','unfinished':[r['image'] for r in rows if r['stage']!='complete'],'quality_notes':{r['image']:r['quality_note'] for r in rows if r.get('quality_note')}}
    (folder/'conversion_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2));return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder');audit(parser.parse_args().folder)
