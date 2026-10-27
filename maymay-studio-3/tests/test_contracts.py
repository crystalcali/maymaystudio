import zipfile
import pytest
from maymay.recipes import BY_ID,validate_params,benchmark_signature
from maymay.datasets import safe_members
from maymay.provider import YouGPU
from agent.commands import train_commands,generate_command

def test_training_three_architectures(tmp_path):
    c={k:'/weights/'+k for k in ['raw','dit','vae','text_encoder','low','high']}
    for rid in ['krea.train','klein.train','wan.i2v.train','wan.t2v.train']:
        p=validate_params(rid,{'expert':'both'});cmds=train_commands(rid,p,c,'/musubi',tmp_path)
        assert '--save_state' in cmds[-1][1]
        assert '--max_train_steps' in cmds[-1][1]
        assert not any('--turbo_dit' in cmd for _,cmd in cmds)
        if rid.startswith('wan'):
            assert len(cmds)==4
            assert '--min_timestep' in cmds[-1][1]
        if rid=='wan.i2v.train':assert '--i2v' in cmds[0][1]

def test_krea_generation_correct_flags(tmp_path):
    p=validate_params('krea.generate',{'prompt':'x','guidance':8,'fp8':True})
    c={k:'/w/'+k for k in ['dit','vae','text_encoder']}
    cmd=generate_command('krea.generate',p,c,'/musubi',tmp_path)
    assert cmd[2]=='x'
    assert '--dtype' not in cmd
    assert '--fp8_scaled' in cmd
    assert cmd[cmd.index('--guidance_scale')+1]=='1'

def test_wan_adapter_experts(tmp_path):
    p=validate_params('wan.i2v.generate',{'prompt':'x'})
    c={k:'/w/'+k for k in ['low','high','vae','text_encoder']}
    cmd=generate_command('wan.i2v.generate',p,c,'/m',tmp_path,'/ref.png',[{'path':'/low.safetensors','weight':.8,'expert':'low'},{'path':'/high.safetensors','weight':1,'expert':'high'}])
    assert cmd[cmd.index('--lora_weight')+1]=='/low.safetensors'
    assert cmd[cmd.index('--lora_weight_high_noise')+1]=='/high.safetensors'

@pytest.mark.parametrize('rid,p',[('wan.5b.generate',{'width':832,'height':480}),('krea.train',{'gpus':2}),('wan.i2v.generate',{'frames':80}),('klein.train',{'blocks_to_swap':20})])
def test_invalid_memory_and_architecture_params(rid,p):
    with pytest.raises(ValueError):validate_params(rid,p)

def test_benchmark_settings_do_not_mix():
    a=validate_params('krea.train',{})
    assert benchmark_signature('krea.train',a,'GPU')!=benchmark_signature('krea.train',{**a,'fp8':True},'GPU')
    assert benchmark_signature('krea.train',a,'GPU')==benchmark_signature('krea.train',{**a,'steps':300},'GPU')

def test_provider_payload_contract(monkeypatch):
    calls=[]
    def request(self,method,path,payload=None):
        calls.append((method,path,payload))
        if path=='/catalog/gpus':return [{'id':'g','name':'A100','vram_gb':80}]
        if path=='/catalog/offers/g':return [{'id':'offer','in_stock':True,'gpu_count':2,'ram_gb':240,'root_disk_gb':100,'ephemeral_disk_gb':500,'price':'123.45','available_images':[]}]
        return {'id':'i'}
    monkeypatch.setattr(YouGPU,'request',request)
    p=YouGPU();offer=p.catalog()[0]
    assert offer['vram_each']==80 and offer['gpu_count']==2 and offer['hourly']==123.45
    p.create({'offer_id':'offer'});p.terminate('i')
    assert calls[-1][:2]==('POST','/instances/i/terminate')

def test_adapter_format_mismatch_is_not_silently_ignored():
    from agent.adapters import validate_keys
    assert validate_keys(['lora_unet_blocks_0_attn_wq.lora_down.weight'],{'ss_network_module':'networks.lora_krea2'},'krea2')
    with pytest.raises(ValueError):validate_keys(['lora_unet_blocks_0_self_attn_q.lora_down.weight'],{},'krea2')
    with pytest.raises(ValueError):validate_keys(['some.lora_A.weight'],{},'klein9')

def test_hf_access_check_is_required(monkeypatch):
    from maymay.preflight import inspect_components
    class Blocked:
        def __init__(self,**kwargs):pass
        def auth_check(self,**kwargs):raise RuntimeError('gated')
        def model_info(self,*a,**k):raise AssertionError('Metadata alone must not qualify access')
    monkeypatch.setattr('maymay.preflight.HfApi',Blocked)
    report=inspect_components({'dit':{'repo':'gated/model','filename':'model.safetensors'}})
    assert report['ok'] is False and report['components']=={}

def test_child_lease_expires_if_parent_disappears(tmp_path):
    from agent.lease import expired
    import time
    path=tmp_path/'lease';path.touch()
    assert not expired(path)
    assert expired(path,now=time.time()+111)
    path.unlink();assert expired(path)
