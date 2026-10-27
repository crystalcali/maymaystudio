"""Local convenience launcher; production uses Docker entrypoint."""
import os
import argparse
import sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
venv=root/'.venv/bin/python'
if venv.exists() and Path(sys.prefix)!=root/'.venv' and not os.getenv('MAYMAY_DEV_REEXEC'):
    os.environ['MAYMAY_DEV_REEXEC']='1';os.execv(str(venv),[str(venv),__file__,*sys.argv[1:]])
os.chdir(root)
preview=root/'.preview-public-url'
if preview.exists():os.environ['PUBLIC_URL']=preview.read_text().strip()
parser=argparse.ArgumentParser();parser.add_argument('--port',default=os.getenv('PORT','3000'));args,_=parser.parse_known_args()
os.execv(sys.executable,[sys.executable,'-m','uvicorn','maymay.api:create_app','--factory','--host','0.0.0.0','--port',str(args.port)])
