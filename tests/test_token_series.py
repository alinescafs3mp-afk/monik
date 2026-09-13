"""Synthetic read-only graph acceptance. No owner sources or model calls."""
from __future__ import annotations
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from monik.app import create_app
from monik.cli import make_demo
from monik.config import load
from monik.model import normalize, iso
from monik.series import usage_series, WINDOWS
from monik.storage import Store

NOW = 1_800_000_000
PROFILES = [{'name': 'astra', 'label': 'Astra'}, {'name': 'sol', 'label': 'Sol'}]


class Series(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'own.sqlite')

    def put(self, response='r1', profile='astra', at=NOW-100, ingested=NOW-90,
            amount=100, delivery=None, thread='root', **overrides):
        usage = dict(input_tokens=amount, cached_input_tokens=amount//2,
                     output_tokens=10, reasoning_output_tokens=2, total_tokens=amount+10)
        usage.update(overrides)
        row = {'type': 'token_usage_record', 'timestamp': at, 'payload': {
            'thread_id': thread, 'response_id': response, 'usage': usage}}
        delivery = delivery or 'test:' + str(response)
        with self.store.connect(write=True) as db:
            for event in normalize(row, profile, thread, delivery):
                self.store.put(db, event, {'source': 'synthetic', 'delivery': delivery, 'ingested_at': ingested})

    def put_limit(self, used, profile='astra', at=NOW-100, ingested=NOW-90,
                  delivery=None, thread='root', limit_id='codex', role='primary',
                  resets_at=NOW+86400):
        delivery = delivery or f'limit:{profile}:{at}:{used}'
        row = {'type': 'event_msg', 'timestamp': at, 'payload': {
            'type': 'token_count', 'thread_id': thread, 'rate_limits': {'rateLimits': {
                'limitId': limit_id, role: {'usedPercent': used,
                    'windowDurationMins': 10080, 'resetsAt': resets_at}}}}}
        with self.store.connect(write=True) as db:
            for event in normalize(row, profile, thread, delivery):
                self.store.put(db, event, {'source': 'synthetic-limit',
                    'delivery': delivery, 'ingested_at': ingested})

    def series(self, **kwargs):
        return usage_series(self.store, copy.deepcopy(PROFILES), now=NOW, **kwargs)

    def test_all_windows_are_bounded_and_exact(self):
        for hours in WINDOWS:
            data = self.series(hours=hours)
            self.assertEqual(data['window_end']-data['window_start'], hours*3600)
            self.assertEqual([s['profile'] for s in data['series']], ['astra', 'sol'])
            for s in data['series']:
                self.assertLessEqual(len(s['points']), 98)
                self.assertEqual(s['points'][0]['start'], data['window_start'])
                self.assertEqual(s['points'][-1]['end'], data['window_end'])
                self.assertIsNone(s['totals']['total_tokens'])
                self.assertTrue(all(p['values']['total_tokens'] is None for p in s['points']))

    def test_unique_responses_not_deliveries_and_not_cumulative(self):
        self.put(delivery='first'); self.put(delivery='second')
        self.put(response='r2', profile='sol', amount=200)
        data = self.series()
        self.assertEqual(data['series'][0]['totals']['total_tokens'], 110)
        self.assertEqual(data['series'][0]['responses'], 1)
        self.assertEqual(data['series'][1]['totals']['total_tokens'], 210)

    def test_subsets_and_optional_metrics(self):
        self.put()
        a = self.series()['series'][0]
        self.assertEqual(a['totals'], {'input_tokens': 100, 'cached_input_tokens': 50,
            'output_tokens': 10, 'reasoning_output_tokens': 2, 'total_tokens': 110, 'uncached_input_tokens': 50})
        self.put(response='r2', cached_input_tokens=None)
        a = self.series()['series'][0]
        self.assertEqual(a['totals']['total_tokens'], 220)
        self.assertEqual(a['measured']['cached_input_tokens'], 1)
        self.assertEqual(a['measured']['total_tokens'], 2)

    def test_real_zero_is_not_empty(self):
        self.put(amount=0, output_tokens=0, reasoning_output_tokens=0, total_tokens=0)
        a = self.series()['series'][0]
        self.assertEqual(a['totals']['total_tokens'], 0)
        self.assertEqual(a['accepted'], 1)
        point = next(p for p in a['points'] if p['responses'])
        self.assertEqual(point['state'], 'measured'); self.assertEqual(point['values']['total_tokens'], 0)

    def test_time_window_is_half_open(self):
        self.put(response='start', at=NOW-3600, ingested=NOW-3600)
        self.put(response='before', at=NOW-3600-.1, ingested=NOW-3600)
        self.put(response='end', at=NOW, ingested=NOW)
        self.put(response='inside', at=NOW-.1, ingested=NOW-.1)
        self.assertEqual(self.series(hours=1)['series'][0]['accepted'], 2)

    def test_invalid_and_conflicting_measurements_excluded(self):
        self.put(delivery='canonical')
        self.put(delivery='disagreement', amount=200, ingested=NOW-20)
        self.put(response='invalid', total_tokens=999)
        a = self.series()['series'][0]
        self.assertIsNone(a['totals']['total_tokens']); self.assertEqual(a['accepted'], 0)
        self.assertEqual(a['conflicts'], 1); self.assertEqual(a['invalid'], 1)
        self.assertEqual(a['responses'], 2)

    def test_conflict_does_not_poison_earlier_snapshot(self):
        self.put(at=NOW-1000, ingested=NOW-900)
        self.put(amount=200, delivery='late', at=NOW-1000, ingested=NOW-20)
        self.assertEqual(self.series(at=NOW-30)['series'][0]['totals']['total_tokens'], 110)
        self.assertIsNone(self.series()['series'][0]['totals']['total_tokens'])

    def test_backfill_visible_only_after_observation(self):
        self.put(at=NOW-1000, ingested=NOW-20)
        self.assertIsNone(self.series(at=NOW-30)['series'][0]['totals']['total_tokens'])
        self.assertEqual(self.series()['series'][0]['totals']['total_tokens'], 110)

    def test_missing_source_time_not_plotted_using_arrival_time(self):
        self.put(at=None)
        a = self.series()['series'][0]
        self.assertEqual(a['unknown_source_time'], 1)
        self.assertIsNone(a['totals']['total_tokens'])
        self.assertTrue(all(p['state']=='no_records' for p in a['points']))

    def test_one_profile_and_children(self):
        self.put(thread='root'); self.put(response='child', thread='child')
        self.put(response='sol', profile='sol')
        data = self.series(profile='astra')
        self.assertEqual(len(data['series']), 1)
        self.assertEqual(data['series'][0]['totals']['total_tokens'], 220)
        self.assertEqual(data['cursor'], self.store.sequence())

    def test_bad_parameters_rejected(self):
        for args in ({'hours':0}, {'hours':5}, {'hours':True}, {'hours':'6'}, {'profile':'not-configured'},
                     {'at':float('nan')}, {'at':NOW+1}, {'at':-1}, {'at':True}):
            with self.subTest(args=args), self.assertRaises(ValueError): self.series(**args)

    def test_partials_are_marked_and_sums_are_exact(self):
        self.put(at=NOW-2, ingested=NOW-1)
        data = usage_series(self.store, PROFILES, hours=1, now=NOW+13)
        points = data['series'][0]['points']
        self.assertTrue(points[0]['partial']); self.assertTrue(points[-1]['partial'])
        self.assertEqual(sum(p['values']['total_tokens'] or 0 for p in points),110)
        self.assertEqual(sum(p['end']-p['start'] for p in points),3600)

    def test_no_source_text_in_response_and_no_writes(self):
        self.put()
        before = self.store.sequence(); definitions = copy.deepcopy(PROFILES)
        with self.store.connect() as db:
            observed=[];db.set_trace_callback(observed.append)
            data=usage_series(self.store, PROFILES, now=NOW)
        self.assertEqual(self.store.sequence(),before);self.assertEqual(PROFILES,definitions)
        self.assertEqual(len(observed),5)
        self.assertFalse(any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for s in observed))
        self.assertNotIn('raw_sha256',json.dumps(data));self.assertNotIn('payload',json.dumps(data))

    def test_other_profiles_and_unsupported_usage_do_not_leak(self):
        self.put(response='foreign',profile='other')
        self.put(response=None)
        data=self.series()
        self.assertTrue(all(s['responses']==0 for s in data['series']))
        self.assertEqual(data['cursor'],self.store.sequence())

    def test_limit_hourly_rate_is_trailing_projection_and_line_uses_maximum(self):
        self.put_limit(78, profile='astra', at=NOW-1900, ingested=NOW-1899)
        self.put_limit(80, profile='astra', at=NOW-100, ingested=NOW-99)
        self.put_limit(39, profile='sol', at=NOW-1900, ingested=NOW-1899)
        self.put_limit(40, profile='sol', at=NOW-100, ingested=NOW-99)
        data=self.series()
        latest={item['profile']:item for item in data['limit']['latest']}
        self.assertEqual(latest['astra']['rate_percent_per_hour'],4)
        self.assertEqual(latest['astra']['rate_delta_percent'],2)
        self.assertFalse(latest['astra']['stale'])
        self.assertEqual(latest['sol']['rate_percent_per_hour'],2)
        self.assertFalse(latest['sol']['stale'])
        self.assertNotIn('used_percent',latest['astra'])
        measured=[point for point in data['limit_series']
                  if point['rate_percent_per_hour'] is not None]
        self.assertEqual(measured[-1]['rate_percent_per_hour'],4)
        self.assertEqual(measured[-1]['measured_profiles'],2)
        self.assertEqual(measured[-1]['profile'],'astra')
        self.assertEqual(data['limit']['aggregation'],'maximum_observed_rate_percent_per_hour')
        self.assertEqual(data['limit']['rate_target_seconds'],30*60)
        self.assertEqual(data['limit']['latest_lookback_seconds'],7*24*3600)
        self.assertNotEqual(measured[-1]['rate_percent_per_hour'],6)
        for hours in WINDOWS:
            current={item['profile']:item['rate_percent_per_hour']
                     for item in self.series(hours=hours)['limit']['latest']}
            self.assertEqual(current,{'astra':4,'sol':2})

    def test_limit_rate_respects_knowledge_horizon_and_unknown_is_not_zero(self):
        self.put_limit(70, at=NOW-1900, ingested=NOW-1890)
        self.put_limit(72, at=NOW-100, ingested=NOW-10)
        historical=self.series(at=NOW-20)
        self.assertIsNone(historical['limit']['latest'][0]['rate_percent_per_hour'])
        self.assertTrue(all(point['rate_percent_per_hour'] is None
                            for point in historical['limit_series']))
        self.assertEqual(self.series()['limit']['latest'][0]['rate_percent_per_hour'],4)

    def test_current_limit_rate_is_independent_of_chart_bucket_size(self):
        self.put_limit(10, at=NOW-2200, ingested=NOW-2199)
        self.put_limit(11, at=NOW-1801, ingested=NOW-1800)
        self.put_limit(12, at=NOW-400, ingested=NOW-399)
        rates=[]
        for hours in WINDOWS:
            latest=self.series(hours=hours)['limit']['latest'][0]
            rates.append(latest['rate_percent_per_hour'])
        self.assertEqual(rates,[4]*len(WINDOWS))

    def test_limit_rate_expands_window_when_half_hour_counter_is_flat(self):
        self.put_limit(20, at=NOW-3700, ingested=NOW-3699)
        self.put_limit(21, at=NOW-1900, ingested=NOW-1899)
        self.put_limit(21, at=NOW-100, ingested=NOW-99)
        latest=self.series()['limit']['latest'][0]
        self.assertEqual(latest['rate_percent_per_hour'],1)
        self.assertEqual(latest['rate_elapsed_seconds'],3600)
        self.assertEqual(latest['rate_state'],'measured')

    def test_token_volume_never_fabricates_a_limit_rate(self):
        self.put_limit(20, at=NOW-100, ingested=NOW-99)
        self.put(amount=10_000_000, at=NOW-50, ingested=NOW-49)
        latest=self.series()['limit']['latest'][0]
        self.assertIsNone(latest['rate_percent_per_hour'])
        self.assertEqual(latest['rate_state'],'insufficient_history')

    def test_limit_rate_is_unknown_across_reset_or_without_half_hour_basis(self):
        self.put_limit(99, at=NOW-1900, ingested=NOW-1899, resets_at=NOW-1000)
        self.put_limit(2, at=NOW-100, ingested=NOW-99, resets_at=NOW+86400)
        data=self.series()
        self.assertIsNone(data['limit']['latest'][0]['rate_percent_per_hour'])
        self.assertEqual(data['limit']['latest'][0]['rate_state'],'window_changed')
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'own.sqlite')
            original=self.store;self.store=store
            try:
                self.put_limit(12, at=NOW-100, ingested=NOW-99)
                only=self.series()
                self.assertIsNone(only['limit']['latest'][0]['rate_percent_per_hour'])
                self.assertEqual(only['limit']['latest'][0]['rate_state'],'insufficient_history')
            finally:self.store=original

    def test_limit_line_omits_side_quotas_secondary_and_disputed_values(self):
        self.put_limit(30, delivery='account')
        self.put_limit(99, delivery='model', limit_id='codex_bengalfox')
        self.put_limit(95, delivery='secondary', role='secondary')
        self.put_limit(80, delivery='account', ingested=NOW-20)
        self.assertIsNone(self.series()['limit']['latest'][0]['rate_percent_per_hour'])
        earlier=self.series(at=NOW-30)
        self.assertIsNone(earlier['limit']['latest'][0]['rate_percent_per_hour'])
        self.assertNotIn(99,[p['rate_percent_per_hour'] for p in earlier['limit_series']])

    def test_limit_profile_filter_is_preserved(self):
        self.put_limit(24, profile='astra', at=NOW-1900, ingested=NOW-1899)
        self.put_limit(25, profile='astra')
        self.put_limit(54, profile='sol', at=NOW-1900, ingested=NOW-1899)
        self.put_limit(55, profile='sol')
        data=self.series(profile='sol')
        self.assertEqual([item['profile'] for item in data['limit']['latest']],['sol'])
        self.assertEqual({p['profile'] for p in data['limit_series'] if p['profile']},{'sol'})

    def anomaly_baseline(self, *, recent_multiplier=1, recent_responses=1):
        start=NOW-(6*3600+15*60)
        for slot in range(24):
            self.put(response=f'base-{slot}',at=start+slot*900+30,
                     ingested=start+slot*900+31,amount=90)
        for index in range(recent_responses):
            self.put(response=f'recent-{index}',at=NOW-800+index,
                     ingested=NOW-799+index,amount=90*recent_multiplier//recent_responses,
                     output_tokens=10*recent_multiplier//recent_responses,
                     total_tokens=100*recent_multiplier//recent_responses)
        return self.series()['anomaly']

    def test_anomaly_index_is_fixed_window_relative_and_bounded(self):
        normal=self.anomaly_baseline()
        self.assertEqual(normal['score'],25)
        self.assertEqual(normal['level'],'green')
        self.assertEqual(normal['recent_minutes'],15)
        self.assertEqual(normal['baseline_hours'],6)
        self.assertEqual(normal['ratio'],1)
        self.assertEqual(normal['score_source'],'combined')
        self.assertEqual(normal['score_confidence'],normal['confidence'])
        self.assertNotIn('limit_percent',normal)

    def test_confident_fourfold_rate_is_red(self):
        anomaly=self.anomaly_baseline(recent_multiplier=4,recent_responses=4)
        self.assertEqual(anomaly['score'],100)
        self.assertEqual(anomaly['level'],'red')
        self.assertEqual(anomaly['confidence'],'high')

    def test_historical_anomaly_series_matches_live_score_and_is_bounded(self):
        self.anomaly_baseline(recent_multiplier=4,recent_responses=4)
        data=self.series(hours=6)
        line=data['anomaly_series']
        self.assertEqual(len(line),len(data['series'][0]['points']))
        self.assertEqual(line[-1]['score'],data['anomaly']['score'])
        self.assertEqual(line[-1]['score_source'],data['anomaly']['score_source'])
        self.assertTrue(all(p['score'] is None or 0<=p['score']<=100 for p in line))
        scores=[p['score'] for p in line if p['score'] is not None]
        self.assertIn(25,scores)
        self.assertEqual(scores[-1],100)

    def test_strongest_profile_controls_the_overall_alert(self):
        self.anomaly_baseline(recent_multiplier=4,recent_responses=4)
        start=NOW-(6*3600+15*60)
        for slot in range(24):
            self.put(response=f'sol-base-{slot}',profile='sol',at=start+slot*900+30,
                     ingested=start+slot*900+31,amount=90)
        self.put(response='sol-recent',profile='sol',at=NOW-100,ingested=NOW-99,amount=90)
        anomaly=self.series()['anomaly']
        self.assertEqual(anomaly['score'],100)
        self.assertEqual(anomaly['score_source'],'astra')
        self.assertEqual(anomaly['score_source_label'],'Astra')
        self.assertEqual(anomaly['score_confidence'],'high')
        self.assertLess(anomaly['ratio'],4)

    def test_absent_measurements_are_unknown_but_measured_zero_is_zero(self):
        self.assertIsNone(self.series()['anomaly']['score'])
        start=NOW-(6*3600+15*60)
        for slot in range(8):
            self.put(response=f'base-{slot}',at=start+slot*900+30,
                     ingested=start+slot*900+31,amount=90)
        self.put(response='zero',at=NOW-100,ingested=NOW-99,amount=0,
                 output_tokens=0,reasoning_output_tokens=0,total_tokens=0)
        anomaly=self.series()['anomaly']
        self.assertEqual(anomaly['score'],0)
        self.assertEqual(anomaly['level'],'green')
        self.assertEqual(anomaly['recent_tokens'],0)


class API(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.cfg=load(make_demo(Path(tmp.name)/'demo'))
        self.app=create_app(self.cfg,collect=False);self.app.state.collector.tick()
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8866')
        self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)

    def login(self):
        token=Path(self.cfg['token_file']).read_text().strip()
        response=self.client.post('/api/v1/login',json={'token':token},headers={'Origin':'http://127.0.0.1:8866'})
        self.assertEqual(response.status_code,200)

    def test_authentication_is_mandatory(self):
        self.assertEqual(self.client.get('/api/v1/usage-series').status_code,401)
        self.login();self.assertEqual(self.client.get('/api/v1/usage-series').status_code,200)

    def test_all_presets_and_profile(self):
        self.login()
        for hours in WINDOWS:
            r=self.client.get(f'/api/v1/usage-series?hours={hours}&profile=sol')
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual([s['profile'] for s in r.json()['series']],['sol'])
            self.assertTrue(r.json()['demo'])

    def test_invalid_unbounded_and_repeated_params(self):
        self.login()
        for q in ('hours=25','hours=0','hours=NaN','hours=6&hours=1','profile=unknown','sql=SELECT','at=2026-09-10','at=garbage'):
            self.assertEqual(self.client.get('/api/v1/usage-series?'+q).status_code,400,q)

    def test_static_chart_assets_and_read_only_boundary(self):
        for p in ('/token-graph','/token-graph.js','/token-graph.css'):
            r=self.client.get(p);self.assertEqual(r.status_code,200)
            self.assertIn("default-src 'none'",r.headers['content-security-policy'])
        page=self.client.get('/token-graph').text
        script=self.client.get('/token-graph.js').text
        self.assertIn('graph-anomaly',page);self.assertIn('paintAnomaly',script);self.assertIn('anomaly_series',script)
        self.assertIn('rate_percent_per_hour',script);self.assertIn('series-limit',script)
        self.login();payload=self.client.get('/api/v1/usage-series').json()
        self.assertIn('limit',payload);self.assertIn('limit_series',payload)
        self.login();self.assertEqual(self.client.post('/api/v1/usage-series',json={}).status_code,405)

    def test_fixed_time_is_valid_and_future_is_rejected(self):
        self.login()
        r=self.client.get('/api/v1/usage-series',params={'at':iso(time.time()-2)})
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['mode'],'historical')
        r=self.client.get('/api/v1/usage-series',params={'at':iso(time.time()+3600)})
        self.assertEqual(r.status_code,400)

if __name__=='__main__':unittest.main(verbosity=2)
