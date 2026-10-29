"""Run on the GPU host, never in the web container. Dry-run unless --apply."""
import argparse
import json
import shutil
import subprocess as sp
from pathlib import Path


def run(*args):
    return sp.check_output(args,text=True).strip()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    name='maymay-agent';old=name+'-before-diskmove'
    info=json.loads(run('docker','inspect',name))[0]
    assert old not in run('docker','ps','-a','--format','{{.Names}}').splitlines(), 'Backup container already exists; inspect it manually'
    mounts=info['Mounts']
    assert len(mounts)==1 and mounts[0]['Destination']=='/workspace', 'Custom mounts require manual migration'
    assert Path('/ephemeral').is_mount(), '/ephemeral must be a mounted disk'
    target=Path('/ephemeral/maymay-workspace');patch=Path('/ephemeral/maymay-agent-main.py')
    assert not target.exists() and not patch.exists(), 'Destination already exists; no data overwritten'
    source=mounts[0]['Source']
    size=int(run('du','-sb',source).split()[0])
    assert shutil.disk_usage('/ephemeral').free>size+30*1024**3, 'Not enough space for copy and 30 GiB reserve'
    print('Copy workspace to',target,'; preserve old container and volume. Active tasks must be cancelled/completed first.')
    if not args.apply:
        print('Dry run only. Use sudo python3 scripts/migrate_workspace.py --apply after tasks have stopped.')
        return
    run('docker','stop',name)
    target.mkdir()
    sp.run(['cp','-a',source+'/.',str(target)],check=True)
    run('docker','cp',name+':/opt/studio/agent/main.py',str(patch))
    run('docker','rename',name,old)
    cmd=['docker','run','-d','--init','--gpus','device=0','--restart','unless-stopped','--name',name,
         '--mount',f'type=bind,src={target},dst=/workspace',
         '--mount',f'type=bind,src={patch},dst=/opt/studio/agent/main.py,readonly']
    for env in info['Config'].get('Env',[]):cmd+=['-e',env]
    cmd += [info['Image']]+(info['Config'].get('Cmd') or [])
    sp.run(cmd,check=True)
    print('Copied. Verify docker logs and df -h /workspace. Old data was not deleted.')


if __name__=='__main__':
    try:main()
    except sp.CalledProcessError:
        raise SystemExit('Docker/copy command failed. Old data retained; inspect container names and mounts before retrying.') from None
