"""Pure command construction. No shell execution and no torch imports."""
import sys
from pathlib import Path
from maymay.recipes import BY_ID

def script(root,name):return [sys.executable,str(Path(root)/'src/musubi_tuner'/name)]
def append(cmd,**flags):
    for key,value in flags.items():
        if value is None or value is False:continue
        cmd.append('--'+key)
        if value is not True:
            cmd.extend(str(v) for v in value) if isinstance(value,(list,tuple)) else cmd.append(str(value))
    return cmd

def train_commands(rid,p,c,root,work):
    family=BY_ID[rid]['family'];work=Path(work);out=work/'outputs';ds=work/'dataset.toml';commands=[]
    stem='krea2' if family=='krea2' else 'flux_2' if family=='klein9' else 'wan'
    cache=append(script(root,stem+'_cache_latents.py'),dataset_config=ds,vae=c['vae'])
    if family=='klein9':append(cache,model_version='klein-base-9b')
    if family=='wan22-i2v':append(cache,i2v=True)
    commands.append(('Кэш латентов',cache))
    text=append(script(root,stem+'_cache_text_encoder_outputs.py'),dataset_config=ds,batch_size=1)
    append(text,**({'t5':c['text_encoder']} if stem=='wan' else {'text_encoder':c['text_encoder']}))
    if family=='klein9':append(text,model_version='klein-base-9b')
    commands.append(('Кэш описаний',text))
    experts=['low','high'] if p.get('expert')=='both' and stem=='wan' else [p.get('expert','low')]
    for expert in experts:
        train=script(root,stem+'_train_network.py')
        train=[sys.executable,'-m','accelerate.commands.launch','--num_processes','1','--num_cpu_threads_per_process','2','--mixed_precision',p['precision']]+train[1:]
        append(train,dataset_config=ds,dit=c['raw'] if stem=='krea2' else c[expert] if stem=='wan' else c['dit'],vae=c['vae'],sdpa=True,
            mixed_precision=p['precision'],optimizer_type='adamw8bit',learning_rate=p['learning_rate'],gradient_checkpointing=True,
            gradient_accumulation_steps=p.get('gradient_accumulation',1),network_dim=p['rank'],network_alpha=p['rank'],
            network_module='networks.lora_'+('flux_2' if stem=='flux_2' else stem),max_train_steps=p['steps'],
            save_every_n_steps=p['save_every'],save_state=True,save_state_on_train_end=True,
            max_data_loader_n_workers=2,persistent_data_loader_workers=True,
            output_dir=out/(expert if stem=='wan' else 'main'),output_name='character_'+(expert if stem=='wan' else family),seed=p.get('seed',42),weighting_scheme='none')
        if stem=='krea2':append(train,timestep_sampling='shift',discrete_flow_shift=2.5)
        elif stem=='flux_2':append(train,model_version='klein-base-9b',timestep_sampling='flux2_shift')
        else:
            boundary=900 if family=='wan22-i2v' else 875
            append(train,task='i2v-A14B' if family=='wan22-i2v' else 't2v-A14B',timestep_sampling='shift',discrete_flow_shift=3,
                min_timestep=0 if expert=='low' else boundary,max_timestep=boundary if expert=='low' else 1000,preserve_distribution_shape=True)
        if p.get('fp8'):append(train,fp8_base=True,fp8_scaled=True)
        if p.get('blocks_to_swap'):append(train,blocks_to_swap=p['blocks_to_swap'])
        if p.get('resume_path'):
            if len(experts)>1:raise ValueError('Resume both experts as separate jobs')
            append(train,resume=p['resume_path'])
        if p.get('preview_every',0):
            append(train,sample_prompts=work/'sample_prompts.txt',sample_every_n_steps=p['preview_every'])
            append(train,**({'t5':c['text_encoder']} if stem=='wan' else {'text_encoder':c['text_encoder']}))
        commands.append(('Обучение '+expert if stem=='wan' else 'Обучение',train))
    return commands

def generate_command(rid,p,c,root,out,reference=None,adapters=None):
    family=BY_ID[rid]['family'];adapters=adapters or []
    if family=='krea2':
        cmd=script(root,'krea2_generate_image.py')+[p['prompt']]
        append(cmd,dit=c['dit'],vae=c['vae'],text_encoder=c['text_encoder'],steps=p['steps'],guidance_scale=1,mu=1.15,
               width=p['width'],height=p['height'],seed=p['seed'],save_path=out,attn_mode='torch')
        if p.get('fp8'):append(cmd,fp8_scaled=True)
    elif family=='klein9':
        cmd=append(script(root,'flux_2_generate_image.py'),model_version='klein-base-9b',dit=c['dit'],vae=c['vae'],text_encoder=c['text_encoder'],
             prompt=p['prompt'],negative_prompt=p.get('negative_prompt'),image_size=[p['height'],p['width']],infer_steps=p['steps'],guidance_scale=p['guidance'],seed=p['seed'],save_path=out,attn_mode='torch')
        if reference:append(cmd,control_image_path=[reference])
        if p.get('fp8'):append(cmd,fp8=True,fp8_scaled=True)
    else:
        cmd=append(script(root,'wan_generate_video.py'),task='i2v-A14B' if family=='wan22-i2v' else 't2v-A14B',dit=c['low'],dit_high_noise=c['high'],vae=c['vae'],t5=c['text_encoder'],
             prompt=p['prompt'],negative_prompt=p.get('negative_prompt'),video_size=[p['height'],p['width']],video_length=p['frames'],fps=p['fps'],infer_steps=p['steps'],guidance_scale=p['guidance'],guidance_scale_high_noise=p['guidance'],flow_shift=p['flow_shift'],seed=p['seed'],save_path=out,attn_mode='torch',lazy_loading=True)
        if reference:append(cmd,image_path=reference)
        if p.get('fp8'):append(cmd,fp8=True,fp8_scaled=True)
    if p.get('blocks_to_swap'):append(cmd,blocks_to_swap=p['blocks_to_swap'])
    if family.startswith('wan'):
        low=[a for a in adapters if a.get('expert') in ('both','low')];high=[a for a in adapters if a.get('expert') in ('both','high')]
        if low:append(cmd,lora_weight=[a['path'] for a in low],lora_multiplier=[a['weight'] for a in low])
        if high:append(cmd,lora_weight_high_noise=[a['path'] for a in high],lora_multiplier_high_noise=[a['weight'] for a in high])
    elif adapters:append(cmd,lora_weight=[a['path'] for a in adapters],lora_multiplier=[a['weight'] for a in adapters])
    return cmd
