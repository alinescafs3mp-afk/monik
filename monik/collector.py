"""Single bounded filesystem observer. Source handles are read-only; no RPC client exists."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from .config import expand,forbidden
from .model import digest,dumps,normalize,safe,timestamp
from .snapshot import private_snapshot, source_signature, open_regular

LOG=logging.getLogger('monik.collector')
MAX_LINE=16*1024*1024
RECENT=2*1024*1024
BATCH=256*1024
MAX_THREADS=4096


@contextmanager
def source_file(path,roots):
    raw=expand(path)
    if forbidden(raw): raise PermissionError('Excluded source')
    resolved=raw.resolve(strict=True)
    allowed=[expand(x).resolve() for x in roots]
    if forbidden(resolved) or not any(resolved==x or resolved.is_relative_to(x) for x in allowed):
        raise PermissionError('Outside source allowlist')
    fd=open_regular(resolved)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise PermissionError('Source is not a regular file')
        actual=Path(os.readlink(f'/proc/self/fd/{fd}')) if Path('/proc/self/fd').exists() else resolved
        if forbidden(actual) or not any(actual==x or actual.is_relative_to(x) for x in allowed):
            raise PermissionError('Source changed outside allowlist')
        with os.fdopen(fd,'rb',closefd=False) as stream: yield stream
    finally: os.close(fd)


def read_json(path,limit=2*1024*1024):
    with source_file(path,[path]) as f:
        raw=f.read(limit+1)
        if len(raw)>limit: raise ValueError('Snapshot exceeds limit')
        value=json.loads(raw)
        if not isinstance(value,dict): raise ValueError('Snapshot must be an object')
        return value


class Collector:
    def __init__(self,config,store):
        self.config=config;self.store=store;self.stop=threading.Event();self.worker=None
        self.profiles=[{**p, '_explicit_root':p.get('root_id')} for p in config['profiles']];self.owners={};self.paths={}
        self.last_discovery=0;self.last_success=None;self.last_error=None;self.ticks=0;self.source_reads=0;self.discovery_count=0;self.backfill_round=0;self.idle_cache={};self.state_cache={};self.cycle_snapshot_bytes=0

    def start(self):
        if self.worker and self.worker.is_alive(): raise RuntimeError('Collector already running')
        self.worker=threading.Thread(target=self.run,name='monik-collector',daemon=True);self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=10)
            if self.worker.is_alive(): raise RuntimeError('Collector has not stopped; retain service lock until process exit')

    def run(self):
        while not self.stop.is_set():
            try: self.tick();self.last_success=time.time();self.last_error=None
            except Exception as exc:
                self.last_error=type(exc).__name__;LOG.error('Observer cycle failed: %s',type(exc).__name__)
            self.stop.wait(self.config.get('poll_seconds',0.5))

    def status(self):
        return {'running':bool(self.worker and self.worker.is_alive()),'last_success':self.last_success,'last_error':self.last_error,'ticks':self.ticks,'source_reads':self.source_reads,'discovery_count':self.discovery_count,'physical_sources':len(self.paths),'model_calls':0,'rpc_calls':0}

    def snapshot(self,source,profile,thread,kind,data):
        data=safe(data);hashed=digest(data);key=source+':'+kind+':'+thread
        with self.store.connect(write=True) as db:
            state=self.store.cursor(db,key)
            if state.get('hash')==hashed: return
            revision=state.get('revision',0)+1;now=time.time()
            ev={'profile':profile,'thread_id':thread,'kind':kind,'at':None,'timestamp_kind':'observer_clock','native':key+':'+str(revision),'text':dumps(data),'data':data}
            self.store.put(db,ev,{'source':source,'delivery':key+':'+str(revision),'observed_at':now,'ingested_at':now})
            self.store.save_cursor(db,key,{'hash':hashed,'revision':revision})

    def source_health(self,source,profile,kind,status,detail=''):
        with self.store.connect(write=True) as db: self.store.health(db,source,profile,kind,status,detail)

    def registry(self,p):
        if not p.get('registry'): return
        source=p['name']+':registry'
        try:
            reg=read_json(p['registry']);root=p.get('_explicit_root') or reg.get('designated_root_thread_id')
            if root and isinstance(root,str):
                p['root_id']=root
                self.snapshot(source,p['name'],root,'root_binding',{'root_id':root,'basis':'designated_registry_or_explicit_config'})
            assignments=reg.get('assignments',{})
            if not isinstance(assignments,dict): raise ValueError('Unsupported assignments schema')
            for key,row in list(assignments.items())[:MAX_THREADS]:
                if not isinstance(row,dict): continue
                child=row.get('child_thread_id')
                if not isinstance(child,str): continue
                owner=row.get('owner_thread_id');parent=row.get('parent_thread_id') or owner
                data={k:row.get(k) for k in ('assignment_id','generation','owner_thread_id','state')}
                data.update(parent_thread_id=parent,root_id=root,cleanup_confirmed=bool(isinstance(row.get('cleanup'),dict) and row['cleanup'].get('confirmed')))
                self.snapshot(source,p['name'],child,'lifecycle_thread',data)
            self.source_health(source,p['name'],'registry','watching')
        except (OSError,ValueError,TypeError,RecursionError) as exc:
            self.source_health(source,p['name'],'registry','missing' if isinstance(exc,FileNotFoundError) else 'error',type(exc).__name__)

    def state_tree(self,p,root):
        path=expand(p['state_db']).resolve(strict=True)
        if forbidden(path): raise PermissionError('Excluded database')
        fingerprint=source_signature(path)
        cachekey=(str(path),root)
        old=self.state_cache.get(cachekey)
        if old and old[0]==fingerprint: return old[1]
        remaining=self.config.get('state_snapshot_bytes',64*1024*1024)-self.cycle_snapshot_bytes
        needed=sum(s[2] for s in fingerprint if s is not None)
        if needed>remaining: raise ValueError('Metadata snapshot cycle byte budget exceeded')
        self.cycle_snapshot_bytes+=needed
        with private_snapshot(path,self.store.path.parent,remaining) as snapshot:
            result=self._read_state_tree(snapshot,root)
        if len(self.state_cache)>=128: self.state_cache.pop(next(iter(self.state_cache)))
        self.state_cache[cachekey]=(fingerprint,result)
        return result

    def _read_state_tree(self,path,root):
        # SQLite sees only a private copy. It may create SHM or checkpoint that copy.
        db=sqlite3.connect(path,timeout=0.05)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('PRAGMA busy_timeout=50')
        deadline=time.monotonic()+0.3;db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        try:
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('Metadata snapshot failed quick_check')
            todo=[root];seen=set();edges={};rows=[]
            while todo and len(seen)<MAX_THREADS:
                batch=[x for x in todo[:128] if x not in seen];todo=todo[128:]
                if not batch: continue
                seen.update(batch);marks=','.join('?' for _ in batch)
                rows.extend(dict(x) for x in db.execute(f'SELECT id,rollout_path,agent_nickname,archived FROM threads WHERE id IN ({marks})',batch))
                for edge in db.execute(f'SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges WHERE parent_thread_id IN ({marks}) LIMIT ?',(*batch,MAX_THREADS)):
                    parent,child=edge[0],edge[1]
                    if child not in seen: todo.append(child)
                    edges.setdefault(child,parent)
            if todo: raise ValueError('Thread tree exceeds bounded limit')
            return rows,edges
        finally: db.close()

    def discover(self):
        self.discovery_count+=1;self.cycle_snapshot_bytes=0;descriptors=[];owners={}
        with self.store.connect() as db:
            historical=[dict(x) for x in db.execute('SELECT * FROM bindings LIMIT 16384')]
        for row in historical: owners.setdefault(row['thread_id'],set()).add(row['profile'])
        for p in self.profiles:
            name=p['name'];self.registry(p)
            if p.get('root_id'): self.snapshot(name+':designation',name,p['root_id'],'root_binding',{'root_id':p['root_id'],'basis':'configured_or_registry'})
            roots={r['root_id'] for r in historical if r['profile']==name}
            if p.get('root_id'): roots.add(p['root_id'])
            for root in sorted(roots):
                owners.setdefault(root,set()).add(name)
                if not p.get('state_db'): continue
                try:
                    rows,edges=self.state_tree(p,root)
                    for row in rows:
                        tid=row['id'];owners.setdefault(tid,set()).add(name)
                        with self.store.connect(write=True) as db:
                            db.execute('INSERT OR IGNORE INTO bindings VALUES(?,?,?,?)',(name,tid,root,time.time()))
                        self.snapshot(name+':state',name,tid,'thread_snapshot',{'root_id':root,'parent_thread_id':edges.get(tid),'nickname':row.get('agent_nickname'),'archived':row.get('archived'),'status_semantics':'metadata_not_process_liveness'})
                        if row.get('rollout_path'): descriptors.append({'path':row['rollout_path'],'profile':name,'thread':tid,'kind':'rollout','roots':p.get('rollout_dirs',[])})
                    self.source_health(name+':state',name,'state','watching','root_tree_only')
                except (OSError,sqlite3.Error,ValueError,TypeError) as exc:
                    self.source_health(name+':state',name,'state','missing' if isinstance(exc,FileNotFoundError) else 'error',type(exc).__name__)
            if not roots: self.source_health(name+':identity',name,'identity','unknown','No designated root. Set root_id explicitly or provide lifecycle registry.')
            else: self.source_health(name+':identity',name,'identity','mapped')
            for key,kind in (('lifecycle_events','lifecycle'),('peer_events','peer')):
                if p.get(key): descriptors.append({'path':p[key],'profile':name,'thread':p.get('root_id') or 'unknown','kind':kind,'roots':[p[key]]})
            for row in p.get('files',[]):
                tid=row['thread_id'];owners.setdefault(tid,set()).add(name)
                descriptors.append({'path':row['path'],'profile':name,'thread':tid,'kind':row.get('kind','rollout'),'roots':[row['path']]})
        self.owners={tid:next(iter(names)) for tid,names in owners.items() if len(names)==1}
        ambiguous={tid for tid,names in owners.items() if len(names)>1}
        for tid in ambiguous: self.source_health('identity:'+digest(tid)[:12],'unknown','identity','conflict','Thread belongs to multiple configured root trees; collection paused for it')
        seen={}
        for d in descriptors:
            if d['thread'] in ambiguous: continue
            try:
                with source_file(d['path'],d['roots']) as f:
                    s=os.fstat(f.fileno());key=f'{s.st_dev}:{s.st_ino}'
                if key in seen and (seen[key] is None or seen[key]['profile']!=d['profile']):
                    self.source_health('inode:'+key,'unknown','identity','conflict','Same physical source has conflicting owners');seen[key]=None;continue
                if key not in seen: seen[key]=d
            except (OSError,ValueError) as exc:
                self.source_health(d['profile']+':'+d['kind']+':'+digest(d['path'])[:10],d['profile'],d['kind'],'missing' if isinstance(exc,FileNotFoundError) else 'error',type(exc).__name__)
        # Keep known paths while a metadata adapter is temporarily unavailable.
        for key,d in seen.items():
            if d is not None:
                for old,previous in list(self.paths.items()):
                    if old!=key and expand(previous['path']).resolve()==expand(d['path']).resolve(): self.paths.pop(old,None)
                self.paths[key]=d
            else: self.paths.pop(key,None)
        for key,d in list(self.paths.items()):
            if d['thread'] in ambiguous: self.paths.pop(key,None)
        self.last_discovery=time.monotonic()

    def emit_error(self,db,d,key,epoch,offset,kind,detail):
        ev={'profile':d['profile'],'thread_id':d['thread'],'kind':kind,'at':None,'timestamp_kind':'observer_clock','native':key+':'+epoch+':'+str(offset)+':'+kind,'text':detail,'data':{'detail':detail,'source':key,'offset':offset}}
        self.store.put(db,ev,{'source':key,'epoch':epoch,'offset':offset,'delivery':ev['native'],'observed_at':time.time()})

    def process(self,db,d,key,epoch,offset,raw,lane,initial):
        delivery=f'{key}:{epoch}:{offset}';observed=time.time() if offset>=initial else None
        provenance={'source':key,'epoch':epoch,'offset':offset,'delivery':delivery,'raw_sha256':hashlib.sha256(raw).hexdigest(),'observed_at':observed}
        try:
            row=json.loads(raw)
            if not isinstance(row,dict): raise ValueError('Record must be object')
            self.source_reads+=1
            if d['kind']=='peer':
                data=safe({k:row.get(k) for k in ('event_id','file','kind','observed_at','seq','sha256','size')})
                events=[{'profile':d['profile'],'thread_id':d['thread'],'kind':'peer_event','at':timestamp(row.get('observed_at')),'native':row.get('event_id') or delivery,'data':data,'text':dumps(data)}]
            else:
                if row.get('type')=='session_meta' and isinstance(row.get('payload'),dict):
                    candidate=row['payload'].get('id',row['payload'].get('session_id'))
                    if isinstance(candidate,str) and self.owners.get(candidate)==d['profile']: lane['thread']=candidate
                events=normalize(row,d['profile'],lane.get('thread',d['thread']),delivery,lane.get('model'))
            for event in events:
                tid=event['thread_id'];owner=self.owners.get(tid)
                if d['kind']=='rollout' and owner!=d['profile']:
                    self.emit_error(db,d,key,epoch,offset,'identity_unknown','Unattributed origin thread; record excluded from profile metrics');continue
                if event['kind']=='settings' and event['data'].get('model'): lane['model']=event['data']['model']
                if event['thread_id']!=lane.get('thread',d['thread']): event['model']=None
                if event['kind']=='cumulative': event['data']['counter_epoch']=key+':'+epoch+':'+str(event['data'].get('counter_epoch','thread'))
                self.store.put(db,event,provenance)
                if event['kind']=='lifecycle' and event['data'].get('kind')=='CLEANUP_CONFIRMED':
                    for child in event['data'].get('tree',[])[:MAX_THREADS]:
                        if isinstance(child,str):
                            status={**event,'thread_id':child,'kind':'lifecycle_thread','native':event['native']+':'+child,'data':{'state':event['data'].get('final_state'),'assignment_id':event['data'].get('assignment'),'cleanup_confirmed':True}}
                            self.store.put(db,status,provenance)
            return max((e['at'] for e in events if e.get('at') is not None),default=None)
        except (ValueError,TypeError,KeyError,RecursionError,OverflowError) as exc:
            self.emit_error(db,d,key,epoch,offset,'parse_error',type(exc).__name__+'; complete source line skipped, contents not logged')
            return None

    @staticmethod
    def anchor(f,offset):
        f.seek(max(0,offset-64));return hashlib.sha256(f.read(min(offset,64))).hexdigest()

    def tail(self,key,d,which='live'):
        used=0
        try:
            with source_file(d['path'],d['roots']) as f:
                st=os.fstat(f.fileno());actual=f'{st.st_dev}:{st.st_ino}'
                signature=(st.st_size,st.st_mtime_ns,st.st_ctime_ns)
                cached=self.idle_cache.get((key,which))
                if cached and cached[0]==signature and time.monotonic()-cached[1]<5: return 0
                if actual!=key:
                    self.last_discovery=0
                    self.source_health(key,d['profile'],d['kind'],'replaced','Awaiting inode reconciliation');return 0
                with self.store.connect(write=True) as db:
                    state=self.store.cursor(db,'tail:'+key);generation=state.get('generation',0)
                    reset=False
                    if state:
                        live=state['live'];off=live['offset']
                        reset='recent' not in state or st.st_size<off or (off and live.get('anchor')!=self.anchor(f,off))
                        if not reset and state.get('head_size'):
                            f.seek(0);reset=hashlib.sha256(f.read(state['head_size'])).hexdigest()!=state['head_hash']
                    if not state or reset:
                        if reset:
                            generation+=1;self.emit_error(db,d,key,str(generation),0,'source_gap','Truncate or in-place rewrite detected; replay starts a new source epoch')
                        start=max(0,st.st_size-RECENT)
                        if start:
                            f.seek(start);f.readline(MAX_LINE+1);start=f.tell()
                        # New appends bypass all pre-existing history, including the recent tail.
                        f.seek(max(0,st.st_size-MAX_LINE-1));tail_start=f.tell();tail_bytes=f.read(MAX_LINE+1)
                        newline=tail_bytes.rfind(b'\n');live_start=tail_start+newline+1 if newline>=0 else 0
                        start=min(start,live_start)
                        f.seek(0);head=f.read(min(64,st.st_size))
                        state={'generation':generation,'initial':st.st_size,'boundary':start,'recent_boundary':live_start,'head_size':len(head),'head_hash':hashlib.sha256(head).hexdigest(),
                            'live':{'offset':live_start,'thread':d['thread']},'recent':{'offset':start,'thread':d['thread']},'backfill':{'offset':0,'thread':d['thread']}}
                        state['live']['anchor']=self.anchor(f,live_start)
                    lane=state[which];end=st.st_size if which=='live' else state['recent_boundary'] if which=='recent' else state['boundary'];last=None;status='watching';epoch=str(generation)
                    for _ in range(200 if which=='live' else 80):
                        offset=lane['offset']
                        if offset>=end or used>=BATCH: break
                        f.seek(offset);raw=f.readline(min(MAX_LINE+1,end-offset));used+=len(raw)
                        if not raw: break
                        if lane.get('discarding'):
                            lane['offset']=f.tell()
                            if raw.endswith(b'\n'): lane.pop('discarding',None)
                            continue
                        if len(raw)>MAX_LINE:
                            self.emit_error(db,d,key,epoch,offset,'oversized_record','Line exceeds 16 MiB; discarded in bounded chunks; gap is explicit')
                            lane['discarding']=not raw.endswith(b'\n');lane['offset']=f.tell();status='gap';continue
                        if not raw.endswith(b'\n'):
                            status='partial_line';break
                        last=self.process(db,d,key,epoch,offset,raw,lane,0 if which=='live' else state['initial']) or last
                        lane['offset']=f.tell()
                    if which=='recent' and lane['offset']>=end and lane.get('model') and not state['live'].get('model'):
                        state['live']['model']=lane['model']
                    lane['anchor']=self.anchor(f,lane['offset']);lane['status']=status
                    self.store.save_cursor(db,'tail:'+key,state)
                    backlog=max(0,state['boundary']-state['backfill']['offset'])+max(0,state['recent_boundary']-state['recent']['offset'])+max(0,st.st_size-state['live']['offset'])
                    visible_status=state['live'].get('status','watching')
                    if visible_status=='watching' and backlog: visible_status='catching_up'
                    self.store.health(db,key,d['profile'],d['kind'],visible_status,'backfill_remaining_bytes='+str(backlog),st.st_size,state['live']['offset'],last)
                if lane['offset']>=end or status=='partial_line':
                    self.idle_cache[(key,which)]=(signature,time.monotonic())
        except (OSError,ValueError) as exc:
            self.source_health(key,d['profile'],d['kind'],'missing' if isinstance(exc,FileNotFoundError) else 'error',type(exc).__name__)
        return used

    def tick(self):
        self.ticks+=1
        if time.monotonic()-self.last_discovery>=self.config.get('discovery_seconds',5): self.discover()
        entries=list(self.paths.items());used=0
        # Every source gets its live lane before any historical backfill work.
        for key,d in entries:
            if self.stop.is_set(): return
            used+=self.tail(key,d,'live')
        if entries:
            for step in range(min(len(entries),8)):
                key,d=entries[(self.backfill_round+step)%len(entries)]
                if used>=16*1024*1024: break
                used+=self.tail(key,d,'recent')
                if used<16*1024*1024: used+=self.tail(key,d,'backfill')
            self.backfill_round=(self.backfill_round+8)%len(entries)
