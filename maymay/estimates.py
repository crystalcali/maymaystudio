from statistics import median
from sqlalchemy import select
from .db import Benchmark
from .recipes import benchmark_signature, BY_ID

def estimate(db,recipe_id,params,offer):
    sig=benchmark_signature(recipe_id,params,offer['gpu'])
    rows=db.scalars(select(Benchmark).where(Benchmark.signature==sig).order_by(Benchmark.created.desc()).limit(10)).all()
    if not rows:return {'seconds':None,'cost':None,'confidence':'not_measured','label':'Скорость пока не измерена'}
    unit=median(r.seconds/r.samples for r in rows if r.samples)
    units=params.get('steps',1200) if BY_ID[recipe_id]['kind']=='train' else params.get('count',1)
    seconds=unit*units+median(r.setup_seconds for r in rows)
    return {'seconds':round(seconds),'range_seconds':[round(seconds*.8),round(seconds*1.4)],'cost':round(seconds/3600*offer['hourly'],2),
        'confidence':'measured','samples':sum(r.samples for r in rows),'label':'На основе замеров; диск и простой оплачиваются отдельно'}
