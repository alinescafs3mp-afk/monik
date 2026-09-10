"""A contradiction becomes known on ingestion, not at the original response time."""
from pathlib import Path
import tempfile
import unittest
from monik.model import normalize
from monik.storage import Store


class UsageHistory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'observer.sqlite')
        self.put('first', 100, 200)

    def put(self, delivery, amount, ingested):
        row = {'timestamp':100, 'type':'token_usage_record', 'payload':{
            'response_id':'same-response', 'usage':{'input_tokens':amount,
            'cached_input_tokens':30, 'output_tokens':10,
            'reasoning_output_tokens':2, 'total_tokens':amount+10}}}
        with self.store.connect(write=True) as db:
            for event in normalize(row, 'astra', 'root', delivery):
                self.store.put(db, event, {'source':'synthetic','delivery':delivery,'ingested_at':ingested})

    def test_event_time_view_is_not_rewritten_by_a_late_conflict(self):
        self.put('conflicting', 200, 400)
        self.assertEqual(self.store.usage({'at':300})['total_tokens'], 110)
        self.assertIsNone(self.store.usage({'at':500})['total_tokens'])

    def test_knowledge_view_and_exact_detection_boundary(self):
        self.put('conflicting', 200, 400)
        for view in ('event','knowledge'):
            with self.subTest(view=view):
                self.assertEqual(self.store.usage({'at':399,'view':view})['conflicts'], 0)
                self.assertEqual(self.store.usage({'at':400,'view':view})['conflicts'], 1)
        self.assertIsNone(self.store.usage({'at':199,'view':'knowledge'})['total_tokens'])

    def test_until_uses_detection_time_in_both_views(self):
        self.put('conflicting', 200, 400)
        for view in ('event','knowledge'):
            with self.subTest(view=view):
                self.assertEqual(self.store.usage({'until':300,'view':view})['total_tokens'], 110)
                self.assertIsNone(self.store.usage({'until':500,'view':view})['total_tokens'])

    def test_earlier_bound_wins_when_both_cutoffs_are_present(self):
        self.put('conflicting', 200, 400)
        for bounds in ({'at':500,'until':300},{'at':300,'until':500}):
            self.assertEqual(self.store.usage(bounds)['total_tokens'], 110)

    def test_repeat_does_not_move_first_detection(self):
        self.put('conflicting', 200, 400)
        self.put('repeat', 200, 600)
        self.assertEqual(self.store.usage({'at':500})['conflicts'], 1)
        self.assertEqual(self.store.usage({'at':300})['total_tokens'], 110)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='conflict'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT total_tokens FROM response_usage').fetchone()[0], 110)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_undisputed_duplicates_keep_totals_and_cache_ratio(self):
        self.put('duplicate', 100, 300)
        result = self.store.usage({})
        self.assertEqual(result['responses'], 1)
        self.assertEqual(result['conflicts'], 0)
        self.assertEqual(result['total_tokens'], 110)
        self.assertEqual(result['cache_hit_ratio'], .3)

    def test_live_exclusion_survives_reopen(self):
        self.put('conflicting', 200, 400)
        reopened = Store(self.store.path)
        self.assertIsNone(reopened.usage({})['total_tokens'])
        self.assertEqual(reopened.usage({'at':300})['total_tokens'], 110)

if __name__ == '__main__': unittest.main()
