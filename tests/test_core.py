"""Offline acceptance tests: only synthetic or explicitly sanitized local files."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from monik.app import create_app
from monik.cli import make_demo,write_config
from monik.collector import Collector,source_file,projected_http_limit
from monik.config import load,forbidden
from monik.model import normalize,usage,rate_windows,redact,safe,timestamp,dumps,digest,MAX_STORED_TEXT_BYTES
from monik.storage import Store
from monik.sandbox import abi

FX=Path(__file__).parent/'fixtures'
def rows(name): return [json.loads(x) for x in (FX/name).read_text().splitlines()]
def record(i,text='Русский текст',at=None,root='A'):
    return {'timestamp':at or f'2026-09-09T10:00:{(int(i)%60 if str(i).isdigit() else 0):02d}Z','type':'response_item','payload':{'type':'message','role':'assistant','id':'MESSAGE-'+str(i),'content':[{'type':'output_text','text':text}]}}
def encoded(row): return (dumps(row)+'\n').encode()

class Local(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.source=self.root/'sources';self.source.mkdir();self.data=self.root/'data';self.data.mkdir(mode=0o700)
        self.store=Store(self.data/'test.sqlite')
    def put(self,row,profile='astra',thread='A',delivery='x',at=None,ingested=2000000000):
        events=normalize(row,profile,thread,delivery)
        with self.store.connect(write=True) as db:
            for e in events:
                if at is not None:e['at']=at
                self.store.put(db,e,{'source':delivery,'delivery':delivery,'ingested_at':ingested,'observed_at':None})
        return events
    def collector(self,files,profiles=None):
        p=profiles or [{'name':'astra','root_id':'A','files':[{'path':str(f),'thread_id':'A'} for f in files]}]
        return Collector({'profiles':p,'discovery_seconds':1,'poll_seconds':0.2},self.store)
    def test_two_profile_rollouts_remain_separate(self):
        profiles=[]
        for name,tid in (('astra','ASTRA_ROOT_001'),('sol','SOL_ROOT_001')):
            path=self.source/(name+'.jsonl');shutil.copyfile(FX/(name+'_rollout.jsonl'),path)
            profiles.append({'name':name,'root_id':tid,'files':[{'path':str(path),'thread_id':tid}]})
        c=self.collector([],profiles);c.tick()
        self.assertEqual(self.store.usage({'profile':'astra'})['total_tokens'],1100)
        self.assertEqual(self.store.usage({'profile':'sol'})['total_tokens'],2200)
        (self.source/'astra.jsonl').unlink()
        with (self.source/'sol.jsonl').open('ab') as f:f.write(encoded(record(555,'SOL_STILL_LIVE')))
        c.tick();self.assertEqual(len(self.store.events({'profile':'sol','q':'SOL_STILL_LIVE'})['items']),1)
        self.assertEqual(self.store.events({'profile':'astra','q':'SOL_STILL_LIVE'})['items'],[])

    def test_oversize_gap_and_next_record_recovery(self):
        path=self.source/'large.jsonl';path.write_bytes(b'x'*(17*1024*1024)+b'\n'+encoded(record(1,'AFTER_OVERSIZE')))
        c=self.collector([path])
        for _ in range(4):c.tick()
        self.assertEqual(len(self.store.events({'kind':'oversized_record'})['items']),1)
        self.assertEqual(len(self.store.events({'q':'AFTER_OVERSIZE'})['items']),1)

    def test_usage_fixture_and_provenance(self):
        for i,row in enumerate(rows('usage_deliveries.jsonl')):self.put(row,row['profile'],row['thread_id'],str(i))
        expected=json.loads((FX/'expected_aggregates.json').read_text())['usage']
        for p,values in expected['by_profile'].items():
            actual=self.store.usage({'profile':p})
            for key,value in values.items():self.assertEqual(actual[key],value)
        actual=self.store.usage({});self.assertEqual(actual['responses'],4)
        for k,v in expected['combined'].items():self.assertEqual(actual[k],v)
        self.assertAlmostEqual(actual['cache_hit_ratio'],2600/3800)
        self.assertEqual(self.store.coverage()['additional_deliveries'],1)
    def test_invalid_usage_is_not_zero(self):
        self.put({'response_id':'bad','usage':{'input_tokens':2,'output_tokens':1,'total_tokens':99}})
        u=self.store.usage({});self.assertEqual(u['invalid'],1);self.assertIsNone(u['total_tokens'])
        self.assertIsNone(usage({})['total_tokens']);self.assertFalse(usage({'input_tokens':True})['valid'])
    def test_conflicting_usage_canonical_preserved_but_not_counted(self):
        row=rows('usage_deliveries.jsonl')[0];self.put(row,delivery='first')
        row['usage']['input_tokens']=1100;row['usage']['total_tokens']=1200;self.put(row,delivery='second')
        self.assertIsNone(self.store.usage({})['total_tokens'])
        self.assertEqual(self.store.usage({})['conflicts'],1)
        self.assertEqual(len(self.store.events({'kind':'conflict'})['items']),1)
        with self.store.connect() as db:
            canonical=db.execute('SELECT event_id,total_tokens FROM response_usage').fetchone()
            self.assertEqual(canonical['total_tokens'],1100)
            self.assertEqual(db.execute('SELECT count(*) FROM provenance WHERE event_id=?',(canonical['event_id'],)).fetchone()[0],2)
    def test_cumulative_epochs_do_not_inflate_usage(self):
        with self.store.connect(write=True) as db:
            for i,row in enumerate(rows('cumulative_snapshots.jsonl')):
                ev={'profile':'astra','thread_id':'A','kind':'cumulative','at':timestamp(row['at']),'native':str(i),'data':{'value':row['value'],'counter_epoch':row['counter_epoch']},'text':'counter'}
                self.store.put(db,ev,{'source':'counter','delivery':str(i)})
        result=self.store.cumulative({});self.assertEqual(result['summary'],json.loads((FX/'expected_aggregates.json').read_text())['cumulative']);self.assertIsNone(self.store.usage({})['total_tokens'])
    def test_rate_sequences_and_anomaly_evidence(self):
        for i,row in enumerate(rows('rate_limits_real_sanitized.jsonl')):self.put(row,profile=row['_fixture']['profile'],delivery=str(i))
        result=self.store.limits({});self.assertEqual(len(result['history']),8)
        by={x['profile']:x for x in result['latest']};self.assertEqual(by['astra']['remaining_percent'],99);self.assertEqual(by['sol']['remaining_percent'],76)
        self.assertTrue(any('non_monotonic_snapshot' in x['labels'] for x in result['history']));self.assertTrue(any('reset_time_changed' in x['labels'] for x in result['history']))
        self.assertEqual(result['cause'],'unknown');self.assertNotIn('spent_percent',result)
    def test_limits_show_only_latest_main_account_primary(self):
        old={'timestamp':100,'type':'event_msg','payload':{'type':'token_count','rate_limits':{
            'limit_id':'codex','primary':{'used_percent':8,'window_minutes':10080,'resets_at':900}}}}
        current={'timestamp':200,'type':'event_msg','payload':{'type':'token_count','rate_limits':{
            'limit_id':'codex_bengalfox',
            'primary':{'used_percent':38,'window_minutes':300,'resets_at':500},
            'secondary':{'used_percent':32,'window_minutes':10080,'resets_at':1200}}}}
        self.put(old,delivery='old');self.put(current,delivery='current')
        live=self.store.limits({})
        self.assertEqual({(x['limit_id'],x['role']) for x in live['latest']},
                         {('codex','primary')})
        self.assertEqual(len(live['history']),1)
        self.assertTrue(all(x['active_snapshot'] for x in live['latest']))
        self.assertNotIn('codex_bengalfox',json.dumps(live))
        historical=self.store.limits({'at':150})
        self.assertEqual([(x['limit_id'],x['remaining_percent']) for x in historical['latest']],
                         [('codex',92)])

    def test_http_log_projection_refreshes_primary_limit_without_retaining_headers(self):
        log=self.source/'logs.sqlite';db=sqlite3.connect(log)
        db.execute('''CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,target TEXT,
            thread_id TEXT,feedback_log_body TEXT)''')
        secret='COOKIE_MUST_NEVER_REACH_MONIK'
        def body(used):
            headers={'x-codex-plan-type':'pro','x-codex-primary-used-percent':str(used),
                'x-codex-primary-window-minutes':'10080','x-codex-primary-reset-at':'1789578423',
                'set-cookie':secret}
            return 'request: Request completed headers='+json.dumps(headers,separators=(',',':'))+' version=HTTP/2'
        db.execute('INSERT INTO logs VALUES(?,?,?,?,?)',(1,1789070000,'codex_http_client::client','A',body(43)))
        db.execute('INSERT INTO logs VALUES(?,?,?,?,?)',(2,1789070001,'unrelated','A','set-cookie='+secret))
        db.commit();db.close()
        c=self.collector([],[{'name':'astra','root_id':'A','logs_db':str(log)}]);c.tick()
        latest=self.store.limits({})['latest']
        self.assertEqual([(x['profile'],x['remaining_percent']) for x in latest],[('astra',57)])
        self.assertEqual(latest[0]['observation_basis'],'local_codex_response_headers')
        self.assertNotIn(secret,dumps(self.store.detail(latest[0]['uid'],{})))
        self.assertNotIn(secret,(self.data/'test.sqlite').read_bytes().decode('utf-8','ignore'))
        db=sqlite3.connect(log);db.execute('INSERT INTO logs VALUES(?,?,?,?,?)',
            (3,1789070065,'codex_http_client::client','A',body(44)));db.commit();db.close()
        c.tick();self.assertEqual(self.store.limits({})['latest'][0]['remaining_percent'],56)

    def test_shared_http_log_is_separated_by_proven_profile_roots(self):
        log=self.source/'shared.sqlite';db=sqlite3.connect(log)
        db.execute('''CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,target TEXT,
            thread_id TEXT,feedback_log_body TEXT)''')
        def body(used):
            return 'headers='+json.dumps({'x-codex-primary-used-percent':str(used),
                'x-codex-primary-window-minutes':'10080'},separators=(',',':'))+' version=HTTP/2'
        db.executemany('INSERT INTO logs VALUES(?,?,?,?,?)',[
            (1,1789070000,'codex_http_client::client','ASTRA',body(43)),
            (2,1789070001,'codex_http_client::client','SOL',body(35)),
            (3,1789070002,'codex_http_client::client','UNKNOWN',body(99))])
        db.commit();db.close()
        profiles=[{'name':'astra','root_id':'ASTRA','logs_db':str(log)},
                  {'name':'sol','root_id':'SOL','logs_db':str(log)}]
        c=self.collector([],profiles);c.tick()
        self.assertEqual({x['profile']:x['remaining_percent'] for x in self.store.limits({})['latest']},
                         {'astra':57,'sol':65})

    def test_http_log_retries_row_until_new_thread_owner_is_discovered(self):
        log=self.source/'late-owner.sqlite';db=sqlite3.connect(log)
        db.execute('''CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,target TEXT,
            thread_id TEXT,feedback_log_body TEXT)''')
        body='headers='+json.dumps({'x-codex-primary-used-percent':'43'},separators=(',',':'))+' version=HTTP/2'
        db.execute('INSERT INTO logs VALUES(?,?,?,?,?)',
                   (1,1789070000,'codex_http_client::client','CHILD',body));db.commit();db.close()
        c=self.collector([],[{'name':'astra','root_id':'ROOT','logs_db':str(log)}]);c.tick()
        self.assertEqual(self.store.limits({})['latest'],[])
        c.owners['CHILD']='astra';c.log_limits(next(iter(c.log_paths)),next(iter(c.log_paths.values())))
        self.assertEqual(self.store.limits({})['latest'][0]['remaining_percent'],57)

    def test_http_limit_projection_rejects_loose_or_invalid_values(self):
        self.assertIsNone(projected_http_limit({'used_percent':'43 percent'}))
        self.assertIsNone(projected_http_limit({'used_percent':'101'}))
        value=projected_http_limit({'used_percent':'43.5','window_minutes':'10080','resets_at':'1789578423'})
        self.assertEqual(value['remaining_percent'],56.5)
        self.assertEqual(value['window_minutes'],10080)
        self.assertIsNone(projected_http_limit({'used_percent':'43','plan':'secret value'})['plan'])
    def test_main_limit_snapshot_respects_knowledge_cutoff(self):
        old={'timestamp':100,'type':'event_msg','payload':{'type':'token_count','rate_limits':{
            'limit_id':'codex','primary':{'used_percent':8,'window_minutes':10080}}}}
        late={'timestamp':150,'type':'event_msg','payload':{'type':'token_count','rate_limits':{
            'limit_id':'codex','primary':{'used_percent':18,'window_minutes':10080}}}}
        with self.store.connect(write=True) as db:
            for event in normalize(old,'astra','A','old'):
                self.store.put(db,event,{'source':'old','delivery':'old','ingested_at':100})
            for event in normalize(late,'astra','A','late'):
                self.store.put(db,event,{'source':'late','delivery':'late','ingested_at':300})
        historical=self.store.limits({'view':'knowledge','at':200})
        self.assertEqual([(x['used_percent'],x['role']) for x in historical['latest']],[(8,'primary')])
        live=self.store.limits({})
        self.assertEqual([(x['used_percent'],x['role']) for x in live['latest']],[(18,'primary')])
    def test_main_limit_latest_keeps_quiet_profile_beyond_history_cap(self):
        def snapshot(timestamp,used):
            return {'timestamp':timestamp,'type':'event_msg','payload':{'type':'token_count','rate_limits':{
                'limit_id':'codex','primary':{'used_percent':used,'window_minutes':10080}}}}
        self.put(snapshot(100,8),profile='astra',delivery='astra-old')
        for index in range(2000):
            self.put(snapshot(101+index,index%100),profile='sol',delivery=f'sol-{index}')
        limits=self.store.limits({})
        self.assertEqual([x['profile'] for x in limits['latest']],['astra','sol'])
        self.assertEqual(limits['latest'][0]['labels'],['earlier_than_history_page'])
        self.assertTrue(limits['latest'][0]['active_snapshot'])
        self.assertTrue(limits['history_capped'])
    def test_legacy_rate_alias_not_double_counted(self):
        value=json.loads((FX/'app_server_rate_limits_response.json').read_text());out=rate_windows(value.get('result',value))
        self.assertEqual(len({(x['limit_id'],x['role']) for x in out}),len(out))
        self.assertFalse(rate_windows({'primary':{'used_percent':-1}})[0]['valid'])
    def test_asof_and_knowledge_no_future(self):
        self.put(record(1,'Давнее событие'),delivery='old',at=100,ingested=500)
        self.put(record(2,'Будущее событие'),delivery='new',at=200,ingested=600)
        self.assertEqual(len(self.store.events({'at':150})['items']),1)
        self.assertEqual(len(self.store.events({'at':450,'view':'knowledge'})['items']),0)
        event=self.store.events({'at':150})['items'][0]
        self.put(record(1,'Давнее событие'),delivery='late',at=100,ingested=700)
        detail=self.store.detail(event['uid'],{'view':'knowledge','at':550});self.assertEqual(detail['provenance_count'],1)
    def test_settings_model_change_and_no_hindsight_root(self):
        self.put({'type':'session_meta','payload':{'id':'A','source':'cli'}},delivery='meta',at=90)
        self.put({'type':'turn_context','payload':{'model':'old-model','effort':'ultra'}},delivery='s1',at=100)
        self.put({'type':'turn_context','payload':{'model':'new-model','service_tier':'default'}},delivery='s2',at=200)
        p=[{'name':'astra','root_id':'DIFFERENT_CURRENT_ROOT'}]
        old=self.store.overview({'at':150},p)['profiles'][0];self.assertEqual(old['root_id'],'A');self.assertEqual(old['settings']['data']['model'],'old-model')
        new=self.store.overview({'at':250},p)['profiles'][0];self.assertEqual(new['settings']['data']['model'],'new-model');self.assertEqual(new['settings']['data']['effort'],'ultra')
        self.assertIsNone(self.store.overview({'at':80},p)['profiles'][0]['root_id'])
    def test_registry_snapshot_not_backdated(self):
        c=self.collector([]);before=time.time()-1
        c.snapshot('registry','astra','A','lifecycle_thread',{'state':'CLOSED'})
        self.assertEqual(self.store.threads({'at':before})['items'],[])
        self.assertEqual(self.store.threads({})['items'][0]['state'],'CLOSED')
    def test_metadata_does_not_refresh_old_status(self):
        self.put({'type':'event_msg','payload':{'type':'task_started'}},at=100)
        c=self.collector([]);c.snapshot('state','astra','A','thread_snapshot',{'nickname':'Reader'})
        self.assertTrue(self.store.threads({})['items'][0]['stale'])
    def test_russian_code_id_and_path_search(self):
        self.put(record(1,'НАДЁЖНОСТЬ monik.parse_event("TASK_17") /workspace/src/file.py'),delivery='msg')
        for q,mode in [('Надёж','prefix'),('TASK_17','substring'),('/workspace/src/file.py','substring'),('parse_event','substring')]:
            self.assertEqual(len(self.store.events({'q':q,'search_mode':mode})['items']),1)
        self.assertEqual(self.store.events({'q':'" OR * "','search_mode':'substring'})['items'],[])
    def test_partial_then_complete_and_restart(self):
        path=self.source/'a.jsonl';a,b=encoded(record(1)),encoded(record(2));path.write_bytes(a+b[:-12])
        c=self.collector([path]);c.tick();self.assertEqual(len(self.store.events({'kind':'message:assistant'})['items']),1)
        self.assertIn('partial_line',[s['status'] for s in self.store.coverage()['sources']])
        with path.open('ab') as f:f.write(b[-12:])
        c=self.collector([path]);c.tick();c.tick();self.assertEqual(len(self.store.events({'kind':'message:assistant'})['items']),2)
        self.assertEqual(len(self.store.events({'kind':'root_binding'})['items']),1)
    def test_inode_rotation_and_overlap(self):
        path=self.source/'a.jsonl';path.write_bytes(encoded(record(1))+encoded(record(2)))
        c=self.collector([path]);c.tick();path.rename(self.source/'old.jsonl');path.write_bytes(encoded(record(2))+encoded(record(3)))
        c.discover();c.tick();self.assertEqual(len(self.store.events({'kind':'message:assistant'})['items']),3);self.assertEqual(len(c.paths),1)
        self.assertEqual(self.store.coverage()['additional_deliveries'],1)
    def test_same_inode_truncate_creates_gap(self):
        path=self.source/'a.jsonl';path.write_bytes(encoded(record(1,'X'*500))+encoded(record(2)))
        c=self.collector([path]);c.tick();path.write_bytes(encoded(record(3)));c.tick()
        self.assertEqual(len(self.store.events({'kind':'message:assistant'})['items']),3);self.assertEqual(len(self.store.events({'kind':'source_gap'})['items']),1)
    def test_shared_symlink_once(self):
        path=self.source/'a.jsonl';path.write_bytes(encoded(record(1)));link=self.source/'alias.jsonl';link.symlink_to(path)
        c=self.collector([path,link]);c.tick();self.assertEqual(c.source_reads,1);self.assertEqual(len(c.paths),1)
    def test_ambiguous_shared_file_not_attributed(self):
        path=self.source/'a.jsonl';path.write_bytes(encoded(record(1)))
        profiles=[{'name':n,'root_id':n,'files':[{'path':str(path),'thread_id':n}]*2} for n in ('astra','sol')]
        c=self.collector([],profiles);c.tick();self.assertEqual(len(c.paths),0)
        observed=self.store.events({})['items']
        self.assertEqual(len(observed),2)
        self.assertEqual({e['kind'] for e in observed},{'root_binding'})
        self.assertEqual({e['profile'] for e in observed},{'astra','sol'})
        self.assertIsNone(self.store.usage({})['total_tokens'])
    def test_bad_line_unknown_schema_continue(self):
        path=self.source/'a.jsonl';path.write_bytes(b'{bad}\n'+encoded({'type':'future_enum_99','payload':{'value':'safe'}})+encoded(record(1)))
        c=self.collector([path]);c.tick();kinds={x['kind'] for x in self.store.events({})['items']};self.assertIn('parse_error',kinds);self.assertIn('unknown:future_enum_99',kinds);self.assertIn('message:assistant',kinds)
    def test_large_output_is_bounded(self):
        path=self.source/'a.jsonl';large={'type':'response_item','payload':{'type':'function_call_output','call_id':'BIG','output':'x'*(10*1024*1024)}}
        path.write_bytes(encoded(large));c=self.collector([path]);c.tick();items=self.store.events({'kind':'tool_output'})['items'];self.assertEqual(len(items),1)
        detail=self.store.detail(items[0]['uid'],{});self.assertLess(len(dumps(detail)),200000);self.assertIn('TRUNCATED',dumps(detail))
    def test_command_execution_status_does_not_duplicate_bulk_output(self):
        marker='DUPLICATED_BULK_OUTPUT'
        row={'type':'event_msg','payload':{'type':'item_completed','item':{
            'type':'CommandExecution','id':'cmd-1','status':'completed','exit_code':0,
            'command':'printf safe','cwd':'/tmp','stdout':marker*10000,
            'stderr':marker*10000,'aggregated_output':marker*10000,
            'formatted_output':marker*10000}}}
        event=normalize(row,'astra','A','command')[0]
        self.assertEqual(event['kind'],'unknown:CommandExecution')
        self.assertEqual(event['data']['status'],'completed')
        self.assertNotIn('stdout',event['data']);self.assertNotIn('aggregated_output',event['data'])
        self.assertNotIn(marker,dumps(event));self.assertLess(len(dumps(event)),4096)
    def test_storage_compaction_preserves_evidence_and_usage(self):
        marker='OLD_VERBOSE_COMMAND_OUTPUT'
        legacy={'type':'CommandExecution','id':'legacy','status':'completed','exit_code':0,
                'command':'true','stdout':marker*10000,'stderr':'','aggregated_output':marker*10000}
        with self.store.connect(write=True) as db:
            for index in range(70):
                self.store.put(db,{'profile':'astra','thread_id':'A','kind':'unknown:CommandExecution',
                    'at':100+index,'native':'legacy-'+str(index),'data':legacy,'text':marker*10000},
                    {'source':'old','delivery':'old-'+str(index),'ingested_at':101+index})
        self.put({'type':'token_usage_record','timestamp':110,'payload':{'thread_id':'A',
            'response_id':'usage-kept','usage':{'input_tokens':9,'cached_input_tokens':4,
            'output_tokens':1,'reasoning_output_tokens':1,'total_tokens':10}}},delivery='usage',at=110)
        before=(self.store.sequence(),self.store.coverage()['additional_deliveries'],self.store.usage({})['total_tokens'])
        result=self.store.compact()
        after=(self.store.sequence(),self.store.coverage()['additional_deliveries'],self.store.usage({})['total_tokens'])
        self.assertEqual(after,before);self.assertGreaterEqual(result['updated_events'],70)
        event=self.store.events({'kind':'unknown:CommandExecution','limit':1})['items'][0]
        detail=self.store.detail(event['uid'],{})
        self.assertNotIn(marker,dumps(detail));self.assertLessEqual(len(detail['text'].encode()),MAX_STORED_TEXT_BYTES)
        self.assertEqual(self.store.events({'q':marker,'search_mode':'substring'})['items'],[])
        with self.store.connect() as db:self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
    def test_redaction_and_hidden_reasoning(self):
        clean=safe({'password':'TEST_PRIVATE_VALUE','encrypted_content':'OPAQUE_SHOULD_NOT_SURVIVE','body':'Authorization: Bearer abcdefghijklmnop sk-testexample0123456789'})
        self.assertNotIn('TEST_PRIVATE_VALUE',dumps(clean));self.assertNotIn('OPAQUE_SHOULD_NOT_SURVIVE',dumps(clean));self.assertNotIn('abcdefghijklmnop',dumps(clean))
        event=normalize({'type':'response_item','payload':{'type':'reasoning','summary':[{'text':'public summary'}],'encrypted_content':'OPAQUE_ONLY'}},'astra','A','r')[0]
        self.assertNotIn('OPAQUE_ONLY',dumps(event));self.assertIn('public summary',event['text'])
    def test_path_scope_and_exclusions(self):
        other=self.root/'outside';other.write_text('not allowed')
        with self.assertRaises(PermissionError):
            with source_file(other,[self.source]):pass
        self.assertTrue(forbidden('/workspace/Pandora box/secret'));self.assertTrue(forbidden('/home/user/.codex/auth.json'))
    def test_offline_one_source_others_continue(self):
        a,b=self.source/'a.jsonl',self.source/'b.jsonl';a.write_bytes(encoded(record(1)));b.write_bytes(encoded(record(2)))
        c=self.collector([a,b]);c.tick();a.unlink()
        with b.open('ab') as f:f.write(encoded(record(3)))
        c.tick();self.assertEqual(len(self.store.events({'kind':'message:assistant'})['items']),3)
        self.assertIn('missing',[s['status'] for s in self.store.coverage()['sources']])
    def test_atomic_cursor_on_own_storage_failure(self):
        path=self.source/'a.jsonl';path.write_bytes(encoded(record(1)));c=self.collector([path]);c.discover()
        metadata=self.store.events({})['items']
        original=self.store.put
        def fail(db,event,provenance): original(db,event,provenance);raise sqlite3.OperationalError('database or disk is full')
        with patch.object(self.store,'put',side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError):c.tick()
        self.assertEqual(self.store.events({})['items'],metadata)
        with self.store.connect() as db:
            state=json.loads(db.execute("SELECT state FROM cursors WHERE source LIKE 'tail:%'").fetchone()[0]);self.assertEqual(state['recent']['offset'],0);self.assertEqual(state['backfill']['offset'],0)
        c.tick();self.assertEqual(len(self.store.events({'kind':'message:assistant'})['items']),1)
        self.assertEqual(len(self.store.events({'kind':'root_binding'})['items']),1)
    def test_backup_and_reopen(self):
        self.put(record(1));target=self.root/'backup.sqlite';self.store.backup(target);copy=Store(target)
        self.assertEqual(copy.events({})['items'][0]['uid'],self.store.events({})['items'][0]['uid'])
        with self.assertRaises(ValueError):self.store.backup(target)
    def test_state_root_graph_and_source_unchanged(self):
        source=self.source/'state.sqlite';db=sqlite3.connect(source)
        db.executescript('CREATE TABLE threads(id TEXT,rollout_path TEXT,agent_nickname TEXT,archived INT);CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);')
        for tid in ('A','CHILD','FOREIGN'):
            path=self.source/(tid+'.jsonl');path.write_bytes(encoded(record(tid)));db.execute('INSERT INTO threads VALUES(?,?,?,0)',(tid,str(path),tid))
        db.execute('INSERT INTO thread_spawn_edges VALUES(?,?)',('A','CHILD'));db.commit();db.close();before=digest(source.read_bytes().hex())
        p={'name':'astra','root_id':'A','state_db':str(source),'rollout_dirs':[str(self.source)]};c=self.collector([], [p]);c.tick()
        self.assertEqual(set(c.owners),{'A','CHILD'});self.assertEqual(before,digest(source.read_bytes().hex()));self.assertFalse(Path(str(source)+'-wal').exists())
    def test_registry_designation_tracks_new_root(self):
        registry=self.source/'registry.json';registry.write_text(dumps({'designated_root_thread_id':'A','assignments':{}}))
        c=self.collector([],[{'name':'astra','root_id':None,'registry':str(registry)}]);c.discover();self.assertEqual(c.profiles[0]['root_id'],'A')
        registry.write_text(dumps({'designated_root_thread_id':'B','assignments':{}}));c.discover();self.assertEqual(c.profiles[0]['root_id'],'B')
    def test_event_pagination_uses_source_time_not_ingest_id(self):
        self.put(record(1),delivery='late',at=300);self.put(record(2),delivery='old',at=100);self.put(record(3),delivery='middle',at=200)
        first=self.store.events({'limit':1});second=self.store.events({'limit':1,'before':first['next_before']});third=self.store.events({'limit':1,'before':second['next_before']})
        self.assertEqual([first['items'][0]['event_at'],second['items'][0]['event_at'],third['items'][0]['event_at']],[300,200,100])
    def test_backfill_late_completion_does_not_duplicate(self):
        path=self.source/'history.jsonl';path.write_bytes(b''.join(encoded(record(i,'padding'*100)) for i in range(3000)))
        c=self.collector([path]);c.tick()
        with path.open('ab') as f:f.write(encoded(record(9999,'LIVE_MARKER')))
        c.tick();self.assertEqual(len(self.store.events({'q':'LIVE_MARKER'})['items']),1)
        for _ in range(45):c.tick()
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='message:assistant'").fetchone()[0],3001)
    def test_landlock_absence_refuses_unsafe_fallback(self):
        code="from monik.sandbox import enforce;import sys;enforce(sys.argv[1])"
        r=subprocess.run([sys.executable,'-c',code,str(self.data)],capture_output=True)
        if abi()<3:self.assertNotEqual(r.returncode,0);self.assertIn(b'no unsafe fallback',r.stderr)
        else:self.assertEqual(r.returncode,0,r.stderr)
    @unittest.skipIf(abi()<3,'Native Landlock syscalls unavailable in this container; fail-closed path is tested')
    def test_native_landlock_write_denial(self):
        original=self.source/'protected.txt';original.write_text('original')
        code="""from monik.sandbox import enforce
