"""Independent regression tests for the observer's own read transactions."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from monik.model import normalize
from monik.storage import Store

class SnapshotConsistency(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'observer.sqlite')

    def append(self,identity):
        row={'timestamp':100,'type':'response_item','payload':{'type':'message','role':'assistant','id':identity,'content':[{'text':identity}]}}
        with self.store.connect(write=True) as db:
            for event in normalize(row,'astra','root',identity):
                self.store.put(db,event,{'source':'synthetic','delivery':identity})

    def test_nested_reads_use_same_connection(self):
        with self.store.connect() as outer:
            with self.store.connect() as inner:self.assertIs(outer,inner)
        with self.store.connect() as later:self.assertIsNot(later,outer)

    def test_commit_is_invisible_inside_current_snapshot(self):
        self.append('first')
        with self.store.connect() as outer:
            initial=outer.execute('SELECT count(*) FROM events').fetchone()[0]
            self.append('second')
            with self.store.connect() as inner:
                self.assertEqual(inner.execute('SELECT count(*) FROM events').fetchone()[0],initial)
        with self.store.connect() as later:
            self.assertEqual(later.execute('SELECT count(*) FROM events').fetchone()[0],initial+1)

    def test_overview_opens_one_read_connection(self):
        original=sqlite3.connect;opened=[]
        def connect(*args,**kwargs):
            db=original(*args,**kwargs);opened.append(db);return db
        with patch('sqlite3.connect',side_effect=connect):
            self.store.overview({},[{'name':'astra','root_id':None},{'name':'sol','root_id':None}])
        self.assertEqual(len(opened),1)

    def test_exception_does_not_leave_reusable_closed_connection(self):
        with self.assertRaises(RuntimeError):
            with self.store.connect() as outer:
                with self.store.connect():raise RuntimeError('synthetic')
        with self.store.connect() as fresh:
            self.assertIsNot(fresh,outer)
            self.assertEqual(fresh.execute('SELECT 1').fetchone()[0],1)

    def test_other_thread_uses_its_own_connection(self):
        used=[]
        with self.store.connect() as outer:
            def read():
                with self.store.connect() as other:
                    used.append((other is outer,other.execute('SELECT 1').fetchone()[0]))
            worker=threading.Thread(target=read);worker.start();worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(used,[(False,1)])

    def test_schema_and_event_identity_remain_compatible(self):
        self.append('same');first=self.store.events({})['items'][0]['uid']
        self.append('same')
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)
        self.assertEqual(self.store.events({})['items'][0]['uid'],first)

if __name__=='__main__':unittest.main()
