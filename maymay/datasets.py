import io
import json
import hashlib
import random
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from PIL import Image, ImageOps
from sqlalchemy import select
from .db import Item, Dataset, Character, KV, uid
from .storage import sha_file

IMAGE={'.png','.jpg','.jpeg','.webp'}
VIDEO={'.mp4','.mov','.mkv','.webm'}

def safe_members(z,max_bytes=4*1024**3):
    total=0
    if len(z.infolist())>10000:raise ValueError('Too many ZIP entries')
    for i in z.infolist():
        p=PurePosixPath(i.filename.replace('\\','/'))
        if p.is_absolute() or '..' in p.parts or (i.external_attr>>16)&0o170000==0o120000:raise ValueError('Unsafe ZIP member')
        total+=i.file_size
        if total>max_bytes or (i.file_size>50*1024**2 and i.compress_size and i.file_size/i.compress_size>500):raise ValueError('ZIP too large')
    return z.infolist()

def ingest_file(db,storage,dataset,path,name,caption='',group=None):
    ext=Path(name).suffix.lower()
    if ext not in IMAGE|VIDEO:raise ValueError('Only images and videos')
    sha=sha_file(path)
    duplicate=db.scalar(select(Item).where(Item.dataset_id==dataset.id,Item.sha==sha))
    if duplicate:return None
    quality={}
    if ext in IMAGE:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            quality={'width':im.width,'height':im.height,'small':min(im.size)<512}
            gray=ImageOps.grayscale(im).resize((9,8))
            pixels=list(gray.get_flattened_data());bits=[pixels[y*9+x]>pixels[y*9+x+1] for y in range(8) for x in range(8)]
            if max(pixels)-min(pixels)>8:
                dhash=sum(int(b)<<n for n,b in enumerate(bits));quality['dhash']=f'{dhash:016x}'
                for other in db.scalars(select(Item).where(Item.dataset_id==dataset.id)):
                    previous=other.quality.get('dhash')
                    if previous and (dhash ^ int(previous,16)).bit_count()<=4:
                        quality['near_duplicate_of']=other.id;group=other.group_id;break
    iid=uid();key=f'datasets/{dataset.id}/{iid}{ext}'
    storage.put(path,key)
    item=Item(id=iid,dataset_id=dataset.id,name=Path(name).name,key=key,sha=sha,kind='image' if ext in IMAGE else 'video',caption=caption[:12000],group_id=group or iid,quality=quality,accepted=not bool(quality.get('near_duplicate_of')))
    db.add(item);dataset.revision+=1
    return item

def import_zip(db,storage,dataset,path):
    added=0;duplicates=0
    with zipfile.ZipFile(path) as z:
        members=safe_members(z)
        # Validate all images and captions before committing any database changes.
        media=[i for i in members if Path(i.filename).suffix.lower() in IMAGE|VIDEO and not i.is_dir() and '__MACOSX' not in i.filename]
        names=set(z.namelist());known_groups={}
        if len(names)!=len(members):raise ValueError('ZIP содержит повторяющиеся пути')
        captions={}
        for entry in members:
            if entry.filename.endswith('metadata.jsonl') and not entry.is_dir():
                parent=PurePosixPath(entry.filename).parent
                for line in z.read(entry).decode('utf-8-sig').splitlines():
                    if not line.strip():continue
                    row=json.loads(line)
                    if not isinstance(row,dict) or not isinstance(row.get('file_name'),str) or not isinstance(row.get('text'),str):
                        raise ValueError('metadata.jsonl: требуется file_name и text (строки)')
                    key=str(parent/row['file_name'])
                    if key in captions and captions[key]!=row['text']:raise ValueError('Неоднозначные captions metadata.jsonl')
                    captions[key]=row['text']
        txt_by_stem={}
        media_by_stem={}
        for entry in members:
            if entry.filename.endswith('.txt'):
                txt_by_stem.setdefault(PurePosixPath(entry.filename).stem,[]).append(entry.filename)
        for entry in media:media_by_stem.setdefault(PurePosixPath(entry.filename).stem,[]).append(entry.filename)
        if 'manifest.json' in names:
            manifest=json.loads(z.read('manifest.json'))
            known_groups={i.get('sha256'):i.get('group') for i in manifest.get('items',[]) if i.get('sha256') and i.get('group')}

        with tempfile.TemporaryDirectory() as td:
            pending=[]
            for n,i in enumerate(media):
                target=Path(td)/f'{n}{Path(i.filename).suffix.lower()}'
                with z.open(i) as f,target.open('wb') as out:
                    import shutil;shutil.copyfileobj(f,out)
                if target.suffix in IMAGE:
                    with Image.open(target) as im:im.verify()
                cp=str(Path(i.filename).with_suffix('.txt')).replace('\\','/')
                if cp not in names:
                    candidates=txt_by_stem.get(PurePosixPath(i.filename).stem,[])
                    if candidates and (len(candidates)!=1 or len(media_by_stem[PurePosixPath(i.filename).stem])!=1):
                        raise ValueError('Неоднозначное соответствие изображений и отдельных captions: '+i.filename)
                    cp=candidates[0] if candidates else None
                caption=z.read(cp).decode('utf-8-sig')[:12000] if cp else captions.get(i.filename,'')[:12000]
                if cp and i.filename in captions and caption!=captions[i.filename][:12000]:raise ValueError('TXT и metadata.jsonl содержат разные captions')
                pending.append((target,i.filename,caption))
            for target,name,caption in pending:
                item=ingest_file(db,storage,dataset,target,name,caption,known_groups.get(sha_file(target)))
                if item:added+=1
                else:duplicates+=1
    return {'added':added,'duplicates':duplicates}

