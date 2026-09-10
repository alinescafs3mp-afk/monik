"""Event details must use the same historical upper bounds as their timeline.

All records and credentials below are generated in temporary directories. These
checks do not connect to Codex, read owner logs, or change the storage schema.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from monik.app import create_app
from monik.cli import make_demo
from monik.config import load
from monik.model import iso, normalize
from monik.storage import Store


def deliver(store, *, native='message-1', event_at=100, ingested_at=200, source='first'):
    row = {
        'timestamp': event_at,
        'type': 'response_item',
        'payload': {'type': 'message', 'id': native, 'role': 'assistant',
                    'content': [{'text': 'Тестовая запись: оригинальное содержимое.'}]},
    }
    event = normalize(row, 'sol', 'SOL_TEST', source)[0]
    with store.connect(write=True) as db:
        eid = store.put(db, event, {'source': source, 'delivery': source,
                                   'ingested_at': ingested_at, 'observed_at': ingested_at})
        return db.execute('SELECT uid FROM events WHERE id=?', (eid,)).fetchone()[0]


class DetailTimeBounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'observer.sqlite')
        self.uid = deliver(self.store)
        deliver(self.store, source='later-copy', ingested_at=400)

    def test_event_time_upper_bound_matches_timeline(self):
        params = {'view': 'event', 'until': 99}
        self.assertEqual(self.store.events(params)['items'], [])
        self.assertIsNone(self.store.detail(self.uid, params))

    def test_knowledge_time_upper_bound_matches_timeline(self):
        params = {'view': 'knowledge', 'until': 199}
        self.assertEqual(self.store.events(params)['items'], [])
        self.assertIsNone(self.store.detail(self.uid, params))

    def test_until_hides_later_provenance_and_its_count(self):
        result = self.store.detail(self.uid, {'view': 'knowledge', 'until': 300})
        self.assertEqual(result['provenance_count'], 1)
        self.assertEqual([p['source'] for p in result['provenance']], ['first'])
        self.assertTrue(all(p['ingested_at'] <= 300 for p in result['provenance']))

    def test_stricter_of_at_and_until_wins_in_both_orders(self):
        for at, until in ((500, 300), (300, 500)):
            with self.subTest(at=at, until=until):
                result = self.store.detail(self.uid, {'view': 'knowledge', 'at': at, 'until': until})
                self.assertEqual(result['provenance_count'], 1)
                self.assertEqual([p['source'] for p in result['provenance']], ['first'])

    def test_stricter_until_can_exclude_the_whole_event(self):
        for view, until in (('event', 99), ('knowledge', 199)):
            with self.subTest(view=view):
                self.assertIsNone(self.store.detail(self.uid, {'view': view, 'at': 500, 'until': until}))

    def test_boundary_is_inclusive(self):
        result = self.store.detail(self.uid, {'view': 'knowledge', 'until': 200})
        self.assertIsNotNone(result)
        self.assertEqual(result['provenance_count'], 1)
        self.assertEqual(result['provenance'][0]['ingested_at'], 200)
        self.assertEqual(self.store.detail(self.uid, {'view': 'knowledge', 'until': 400})['provenance_count'], 2)

    def test_zero_upper_bound_is_not_treated_as_missing(self):
        uid = deliver(self.store, native='epoch', event_at=0, ingested_at=0, source='epoch-first')
        deliver(self.store, native='epoch', event_at=0, ingested_at=1, source='epoch-later')
        result = self.store.detail(uid, {'view': 'knowledge', 'at': 500, 'until': 0})
        self.assertEqual(result['provenance_count'], 1)
        self.assertEqual(result['provenance'][0]['source'], 'epoch-first')

    def test_event_time_view_still_includes_later_discovered_provenance(self):
        # Event-time is reconstruction, not a claim of observer knowledge at T.
        result = self.store.detail(self.uid, {'view': 'event', 'until': 100})
        self.assertEqual(result['provenance_count'], 2)
        self.assertEqual({p['source'] for p in result['provenance']}, {'first', 'later-copy'})

    def test_unbounded_and_at_only_views_keep_their_contract(self):
        self.assertEqual(self.store.detail(self.uid, {'view': 'knowledge'})['provenance_count'], 2)
        self.assertEqual(self.store.detail(self.uid, {'view': 'knowledge', 'at': 300})['provenance_count'], 1)

    def test_provenance_cap_applies_after_historical_filter(self):
        for number in range(105):
            deliver(self.store, source=f'replay-{number}', ingested_at=201 + number)
        result = self.store.detail(self.uid, {'view': 'knowledge', 'until': 301})
        self.assertEqual(result['provenance_count'], 102)
        self.assertEqual(len(result['provenance']), 100)
        self.assertTrue(all(p['ingested_at'] <= 301 for p in result['provenance']))

    def test_read_does_not_mutate_records_or_caller_filters(self):
        with self.store.connect() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM events ORDER BY id')]
        params = {'view': 'knowledge', 'until': 300, 'q': 'unrelated search', 'kind': 'settings'}
        original = dict(params)
        result = self.store.detail(self.uid, params)
        self.assertEqual(params, original)
        self.assertEqual(result['data']['content'][0]['text'], 'Тестовая запись: оригинальное содержимое.')
        with self.store.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM events ORDER BY id')], before)


class DetailTimeBoundsHTTP(unittest.TestCase):
    def test_authenticated_route_preserves_upper_bounds_through_paging(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load(make_demo(Path(tmp) / 'demo'))
            app = create_app(cfg, collect=False)
            uid = deliver(app.state.store)
            deliver(app.state.store, source='later-copy', ingested_at=400)
            token = Path(cfg['token_file']).read_text().strip()
            with TestClient(app, base_url='http://127.0.0.1:8866') as client:
                headers = {'Authorization': 'Bearer ' + token}
                path = '/api/v1/events/' + uid
                for view, until in (('event', 99), ('knowledge', 199)):
                    with self.subTest(view=view):
                        params = {'view': view, 'until': iso(until)}
                        self.assertEqual(client.get('/api/v1/events', params=params, headers=headers).json()['items'], [])
                        self.assertEqual(client.get(path, params=params, headers=headers).status_code, 404)
                for offset in (0, 10):
                    response = client.get(path, params={'view': 'knowledge', 'at': iso(500),
                                                        'until': iso(300), 'offset': offset}, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    body = response.json()
                    self.assertEqual(body['provenance_count'], 1)
                    self.assertEqual([p['source'] for p in body['provenance']], ['first'])
                    self.assertIn('payload_page', body)
                self.assertEqual(client.get(path).status_code, 401)


if __name__ == '__main__':
    unittest.main()
