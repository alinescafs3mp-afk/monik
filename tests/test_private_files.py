"""Own configuration, token and lock files reject aliases and weak modes."""
from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from monik.app import create_app
from monik.cli import make_demo,write_config
from monik.config import init,load


class PrivateFiles(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'private';self.root.mkdir(mode=0o700)

    def test_config_update_is_private_atomic_and_leaves_no_static_tmp(self):
        path=self.root/'config.json';write_config(path,{'one':1});first=path.stat().st_ino
        write_config(path,{'two':2})
        self.assertNotEqual(path.stat().st_ino,first)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(list(self.root.glob('.config.json.*.tmp')),[])
        self.assertFalse((self.root/'config.tmp').exists())

    def test_init_creates_and_revalidates_private_config_and_token(self):
        path=init(self.root/'new'/'config.json',home=self.root/'owner-home')
        config=load(path);token=Path(config['token_file'])
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(token.stat().st_mode & 0o777,0o600)
        self.assertEqual(init(path),path)

    def test_init_migrates_missing_logs_db_but_preserves_explicit_opt_out(self):
        path=init(self.root/'migrate'/'config.json',home=self.root/'owner-home')
        config=load(path);config.pop('_path',None)
        config['profiles'][0].pop('logs_db')
        config['profiles'][1]['logs_db']=None
        path.write_text(json.dumps(config));path.chmod(0o600)
        init(path)
        migrated=load(path)
        self.assertEqual(migrated['profiles'][0]['logs_db'],
                         str(Path(migrated['profiles'][0]['state_db']).with_name('logs_2.sqlite')))
        self.assertIsNone(migrated['profiles'][1]['logs_db'])

    def test_config_symlink_hardlink_and_weak_mode_are_refused(self):
        original=self.root/'original';write_config(original,{'safe':True})
        symlink=self.root/'symlink';symlink.symlink_to(original)
        with self.assertRaises(ValueError): write_config(symlink,{'safe':False})
        hardlink=self.root/'hardlink';os.link(original,hardlink)
        with self.assertRaises(ValueError): write_config(hardlink,{'safe':False})
        hardlink.unlink();os.chmod(original,0o644)
        with self.assertRaises(ValueError): write_config(original,{'safe':False})

    def test_token_symlink_and_hardlink_are_refused_by_server(self):
        config_path=make_demo(self.root/'demo');config=load(config_path)
        token=Path(config['token_file']);real=self.root/'copied-token';real.write_bytes(token.read_bytes());real.chmod(0o600)
        token.unlink();token.symlink_to(real)
        with self.assertRaises(ValueError): create_app(config,collect=False)
        token.unlink();os.link(real,token)
        with self.assertRaises(ValueError): create_app(config,collect=False)

    def test_collector_lock_must_be_private_single_link_regular_file(self):
        for variant in ('weak','hardlink','symlink'):
            with self.subTest(variant=variant):
                base=self.root/variant;config=load(make_demo(base))
                data=Path(config['data_dir']);data.mkdir(mode=0o700)
                lock=data/'collector.lock';target=base/'lock-target';target.write_text('');target.chmod(0o600)
                if variant=='weak': lock.write_text('');lock.chmod(0o644)
                elif variant=='hardlink': os.link(target,lock)
                else: lock.symlink_to(target)
                app=create_app(config,collect=False)
                with self.assertRaises(ValueError):
                    with TestClient(app): pass


if __name__=='__main__': unittest.main()
