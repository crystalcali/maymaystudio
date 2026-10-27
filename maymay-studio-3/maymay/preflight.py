from huggingface_hub import HfApi

def inspect_components(components,token='',required=None):
    api=HfApi(token=token or None);result={};errors=[]
    for name,spec in components.items():
        if required and name not in required:continue
        if isinstance(spec,str):spec={'path':spec}
        if spec.get('path'):
            result[name]={'status':'worker_check_required','spec':spec};continue
        try:
            api.auth_check(repo_id=spec['repo'],repo_type='model')
            info=api.model_info(spec['repo'],revision=spec.get('revision','main'),files_metadata=True)
            files={s.rfilename:s for s in info.siblings}
            filename=spec.get('filename','')
            if filename and filename not in files:raise ValueError('File not found: '+filename)
            if spec.get('subfolder'):
                prefix=spec['subfolder'].strip('/')+'/'
                matched=[p for p in files if p.startswith(prefix) and p.endswith('.safetensors')]
                if not matched:raise ValueError('No encoder shards found')
            else:matched=[filename] if filename else list(files)
            size=sum((getattr(files[p],'size',None) or 0) for p in matched)
            pinned={**spec,'revision':info.sha,'bytes':size,'files':[{"name":p,"bytes":getattr(files[p],'size',None) or 0} for p in matched]}
            result[name]={'status':'accessible','bytes':size,'spec':pinned,'license':(info.card_data or {}).get('license','unknown')}
        except Exception as e:
            # No raw exception strings: HF errors can contain signed request details.
            errors.append(name+': '+('404 / file missing' if isinstance(e,ValueError) else 'access or network error; check license and HF token'))
    return {'ok':not errors,'components':result,'errors':errors,'estimated_bytes':sum(x.get('bytes',0) for x in result.values())}
