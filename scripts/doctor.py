"""Run on rented GPU before a long task: python scripts/doctor.py."""
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
report={'python':sys.version,'packages':{},'gpu':None}
for package in ['torch','torchvision','transformers','diffusers','accelerate','safetensors','bitsandbytes','musubi_tuner']:
    try:m=importlib.import_module(package);report['packages'][package]={'version':getattr(m,'__version__','installed'),'path':getattr(m,'__file__','')}
    except Exception as e:report['packages'][package]={'error':str(e)}
try:
    import torch
    from torchvision.ops import nms
    from transformers import Qwen2_5_VLForConditionalGeneration
    report['gpu']={'available':torch.cuda.is_available(),'bf16':torch.cuda.is_bf16_supported() if torch.cuda.is_available() else False,'count':torch.cuda.device_count()}
except Exception as e:report['import_check']=str(e)
report['ffmpeg']=bool(shutil.which('ffmpeg'))
report['disk_free_gb']=round(shutil.disk_usage('/workspace' if Path('/workspace').exists() else '.').free/1024**3,1)
print(json.dumps(report,indent=2))
if any('error' in p for p in report['packages'].values()) or not report.get('gpu',{}).get('available'):sys.exit(1)
