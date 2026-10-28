import time
import uuid
from sqlalchemy import create_engine, String, Text, Float, JSON, Boolean, Integer, ForeignKey, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid(): return uuid.uuid4().hex
class Base(DeclarativeBase): pass
class Record(Base):
    __abstract__ = True
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    created: Mapped[float] = mapped_column(Float, default=time.time)
class Character(Record):
    __tablename__='characters'
    name: Mapped[str] = mapped_column(String(160))
    trigger: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text,default='')
    face_id: Mapped[str|None] = mapped_column(String(64),nullable=True)
    body_id: Mapped[str|None] = mapped_column(String(64),nullable=True)
class Dataset(Record):
    __tablename__='datasets'
    character_id: Mapped[str] = mapped_column(ForeignKey('characters.id'))
    name: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer,default=1)
class Item(Record):
    __tablename__='items'
    dataset_id: Mapped[str] = mapped_column(ForeignKey('datasets.id'))
    name: Mapped[str] = mapped_column(String(240))
    key: Mapped[str] = mapped_column(Text)
    sha: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    caption: Mapped[str] = mapped_column(Text,default='')
    accepted: Mapped[bool] = mapped_column(Boolean,default=True)
    group_id: Mapped[str] = mapped_column(String(120))
    quality: Mapped[dict] = mapped_column(JSON,default=dict)
class Profile(Record):
    __tablename__='profiles'
    name: Mapped[str] = mapped_column(String(160))
    family: Mapped[str] = mapped_column(String(40))
    components: Mapped[dict] = mapped_column(JSON,default=dict)
class Adapter(Record):
    __tablename__='adapters'
    name: Mapped[str] = mapped_column(String(200))
    family: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String(200),default='')
    expert: Mapped[str] = mapped_column(String(20),default='both')
    character_id: Mapped[str|None] = mapped_column(String(64),nullable=True)
class Worker(Record):
    __tablename__='workers'
    name: Mapped[str] = mapped_column(String(160))
    token_hash: Mapped[str] = mapped_column(String(64),unique=True)
    capabilities: Mapped[dict] = mapped_column(JSON,default=dict)
    heartbeat: Mapped[float] = mapped_column(Float,default=0)
    draining: Mapped[bool] = mapped_column(Boolean,default=False)
    node_id: Mapped[str|None] = mapped_column(String(64),nullable=True)
class Pairing(Record):
    __tablename__='pairings'
    token_hash: Mapped[str] = mapped_column(String(64),unique=True)
    expires: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean,default=False)
    node_id: Mapped[str|None] = mapped_column(String(64),nullable=True)
class Node(Record):
    __tablename__='nodes'
    provider_id: Mapped[str|None] = mapped_column(String(100),nullable=True)
    request_id: Mapped[str] = mapped_column(String(64),unique=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(40),default='creating')
    hourly: Mapped[float] = mapped_column(Float,default=0)
    offer: Mapped[dict] = mapped_column(JSON,default=dict)
    auto_terminate: Mapped[bool] = mapped_column(Boolean,default=False)
    idle_minutes: Mapped[int] = mapped_column(Integer,default=15)
    budget: Mapped[float|None] = mapped_column(Float,nullable=True)
    connection: Mapped[dict] = mapped_column(JSON,default=dict)
    terminated: Mapped[float|None] = mapped_column(Float,nullable=True)
class Job(Record):
    __tablename__='jobs'
    recipe: Mapped[str] = mapped_column(String(80))
    character_id: Mapped[str|None] = mapped_column(String(64),nullable=True)
    params: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40),default='queued',index=True)
    stage: Mapped[str] = mapped_column(String(120),default='В очереди')
    worker_id: Mapped[str|None] = mapped_column(String(64),nullable=True)
    preferred_worker: Mapped[str|None] = mapped_column(String(64),nullable=True)
    node_id: Mapped[str|None] = mapped_column(String(64),nullable=True)
    attempt: Mapped[str|None] = mapped_column(String(64),nullable=True)
    lease: Mapped[float] = mapped_column(Float,default=0)
    progress: Mapped[float] = mapped_column(Float,default=0)
    eta: Mapped[float|None] = mapped_column(Float,nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON,default=dict)
    error: Mapped[str] = mapped_column(Text,default='')
    started: Mapped[float|None] = mapped_column(Float,nullable=True)
    finished: Mapped[float|None] = mapped_column(Float,nullable=True)
class Log(Record):
    __tablename__='logs'
    job_id: Mapped[str] = mapped_column(ForeignKey('jobs.id'),index=True)
    text: Mapped[str] = mapped_column(Text)
class Artifact(Record):
    __tablename__='artifacts'
    job_id: Mapped[str|None] = mapped_column(String(64),nullable=True,index=True)
    kind: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(Text,unique=True)
    name: Mapped[str] = mapped_column(String(240))
    sha: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    meta: Mapped[dict] = mapped_column(JSON,default=dict)
class KV(Base):
    __tablename__='settings'
    key: Mapped[str] = mapped_column(String(100),primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
class Benchmark(Record):
    __tablename__='benchmarks'
    signature: Mapped[str] = mapped_column(String(64),index=True)
    recipe: Mapped[str] = mapped_column(String(80))
    gpu: Mapped[str] = mapped_column(String(200))
    seconds: Mapped[float] = mapped_column(Float)
    samples: Mapped[int] = mapped_column(Integer)
    setup_seconds: Mapped[float] = mapped_column(Float,default=0)

def connect(url):
    engine=create_engine(url,connect_args={'check_same_thread':False} if url.startswith('sqlite') else {},pool_pre_ping=True)
    if url.startswith('sqlite'):
        @event.listens_for(engine,'connect')
        def pragmas(conn, _):
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=10000')
    Base.metadata.create_all(engine)
    return engine,sessionmaker(engine,expire_on_commit=False)

def asdict(row):
    return {c.name:getattr(row,c.name) for c in row.__table__.columns}
