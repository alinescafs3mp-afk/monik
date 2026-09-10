"""Private Observatory DB. Never receives a Codex database path."""
from __future__ import annotations
import json
import fcntl
import os
import sqlite3
import stat
import time
from pathlib import Path
from contextlib import contextmanager
from .model import FIELDS,digest,dumps
from .queries import Queries

SCHEMA='''
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,uid TEXT NOT NULL UNIQUE,
 profile TEXT NOT NULL,thread_id TEXT NOT NULL,kind TEXT NOT NULL,event_at REAL,time REAL NOT NULL,
 ingested_at REAL NOT NULL,observed_at REAL,timestamp_kind TEXT NOT NULL,model TEXT,turn_id TEXT,
 call_id TEXT,response_id TEXT,text TEXT NOT NULL,search_text TEXT NOT NULL,data TEXT NOT NULL,hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_profile_time ON events(profile,time,id);
CREATE INDEX IF NOT EXISTS events_thread_kind_time ON events(profile,thread_id,kind,time,id);
CREATE INDEX IF NOT EXISTS events_kind_time ON events(kind,time,id);
CREATE INDEX IF NOT EXISTS events_ingested ON events(ingested_at,id);
CREATE INDEX IF NOT EXISTS events_conflict_uid ON events(json_extract(data,'$.canonical_uid')) WHERE kind='conflict';
CREATE TABLE IF NOT EXISTS provenance(event_id INTEGER NOT NULL REFERENCES events(id),delivery TEXT NOT NULL,
 source TEXT NOT NULL,epoch TEXT NOT NULL,offset INTEGER,raw_sha256 TEXT,observed_at REAL,ingested_at REAL NOT NULL,
 PRIMARY KEY(event_id,delivery));
CREATE TABLE IF NOT EXISTS cursors(source TEXT PRIMARY KEY,state TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sources(source TEXT PRIMARY KEY,profile TEXT,kind TEXT,status TEXT,checked REAL,
 last_event REAL,detail TEXT,size INTEGER,offset INTEGER);
CREATE TABLE IF NOT EXISTS response_usage(event_id INTEGER PRIMARY KEY REFERENCES events(id),profile TEXT,
 thread_id TEXT,response_id TEXT,input_tokens INTEGER,cached_input_tokens INTEGER,output_tokens INTEGER,
 reasoning_output_tokens INTEGER,total_tokens INTEGER,uncached_input_tokens INTEGER,valid INTEGER NOT NULL,
 UNIQUE(profile,thread_id,response_id));
CREATE TABLE IF NOT EXISTS bindings(profile TEXT NOT NULL,thread_id TEXT NOT NULL,root_id TEXT NOT NULL,
 first_seen REAL NOT NULL,PRIMARY KEY(profile,thread_id));
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text,content='events',content_rowid='id',tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
 INSERT INTO fts(rowid,text) VALUES(new.id,new.text); END;
PRAGMA user_version=1;
'''

