"""Check source paths and CLI flag spelling without importing GPU modules."""
import ast
import sys
from pathlib import Path
from agent.commands import train_commands,generate_command
from maymay.recipes import RECIPES
root=Path(sys.argv[1]).resolve();known={'--num_processes','--num_cpu_threads_per_process'}
for path in (root/'src').rglob('*.py'):
    try:tree=ast.parse(path.read_text())
    except SyntaxError:continue
    for n in ast.walk(tree):
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='add_argument':
            known.update(a.value for a in n.args if isinstance(a,ast.Constant) and isinstance(a.value,str) and a.value.startswith('--'))
components={k:'/weight/'+k for k in ('raw','dit','vae','text_encoder','low','high')};count=0
for r in RECIPES:
    if r['kind']=='prepare' or r['family']=='wan22-5b':continue
    p={**r['defaults'],'prompt':'test','validation_prompt':'test'}
    if r['kind']=='train':cmds=train_commands(r['id'],p,components,root,Path('/tmp/job'))
    else:cmds=[('generate',generate_command(r['id'],p,components,root,Path('/tmp/output'))) ]
    for _,cmd in cmds:
        for flag in cmd:
            if str(flag).startswith('--') and flag not in known:raise RuntimeError('Unknown source flag: '+flag)
        scripts=[Path(t) for t in cmd if str(t).endswith('.py')]
        if not all(p.is_file() for p in scripts):raise RuntimeError('Missing recipe script')
        count+=1
print(f'{count} command sequences: script paths and flag names match pinned source (not a GPU runtime test).')
