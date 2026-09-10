"""Compatibility for limit snapshots already stored with integral reset JSON."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from monik.model import normalize
from monik.storage import Store


def limit_event(reset, *, used=10, window=300):
    row={'timestamp':100,'type':'event_msg','payload':{'type':'token_count','rate_limits':{
        'limit_id':'codex','primary':{'used_percent':used,'window_minutes':window,'resets_at':reset}}}}
    return normalize(row,'astra','ASTRA','stable-source-record')[0]


class ResetReplay(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'observer.sqlite')

    def put(self,event,delivery):
        with self.store.connect(write=True) as db:
            return self.store.put(db,event,{'source':'synthetic','delivery':delivery,'ingested_at':200})

    def test_equal_integer_and_float_reset_keep_canonical_and_provenance(self):
        uid=self.put(limit_event(1_700_000_000),'first')
        self.assertEqual(self.put(limit_event(1_700_000_000.0),'replay'),uid)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='limit'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='conflict'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM provenance WHERE event_id=?',(uid,)).fetchone()[0],2)

    def test_boolean_reset_is_not_numeric_equivalence(self):
        self.put(limit_event(1),'first');self.put(limit_event(True),'boolean')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='conflict'").fetchone()[0],1)

    def test_real_reset_and_window_changes_remain_conflicts(self):
        self.put(limit_event(1_700_000_000),'first')
        self.put(limit_event(1_700_000_001),'changed-reset')
        self.put(limit_event(1_700_000_000,window=301),'changed-window')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='conflict'").fetchone()[0],2)


if __name__=='__main__': unittest.main()