class Store(Queries):
    def __init__(self,path: str|Path):
        self.path=Path(path).absolute()
        if any(p.is_symlink() for p in (self.path.parent,*self.path.parent.parents)):
            raise ValueError('Own database directory must not use symbolic links')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self._check_files()
        with self.connect(write=True) as db:
            if db.execute('PRAGMA user_version').fetchone()[0] not in (0,1): raise RuntimeError('Unsupported own database version')
            db.execute('PRAGMA journal_mode=WAL');db.executescript(SCHEMA)
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('Own database failed quick_check')
        os.chmod(self.path,0o600)

    def _check_files(self):
        for suffix in ('','-wal','-shm','-journal'):
            p=Path(str(self.path)+suffix)
            try: s=p.lstat()
            except FileNotFoundError: continue
            if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_uid!=os.geteuid():
                raise ValueError('Own database and sidecars must be regular, single-link files owned by this user')

    @contextmanager
    def connect(self,write=False):
        self._check_files()
        lock=None;db=None
        try:
            # Serialize every monik write connection, including startup and backup
            # schema checks, across processes. Read-only projections remain parallel.
            if write:
                lock=os.open(str(self.path)+'.write-lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
                info=os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid():
                    raise ValueError('Invalid own write-lock file')
                deadline=time.monotonic()+1
                while True:
                    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:
                        if time.monotonic()>=deadline: raise sqlite3.OperationalError('Own database writer is busy')
                        time.sleep(.02)
            db=sqlite3.connect(str(self.path) if write else self.path.resolve().as_uri()+'?mode=ro',uri=not write,timeout=1)
            db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA busy_timeout=1000')
            if not write:
                db.execute('PRAGMA query_only=ON');deadline=time.monotonic()+3
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
                db.execute('BEGIN')
            else: db.execute('PRAGMA synchronous=FULL')
            yield db
            if write: db.commit()
        except BaseException:
            if db is not None and write: db.rollback()
            raise
        finally:
            if db is not None: db.close()
            if lock is not None: os.close(lock)

    def put(self,db,event,provenance):
        now=provenance.get('ingested_at',time.time());uid=digest([1,event['profile'],event['thread_id'],event['kind'],event['native']])
        data=dumps(event['data']);hashed=digest(data);old=db.execute('SELECT id,hash FROM events WHERE uid=?',(uid,)).fetchone()
        if old and old['hash']!=hashed:
            conflict={**event,'kind':'conflict','native':uid+':'+hashed,'data':{'canonical_uid':uid,'incoming':event['data'],'policy':'first_canonical_preserved'},'text':'Source disagreement; original canonical metric retained'}
            self.put(db,conflict,{**provenance,'delivery':provenance['delivery']+':conflict'})
        if old: eid=old['id']
        else:
            at=event.get('at')
            cur=db.execute('INSERT INTO events(uid,profile,thread_id,kind,event_at,time,ingested_at,observed_at,timestamp_kind,model,turn_id,call_id,response_id,text,search_text,data,hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (uid,event['profile'],event['thread_id'],event['kind'],at,at if at is not None else now,now,provenance.get('observed_at'),event.get('timestamp_kind','source_record'),event.get('model'),event.get('turn_id'),event.get('call_id'),event.get('response_id'),event['text'],event['text'].casefold(),data,hashed))
            eid=cur.lastrowid
            if event['kind']=='usage':
                u=event['data'];db.execute('INSERT OR IGNORE INTO response_usage VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (eid,event['profile'],event['thread_id'],event.get('response_id') or event['native'],*(u.get(k) for k in FIELDS),u.get('uncached_input_tokens'),int(u.get('valid',False))))
        db.execute('INSERT OR IGNORE INTO provenance VALUES(?,?,?,?,?,?,?,?)',(eid,provenance['delivery'],provenance['source'],provenance.get('epoch','snapshot'),provenance.get('offset'),provenance.get('raw_sha256'),provenance.get('observed_at'),now))
        return eid

    def cursor(self,db,source):
        row=db.execute('SELECT state FROM cursors WHERE source=?',(source,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_cursor(self,db,source,state):
        db.execute('INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET state=excluded.state,updated=excluded.updated',(source,dumps(state),time.time()))

    def health(self,db,source,profile,kind,status,detail='',size=0,offset=0,last_event=None):
        db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET status=excluded.status,checked=excluded.checked,detail=excluded.detail,size=excluded.size,offset=excluded.offset,last_event=coalesce(excluded.last_event,sources.last_event)',(source,profile,kind,status,time.time(),last_event,detail,size,offset))

    def sequence(self):
        with self.connect() as db:
            return db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]

    def stream_batch(self,after):
        with self.connect() as db: return [x[0] for x in db.execute('SELECT id FROM events WHERE id>? ORDER BY id LIMIT 200',(after,))]

    def backup(self,target):
        target=Path(target)
        if target.exists() or target.resolve()==self.path.resolve(): raise ValueError('Backup target must be a new file')
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        with self.connect() as src:
            dst=sqlite3.connect(target)
            try:
                src.backup(dst,pages=256,sleep=0.05)
                if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise RuntimeError('Backup check failed')
            finally: dst.close()
