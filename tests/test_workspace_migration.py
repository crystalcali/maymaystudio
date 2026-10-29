import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('apply',[False,True])
def test_migration_preserves_old_volume_and_does_not_expose_env(monkeypatch,capsys,apply):
    spec=importlib.util.spec_from_file_location('migration',Path(__file__).resolve().parents[1]/'scripts/migrate_workspace.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    calls=[]
    info={'Mounts':[{'Source':'/volume/data','Destination':'/workspace'}],
          'Config':{'Env':['TOKEN=secret-test'],'Cmd':['python','-m','agent.main']},'Image':'sha256:test'}
    def run(*args):
        calls.append(args)
        if args[:2]==('docker','inspect'):return json.dumps([info])
        if args[0]=='du':return '100 /volume/data'
        return ''
    monkeypatch.setattr(module,'run',run)
    monkeypatch.setattr(module.sp,'run',lambda args,**kw:calls.append(tuple(args)))
    monkeypatch.setattr(module.shutil,'disk_usage',lambda p:SimpleNamespace(free=100*1024**3))
    monkeypatch.setattr(Path,'is_mount',lambda p:True)
    monkeypatch.setattr(Path,'exists',lambda p:False)
    monkeypatch.setattr(Path,'mkdir',lambda p:None)
    monkeypatch.setattr('sys.argv',['migration']+(['--apply'] if apply else []))
    module.main()
    assert 'secret-test' not in capsys.readouterr().out
    assert not any('rm' in c or 'prune' in c for c in calls)
    if apply:
        assert ('cp','-a','/volume/data/.','/ephemeral/maymay-workspace') in calls
        launch=next(c for c in calls if c[:2]==('docker','run'))
        assert 'TOKEN=secret-test' in launch
        assert 'type=bind,src=/ephemeral/maymay-workspace,dst=/workspace' in launch
    else:
        assert not any(c[:2] in [('docker','stop'),('docker','run'),('docker','rename')] for c in calls)
