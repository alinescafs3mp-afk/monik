"""The overview API must honor profile selection before computing any cards.

Uses a temporary own database and an in-process API test client. No real Codex
sources, processes, network credentials or schema changes are involved.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from monik.app import create_app
from monik.cli import make_demo
from monik.config import load
from monik.model import normalize
from monik.storage import Store


class OverviewProfile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'observer.sqlite')
        self.profiles = [{'name': 'astra', 'root_id': 'ASTRA'}, {'name': 'sol', 'root_id': 'SOL'}]
        with self.store.connect(write=True) as db:
            for p, total in zip(self.profiles, (110, 210)):
                rows = [
                    {'timestamp': 90, 'type': 'session_meta', 'payload': {'id': p['root_id'], 'source': 'cli'}},
                    {'timestamp': 100, 'type': 'token_usage_record', 'payload': {
                        'thread_id': p['root_id'], 'response_id': 'one', 'usage': {
                            'input_tokens': total - 10, 'output_tokens': 10, 'total_tokens': total,
                            'cached_input_tokens': 30, 'reasoning_output_tokens': 2}}},
                ]
                for i, row in enumerate(rows):
                    delivery = p['name'] + ':' + str(i)
                    for e in normalize(row, p['name'], p['root_id'], delivery):
                        self.store.put(db, e, {'source': 'synthetic', 'delivery': delivery, 'ingested_at': 200})

    def test_no_filter_preserves_all_cards_and_configuration_order(self):
        result = self.store.overview({}, list(reversed(self.profiles)))
        self.assertEqual([p['profile'] for p in result['profiles']], ['sol', 'astra'])

    def test_empty_filter_means_all_profiles(self):
        for value in ('', None):
            with self.subTest(value=value):
                result = self.store.overview({'profile': value}, self.profiles)
                self.assertEqual([p['profile'] for p in result['profiles']], ['astra', 'sol'])

    def test_sol_filter_returns_only_sol_with_its_own_numbers(self):
        result = self.store.overview({'profile': 'sol'}, self.profiles)
        self.assertEqual([p['profile'] for p in result['profiles']], ['sol'])
        self.assertEqual(result['profiles'][0]['usage']['total_tokens'], 210)
        self.assertEqual(result['profiles'][0]['own_usage']['total_tokens'], 210)

    def test_astra_filter_returns_only_astra(self):
        result = self.store.overview({'profile': 'astra'}, self.profiles)
        self.assertEqual([p['profile'] for p in result['profiles']], ['astra'])
        self.assertEqual(result['profiles'][0]['usage']['total_tokens'], 110)

    def test_unselected_profile_is_not_computed_then_discarded(self):
        with patch.object(self.store, 'usage', wraps=self.store.usage) as usage, \
             patch.object(self.store, 'settings', wraps=self.store.settings) as settings, \
             patch.object(self.store, 'latest', wraps=self.store.latest) as latest:
            self.store.overview({'profile': 'sol'}, self.profiles)
        self.assertEqual(len(usage.call_args_list), 2)
        self.assertTrue(all(c.args[0]['profile'] == 'sol' for c in usage.call_args_list))
        self.assertTrue(all(c.args[1] == 'sol' for c in settings.call_args_list))
        self.assertTrue(all(c.args[1] == 'sol' for c in latest.call_args_list))

    def test_unknown_profile_returns_no_cards_or_profile_queries(self):
        with patch.object(self.store, 'usage', wraps=self.store.usage) as usage, \
             patch.object(self.store, 'settings', wraps=self.store.settings) as settings:
            result = self.store.overview({'profile': 'not-configured'}, self.profiles)
        self.assertEqual(result['profiles'], [])
        self.assertEqual(result['cursor'], self.store.sequence())
        usage.assert_not_called(); settings.assert_not_called()

    def test_matching_is_exact_and_not_hardcoded_to_two_names(self):
        profiles = [{'name': 'reviewer', 'root_id': None}, *self.profiles]
        self.assertEqual([p['profile'] for p in self.store.overview({'profile': 'reviewer'}, profiles)['profiles']], ['reviewer'])
        self.assertEqual(self.store.overview({'profile': 'Sol'}, profiles)['profiles'], [])

    def test_history_uses_the_same_selected_card_as_unfiltered_history(self):
        for view, at in (('event', 150), ('knowledge', 250), ('knowledge', 150)):
            with self.subTest(view=view, at=at):
                params = {'view': view, 'at': at}
                all_cards = self.store.overview(params, self.profiles)['profiles']
                selected = self.store.overview({**params, 'profile': 'sol'}, self.profiles)['profiles']
                self.assertEqual(selected, [p for p in all_cards if p['profile'] == 'sol'])

    def test_global_durable_cursor_is_not_replaced_by_selected_profile_cursor(self):
        with self.store.connect(write=True) as db:
            event = normalize({'type': 'event_msg', 'payload': {'type': 'task_started'}},
                              'astra', 'ASTRA', 'newest')[0]
            newest = self.store.put(db, event, {'source': 'synthetic', 'delivery': 'newest'})
        result = self.store.overview({'profile': 'sol'}, self.profiles)
        self.assertEqual([p['profile'] for p in result['profiles']], ['sol'])
        self.assertEqual(result['cursor'], newest)

    def test_parameters_configuration_and_stored_rows_stay_unchanged(self):
        params = {'profile': 'sol', 'since': 0, 'q': 'display text', 'kind': 'message:user'}
        old_params, old_profiles = copy.deepcopy(params), copy.deepcopy(self.profiles)
        with self.store.connect() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM events ORDER BY id')]
        self.store.overview(params, self.profiles)
        self.assertEqual(params, old_params)
        self.assertEqual(self.profiles, old_profiles)
        with self.store.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM events ORDER BY id')], before)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)


class OverviewProfileAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = load(make_demo(Path(self.temp.name) / 'demo'))
        app = create_app(config, collect=False)
        app.state.collector.tick()
        self.client = TestClient(app, base_url='http://127.0.0.1:8866')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.token = Path(config['token_file']).read_text().strip()
        self.store = app.state.store

    def get(self, profile):
        return self.client.get('/api/v1/overview', params={'profile': profile},
                               headers={'Authorization': 'Bearer ' + self.token})

    def test_authenticated_api_applies_profile_and_keeps_response_contract(self):
        response = self.get('sol')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([p['profile'] for p in body['profiles']], ['sol'])
        self.assertEqual(body['profiles'][0]['usage']['total_tokens'], 20600)
        self.assertEqual(body['cursor'], self.store.sequence())
        self.assertEqual(body['model_calls'], 0)
        self.assertTrue(body['demo'])

    def test_unknown_profile_is_empty_but_an_empty_filter_is_not(self):
        self.assertEqual(self.get('not-configured').json()['profiles'], [])
        self.assertEqual(len(self.get('').json()['profiles']), 2)

    def test_profile_filter_does_not_bypass_authentication(self):
        response = self.client.get('/api/v1/overview', params={'profile': 'sol'})
        self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
