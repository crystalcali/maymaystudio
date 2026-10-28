"""Reject unsupported formats instead of silently ignoring LoRA tensors."""
import re

def validate_keys(keys,metadata,family):
    expected='networks.lora_krea2' if family=='krea2' else 'networks.lora_flux_2' if family=='klein9' else 'networks.lora_wan'
    module=(metadata or {}).get('ss_network_module','')
    if module and module!=expected:raise ValueError('LoRA trainer architecture mismatch: '+module)
    down=[k for k in keys if k.endswith('.lora_down.weight')]
    if not down:raise ValueError('This recipe requires Musubi-compatible LoRA keys; convert external PEFT/other formats explicitly')
    if family=='krea2':pattern=r'lora_unet_(blocks_\d+_attn_w[qkvo]|first|txtfusion|tmlp|txtmlp|tproj)'
    elif family=='klein9':pattern=r'lora_unet_(double_blocks|single_blocks)'
    else:pattern=r'lora_unet_blocks_\d+_(self_attn|cross_attn|ffn)'
    if not any(re.match(pattern,k) for k in down):raise ValueError('No recognized target layers for selected model; unsupported or incompatible LoRA')
    return True
