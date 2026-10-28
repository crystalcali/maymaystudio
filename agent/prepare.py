import json
import re
import subprocess
from pathlib import Path
from .runner import download,emit

def probe_video(src):
    return json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(src)]))

def prepare(job,work):
    p=job['params'];result={'captions':[],'media':[]}
    if job['recipe']=='dataset.caption':
        import torch
        from transformers import AutoProcessor,Qwen2_5_VLForConditionalGeneration
        from PIL import Image
        model_id='Qwen/Qwen2.5-VL-3B-Instruct'
        revision=p['caption_model']['revision']
        processor=AutoProcessor.from_pretrained(model_id,revision=revision,min_pixels=256*28*28,max_pixels=1024*28*28)
        model=Qwen2_5_VLForConditionalGeneration.from_pretrained(model_id,revision=revision,torch_dtype=torch.bfloat16,device_map='auto')
        for n,item in enumerate(p['items']):
            file=download(item['url'],work/(item['id']+Path(item['name']).suffix))
            instruction='Write one factual English training caption. Include framing, pose, visible clothing, expression, lighting and surroundings. Do not invent identity or details. End with person_visible=yes/no and face_visible=yes/no.'
            instruction+=' '+str(p.get('dataset_settings',{}).get('caption_rules',''))[:6000]
            sampled=item['kind']=='video'
            if sampled:
                duration=float(probe_video(file)['format']['duration']);frames=[]
                for k in range(8):
                    image_path=work/f'{item["id"]}_sample{k}.png'
                    subprocess.run(['ffmpeg','-nostdin','-y','-ss',str(duration*(k+.5)/8),'-i',str(file),'-frames:v','1',str(image_path)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    with Image.open(image_path) as im:frames.append(im.convert('RGB').copy())
                messages=[{'role':'user','content':[{'type':'video','video':frames},{'type':'text','text':instruction+' Describe visible actions across these sampled video frames.'}]}]
                text=processor.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
                inputs=processor(text=[text],videos=[frames],videos_kwargs={'fps':8/max(duration,.1)},return_tensors='pt').to(model.device)
            else:
                with Image.open(file) as im:image=im.convert('RGB').copy()
                messages=[{'role':'user','content':[{'type':'image','image':image},{'type':'text','text':instruction}]}]
                text=processor.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
                inputs=processor(text=[text],images=[image],return_tensors='pt').to(model.device)
            with torch.inference_mode():ids=model.generate(**inputs,max_new_tokens=180,do_sample=False)
            caption=processor.batch_decode(ids[:,inputs.input_ids.shape[1]:],skip_special_tokens=True)[0].strip()
            lower=caption.lower();quality={'person_visible':True if 'person_visible=yes' in lower else False if 'person_visible=no' in lower else None,'face_visible':True if 'face_visible=yes' in lower else False if 'face_visible=no' in lower else None,'caption_review_required':True,'sampled_video_frames':8 if sampled else 0}
            caption=re.split('person_visible=',caption,flags=re.I)[0].strip(' ,.;')
            result['captions'].append({'id':item['id'],'caption':p['trigger']+', '+caption,'quality':quality})
            emit('Описание материалов',progress=(n+1)/len(p['items']))
    else:
        duration=float(p.get('clip_seconds',3));overlap=float(p.get('overlap',.5));fps=int(p.get('fps',16));frames_only=bool(p.get('frames_only'));scene_cut=bool(p.get('scene_cut',True))
        if not 1<=duration<=15 or not 0<=overlap<duration or not 1<=fps<=30:raise ValueError('Invalid clip settings')
        for n,item in enumerate(p['items']):
            src=download(item['url'],work/(item['id']+Path(item['name']).suffix))
            length=float(probe_video(src)['format']['duration']);index=0;boundaries=[0,length]
            if length/duration>1000:raise ValueError('Video too long; split it before import')
            if scene_cut:
                emit('Поиск смен сцен')
                scan=subprocess.run(['ffmpeg','-nostdin','-i',str(src),'-vf',"select='gt(scene,0.35)',showinfo",'-an','-f','null','-'],text=True,stderr=subprocess.PIPE,stdout=subprocess.DEVNULL,check=True)
                cuts=[float(v) for v in re.findall(r'pts_time:([\d.]+)',scan.stderr)]
                boundaries=sorted(set([0,*cuts,length]))
            for left,right in zip(boundaries,boundaries[1:]):
                start=left
                while start+duration<=right:
                    ext='.png' if frames_only else '.mp4';target=work/f'{item["id"]}_{index:04}{ext}'
                    args=['ffmpeg','-nostdin','-y','-ss',str(start),'-i',str(src)]
                    if frames_only:args+=['-frames:v','1',str(target)]
                    else:
                        frames=int(duration*fps);frames=((frames-1)//4)*4+1
                        args+=['-vf',f'fps={fps}','-frames:v',str(frames),'-an','-c:v','libx264','-crf','18',str(target)]
                    subprocess.run(args,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    result['media'].append({'path':str(target),'kind':'dataset_image' if frames_only else 'dataset_video','meta':{'group_id':item['group_id'],'caption':item.get('caption',''),'quality':{'review_required':True,'source_video':item['id'],'start':start}}})
                    index+=1;start+=duration-overlap
            emit('Нарезка видео',progress=(n+1)/len(p['items']))
    if not result['captions'] and not result['media']:raise ValueError('No suitable frames/clips produced; shorten clip duration or review source')
    (work/'prepared.json').write_text(json.dumps(result,ensure_ascii=False))
