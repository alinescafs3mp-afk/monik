"""Bounded read projections. SQL is fixed; filter values are always parameters."""
from __future__ import annotations
import json
import time
from .model import FIELDS,iso
from .usage import UsageQueries

class Queries(UsageQueries):
    @staticmethod
    def filters(params,alias='e',include_kind=True):
        parts,values=['1=1'],[]
        for key in ('profile','thread_id','model')+(('kind',) if include_kind else ()):
            if params.get(key): parts.append(f'{alias}.{key}=?');values.append(params[key])
        clock='ingested_at' if params.get('view')=='knowledge' else 'time'
        for key,op in (('since','>='),('until','<='),('at','<=')):
            if params.get(key) is not None: parts.append(f'{alias}.{clock}{op}?');values.append(params[key])
        if params.get('q'):
            if params.get('search_mode')=='substring':
                parts.append(f'instr({alias}.search_text,?)>0');values.append(params['q'].casefold())
            else:
                query=' AND '.join('"'+w.replace('"','""')+'"*' for w in params['q'].split()[:16])
                parts.append(f'{alias}.id IN (SELECT rowid FROM fts WHERE fts MATCH ?)');values.append(query)
        return ' AND '.join(parts),values

    @staticmethod
    def public_event(row,detail=False):
        out=dict(row)
        if out.get('kind')=='tool_call':
            name=json.loads(out['data']).get('name')
            out['tool_name']=name[:128] if isinstance(name,str) else None
        for name in ('search_text','hash','rn'): out.pop(name,None)
        if 'data' in out:
            if detail: out['data']=json.loads(out['data'])
            else: out.pop('data')
        out['text']=out.get('text','')[:4000]
        out['event_time']=iso(out.get('event_at'));out['ingested_time']=iso(out.get('ingested_at'))
        return out

    def events(self,params):
        where,values=self.filters(params);limit=min(200,max(1,int(params.get('limit',80))))
        before,after=params.get('before'),params.get('after')
        with self.connect() as db:
            if before is not None:
                pivot=db.execute('SELECT time FROM events WHERE id=?',(before,)).fetchone()
                if not pivot: raise ValueError('Pagination cursor no longer exists; refresh the timeline')
                if pivot:
                    where+=' AND (e.time<? OR (e.time=? AND e.id<?))';values.extend((pivot[0],pivot[0],before))
            if after is not None: where+=' AND e.id>?';values.append(after)
            order='e.id ASC' if after is not None else 'e.time DESC,e.id DESC'
            rows=db.execute(f'SELECT e.* FROM events e WHERE {where} ORDER BY {order} LIMIT ?',(*values,limit+1)).fetchall()
        items=[self.public_event(r) for r in rows[:limit]]
        return {'items':items,'next_before':items[-1]['id'] if len(rows)>limit and after is None else None,'next_after':items[-1]['id'] if items and after is not None else None,'has_more':len(rows)>limit,'order':'ingestion' if after is not None else 'source_time','coverage':'partial','search_semantics':'literal substring' if params.get('search_mode')=='substring' else 'token prefix; no morphology'}

    def detail(self,uid,params):
        where,values=self.filters({k:v for k,v in params.items() if k in ('at','until','view')})
        with self.connect() as db:
            row=db.execute(f'SELECT e.* FROM events e WHERE uid=? AND {where}',(uid,*values)).fetchone()
            if not row: return None
            out=self.public_event(row,True);out['text']=row['text']
            # Match the timeline's upper bounds. The stricter bound also limits
            # knowledge-time provenance, including its count and paged details.
            cutoff=min((params[k] for k in ('at','until') if params.get(k) is not None),default=None) if params.get('view')=='knowledge' else None
            clause=' AND ingested_at<=?' if cutoff is not None else ''
            vals=(row['id'],cutoff) if cutoff is not None else (row['id'],)
            out['provenance']=[dict(x) for x in db.execute('SELECT source,epoch,offset,raw_sha256,observed_at,ingested_at FROM provenance WHERE event_id=?'+clause+' ORDER BY ingested_at LIMIT 100',vals)]
            out['provenance_count']=db.execute('SELECT count(*) FROM provenance WHERE event_id=?'+clause,vals).fetchone()[0]
            out['raw_policy']='redacted and bounded at ingestion; original payload is not retained'
        return out

    def latest(self,db,profile,kinds,params,thread=None):
        p={k:v for k,v in params.items() if k in ('at','until','view')};p['profile']=profile
        if thread: p['thread_id']=thread
        where,values=self.filters(p);marks=','.join('?' for _ in kinds)
        return db.execute(f'SELECT e.* FROM events e WHERE {where} AND kind IN ({marks}) ORDER BY time DESC,id DESC LIMIT 1',(*values,*kinds)).fetchone()

    def settings(self,db,profile,params,thread):
        if not thread: return None
        p={k:v for k,v in params.items() if k in ('at','until','view')};p.update(profile=profile,thread_id=thread,kind='settings')
        where,values=self.filters(p)
        rows=db.execute(f'SELECT e.* FROM events e WHERE {where} ORDER BY time DESC,id DESC LIMIT 100',values).fetchall()
        if not rows: return None
        out=self.public_event(rows[0],True);merged={};evidence={}
        for row in rows:
            for key,value in json.loads(row['data']).items():
                if value is not None and key not in merged:
                    merged[key]=value;evidence[key]={'uid':row['uid'],'time':row['time']}
        out.update(data=merged,field_evidence=evidence)
        return out

    def limits(self,params):
        p={k:v for k,v in params.items() if k in ('profile','at','until','view')};p['kind']='limit'
        where,values=self.filters(p)
        # ``codex`` is the account-wide limit shown by the root Codex status.
        # Named limits such as ``codex_bengalfox`` are model-specific side
        # quotas.  Preserve every source event in storage, while keeping the
        # owner-facing view focused on one comparable account limit per profile.
        account_limit=" AND json_extract(e.data,'$.limit_id')='codex' AND json_extract(e.data,'$.role')='primary'"
        with self.connect() as db:
            rows=db.execute(f'''SELECT e.* FROM events e WHERE {where}{account_limit}
                ORDER BY time DESC,id DESC LIMIT 2000''',values).fetchall()
            latest_rows=db.execute(f'''SELECT * FROM (SELECT e.*,
                    row_number() OVER(PARTITION BY profile ORDER BY time DESC,id DESC) AS rn
                FROM events e WHERE {where}{account_limit})
                WHERE rn=1 ORDER BY profile LIMIT 16''',values).fetchall()
        latest,history,previous={},{},{}
        now=min((params[k] for k in ('at','until') if params.get(k) is not None),default=time.time())
        for row in reversed(rows):
            d=json.loads(row['data']);key=(row['profile'],d.get('limit_id'),d.get('role'));labels=[];old=previous.get(key)
            if old:
                if d.get('window_minutes')!=old.get('window_minutes'): labels.append('window_changed')
                if d.get('resets_at')!=old.get('resets_at'): labels.append('reset_time_changed')
                if d.get('valid') and old.get('valid') and d['used_percent']<old['used_percent']: labels.append('non_monotonic_snapshot')
            previous[key]=d
            item={**self.public_event(row),**d,'labels':labels,'age_seconds':max(0,now-row['time']),
                  'active_snapshot':False}
            item['stale']=item['age_seconds']>120;history[row['uid']]=item
        for row in latest_rows:
            item=history.get(row['uid'])
            if item is None:
                d=json.loads(row['data']);item={**self.public_event(row),**d,
                    'labels':['earlier_than_history_page'],'age_seconds':max(0,now-row['time']),
                    'stale':now-row['time']>120,'active_snapshot':True}
            else:item['active_snapshot']=True
            latest[row['profile']]=item
        current=sorted(latest.values(),key=lambda x:x['profile'])
        return {'latest':current,'history':list(reversed(list(history.values()))),
                'history_capped':len(rows)==2000,'cause':'unknown',
                'latest_semantics':'latest observed account-wide codex primary limit per profile; model-specific quotas remain stored but are omitted',
                'account_scope':'may include activity outside observed roots'}

    def cumulative(self,params):
        p={**params,'kind':'cumulative'};where,values=self.filters(p)
        with self.connect() as db: rows=db.execute(f'SELECT e.* FROM events e WHERE {where} ORDER BY time DESC,id DESC LIMIT 5000',values).fetchall()[::-1]
        previous={};items=[];counts={'baselines':0,'repeats':0,'negative_changes':0,'known_positive_delta':0}
        for row in rows:
            d=json.loads(row['data']);value=d.get('value');key=(row['profile'],row['thread_id'],d.get('counter_epoch'))
            delta=None
            if type(value) is not int or value<0: status='invalid';previous.pop(key,None)
            elif key not in previous or previous[key] is None: status='baseline';counts['baselines']+=1;previous[key]=value
            else:
                delta=value-previous[key];previous[key]=value
                if delta<0: status='negative_change';counts['negative_changes']+=1;previous[key]=None
                elif delta==0: status='repeat';counts['repeats']+=1
                else: status='positive_delta';counts['known_positive_delta']+=delta
            items.append({**self.public_event(row),**d,'delta':delta,'status':status})
        return {'items':items[-200:],'summary':counts,'capped':len(rows)==5000,'included_in_usage':False}

    def threads(self,params):
        p={k:v for k,v in params.items() if k in ('profile','at','until','view')};where,values=self.filters(p)
        with self.connect() as db:
            rows=db.execute(f"SELECT * FROM (SELECT e.*,row_number() OVER(PARTITION BY profile,thread_id,kind ORDER BY time DESC,id DESC) AS rn FROM events e WHERE {where} AND kind IN ('thread','thread_snapshot','lifecycle_thread','task_started','task_complete','turn_aborted')) WHERE rn=1 LIMIT 6000",values).fetchall()
        nodes={}
        for r in sorted(rows,key=lambda x:(x['time'],x['id'])):
            key=(r['profile'],r['thread_id']);n=nodes.setdefault(key,{'profile':r['profile'],'thread_id':r['thread_id'],'parent_thread_id':None,'state':'unknown','identity_basis':'root_tree'})
            d=json.loads(r['data'])
            if r['kind'] in ('thread','thread_snapshot','lifecycle_thread'):
                for name in ('parent_thread_id','state','assignment_id','generation','owner_thread_id','nickname','root_id'):
                    if d.get(name) is not None: n[name]=d[name]
                if d.get('root_evidence') or (d.get('root_id')==r['thread_id']): n['parent_thread_id']=None
                if d.get('state') is not None: n['state_evidence_at']=r['time']
            else:
                n['state']={'task_started':'last_task_started','task_complete':'last_task_complete','turn_aborted':'last_turn_aborted'}[r['kind']]
                n['state_evidence_at']=r['time']
            n.update(last_evidence_at=r['time'],uid=r['uid'],source_kind=r['kind'])
        now=min((params[k] for k in ('at','until') if params.get(k) is not None),default=time.time())
        for n in nodes.values():
            n['age_seconds']=max(0,now-n.get('state_evidence_at',n['last_evidence_at']));n['stale']=n['age_seconds']>120 and n['state'] not in ('CLOSED','CANCELLED')
        return {'items':list(nodes.values()),'coverage':'partial; snapshot topology/status is known only from observation time','capped':len(rows)==6000}

    def overview(self,params,profiles):
        cards=[]
        selected=params.get('profile')
        for p in profiles:
            if selected and p['name']!=selected:
                continue
            name=p['name'];root=p.get('root_id')
            with self.connect() as db:
                # A current designation is not silently backdated into a historical view.
                if params.get('at') is not None or params.get('until') is not None:
                    binding=self.latest(db,name,['root_binding'],params)
                    root=json.loads(binding['data']).get('root_id') if binding else None
                    if root is None:
                        wh,vs=self.filters({'profile':name,**{k:v for k,v in params.items() if k in ('at','until','view')}})
                        r=db.execute(f"SELECT e.thread_id FROM events e WHERE {wh} AND kind='thread' AND json_extract(data,'$.root_evidence')=1 ORDER BY time DESC,id DESC LIMIT 1",vs).fetchone()
                        root=r[0] if r else None
                setting=self.settings(db,name,params,root)
                goal=self.latest(db,name,['goal'],params,root) if root else None
                last=self.latest(db,name,['task_started','task_complete','turn_aborted'],params,root) if root else None
            cards.append({'profile':name,'root_id':root,'settings':setting,'goal':self.public_event(goal,True) if goal else None,'last_task':self.public_event(last,True) if last else None,'usage':self.usage({**params,'profile':name}),'own_usage':self.usage({**params,'profile':name,'thread_id':root}) if root else None})
        with self.connect() as db: seq=db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
        return {'profiles':cards,'cursor':seq,'now':time.time(),'mode':'past' if params.get('at') is not None else 'live','view':params.get('view','event'),'model_calls':0,'rpc_adapter':'absent'}

    def coverage(self):
        with self.connect() as db:
            rows=[dict(x) for x in db.execute('SELECT profile,kind,count(*) AS records,min(event_at) AS earliest,max(event_at) AS latest,sum(event_at IS NULL) AS unknown_time FROM events GROUP BY profile,kind')]
            sources=[dict(x) for x in db.execute('SELECT * FROM sources ORDER BY profile,source')]
            duplicates=db.execute('SELECT count(*)-(SELECT count(*) FROM events) FROM provenance').fetchone()[0]
        for s in sources: s['check_age_seconds']=max(0,time.time()-(s['checked'] or 0))
        return {'intervals':rows,'sources':sources,'additional_deliveries':duplicates,'coverage':'partial','unsupported':['hidden reasoning','transient streaming deltas','transport retries','billing','runtime state before evidence','thread-history DB backfill','handoff file bodies','app-server RPC'],'source_write_operations':0,'health_time_basis':'current collector, not historical reconstruction'}