def snapshot(db,storage,dataset,validation_ratio=0.15):
    char=db.get(Character,dataset.character_id)
    items=db.scalars(select(Item).where(Item.dataset_id==dataset.id,Item.accepted==True).order_by(Item.id)).all()
    if not items:raise ValueError('Dataset has no accepted items')
    missing=[i.name for i in items if char.trigger not in i.caption or not i.caption.strip()]
    if missing:raise ValueError('Add captions with trigger '+char.trigger+': '+', '.join(missing[:5]))
    groups=sorted(set(i.group_id for i in items));random.Random(42).shuffle(groups)
    n=min(len(groups)-1,max(1,round(len(groups)*validation_ratio))) if len(groups)>1 and validation_ratio else 0
    val=set(groups[:n]);manifest=[]
    with tempfile.TemporaryDirectory() as td:
        zp=Path(td)/'dataset.zip'
        with zipfile.ZipFile(zp,'w',zipfile.ZIP_STORED) as z:
            for i in items:
                split='validation' if i.group_id in val else 'train'
                name=f'{split}/{i.id}{Path(i.name).suffix.lower()}'
                f=Path(td)/'item';storage.get(i.key,f)
                info=zipfile.ZipInfo(name,date_time=(2020,1,1,0,0,0))
                with z.open(info,'w') as out, f.open('rb') as src:
                    import shutil;shutil.copyfileobj(src,out)
                z.writestr(zipfile.ZipInfo(str(Path(name).with_suffix('.txt')),date_time=(2020,1,1,0,0,0)),i.caption)
                manifest.append({'id':i.id,'source_name':i.name,'sha256':i.sha,'group':i.group_id,'split':split,'kind':i.kind,'caption':i.caption})
            z.writestr(zipfile.ZipInfo('manifest.json',date_time=(2020,1,1,0,0,0)),json.dumps({'dataset_id':dataset.id,'revision':dataset.revision,'trigger':char.trigger,'items':manifest},ensure_ascii=False,indent=2))
        sha=sha_file(zp);key=f'snapshots/{dataset.id}/{sha}.zip';storage.put(zp,key)
    record_key='snapshot:'+dataset.id+':'+sha[:40]
    if not db.get(KV,record_key):db.add(KV(key=record_key,value={'key':key,'dataset_id':dataset.id,'revision':dataset.revision,'sha256':sha}))
    return {'key':key,'sha256':sha,'revision':dataset.revision,'train':sum(i['split']=='train' for i in manifest),'validation':len(items)-sum(i['split']=='train' for i in manifest),'kinds':sorted(set(i.kind for i in items))}