from pathlib import Path
import sys
root=Path(sys.argv[1]);enforce(root/'data');(root/'data'/'allowed').write_text('ok')
try: (root/'sources'/'protected.txt').write_text('forbidden')
except PermissionError: pass
else: raise AssertionError('source write unexpectedly allowed')
"""
        r=subprocess.run([sys.executable,'-c',code,str(self.root)],capture_output=True);self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(original.read_text(),'original')

    @unittest.skipIf(abi()<3,'Native Landlock syscalls unavailable in this container; fail-closed path is tested')
    def test_http_log_wal_projection_works_without_source_mutation_under_landlock(self):
        source=self.source/'logs.sqlite';db=sqlite3.connect(source)
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('''CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,target TEXT,
            thread_id TEXT,feedback_log_body TEXT)''')
        body='headers='+json.dumps({'x-codex-primary-used-percent':'43',
            'x-codex-primary-window-minutes':'10080'},separators=(',',':'))+' version=HTTP/2'
        db.execute('INSERT INTO logs VALUES(?,?,?,?,?)',(1,1789070000,'codex_http_client::client','A',body));db.commit()
        def signatures():
            return {p.name:(p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest()) for p in self.source.iterdir()}
        before=signatures()
        code="""from monik.sandbox import enforce
from monik.storage import Store
from monik.collector import Collector
from pathlib import Path
import sys
root=Path(sys.argv[1]);enforce(root/'data');store=Store(root/'data'/'landlock.sqlite')
collector=Collector({'profiles':[{'name':'astra','root_id':'A','logs_db':str(root/'sources'/'logs.sqlite')}],'poll_seconds':.5,'discovery_seconds':5},store)
collector.tick();assert store.limits({})['latest'][0]['remaining_percent']==57
"""
        r=subprocess.run([sys.executable,'-c',code,str(self.root)],capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(before,signatures());db.close()

class API(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=make_demo(Path(self.tmp.name)/'demo');self.cfg=load(self.path);self.app=create_app(self.cfg,collect=False);self.app.state.collector.tick()
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8866');self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.token=Path(self.cfg['token_file']).read_text().strip()
    def login(self):return self.client.post('/api/v1/login',json={'token':self.token},headers={'Origin':'http://127.0.0.1:8866'})
    def test_auth_csrf_and_cookie(self):
        self.assertEqual(self.client.get('/api/v1/overview').status_code,401)
        self.assertEqual(self.client.post('/api/v1/login',json={'token':self.token},headers={'Origin':'http://evil.invalid'}).status_code,403)
        r=self.login();self.assertEqual(r.status_code,200);self.assertIn('HttpOnly',r.headers['set-cookie']);self.assertIn('SameSite=strict',r.headers['set-cookie']);self.assertEqual(self.client.get('/api/v1/overview').status_code,200)
    def test_no_mutation_routes_or_paths(self):
        self.login()
        for p in ('threads/resume','threads/unsubscribe','tool/execute','config','root/delete'):
            self.assertEqual(self.client.post('/api/v1/'+p,json={}).status_code,405)
        self.assertNotEqual(self.client.get('/../../etc/passwd').status_code,200)
        self.assertEqual(self.client.get('/api/v1/events?limit=999999').status_code,400)
        self.assertEqual(self.client.get('/api/v1/events?at=2026-09-09').status_code,400)
        self.assertEqual(self.client.get('/api/v1/events?before=9223372036854775806').status_code,400)
    def test_api_contract_and_detail(self):
        self.login()
        for name in ('overview','events','search','usage','limits','cumulative','threads','assignments','coverage','health/sources','export'):
            r=self.client.get('/api/v1/'+name);self.assertEqual(r.status_code,200,(name,r.text));self.assertIn("default-src 'none'",r.headers['content-security-policy'])
        event=self.client.get('/api/v1/events').json()['items'][0]
        d=self.client.get('/api/v1/events/'+event['uid']);self.assertEqual(d.status_code,200);self.assertIn('payload_page',d.json());self.assertLess(len(d.json()['payload_page']),32769)
        self.assertEqual(self.client.get('/api/v1/events/'+event['uid']+'?at=2020-01-01T00:00:00Z').status_code,404)
    def test_logout_revokes_session(self):
        self.login();self.assertEqual(self.client.post('/api/v1/logout',headers={'Origin':'http://127.0.0.1:8866'}).status_code,200);self.assertEqual(self.client.get('/api/v1/overview').status_code,401)
    def test_rate_limit_login(self):
        for _ in range(5):self.assertEqual(self.client.post('/api/v1/login',json={'token':'wrong'},headers={'Origin':'http://127.0.0.1:8866'}).status_code,401)
        self.assertEqual(self.login().status_code,429)
    def test_lan_plaintext_config_rejected(self):
        self.cfg['bind']='192.168.1.78';write_config(self.path,self.cfg)
        with self.assertRaises(ValueError):load(self.path)
    def test_source_and_data_overlap_rejected(self):
        self.cfg['data_dir']=str(Path(self.cfg['profiles'][0]['files'][0]['path']).parent);write_config(self.path,self.cfg)
        with self.assertRaises(ValueError):load(self.path)

if __name__=='__main__':unittest.main(verbosity=2)
