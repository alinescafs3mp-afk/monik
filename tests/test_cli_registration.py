"""The installed entry point exposes TUI without importing curses elsewhere."""
from __future__ import annotations

import builtins
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monik import cli
from monik.cli import make_demo


class CliRegistration(unittest.TestCase):
    def test_cli_import_keeps_sqlite_native_module_lazy(self):
        root=Path(__file__).resolve().parents[1]
        code="import sys;assert 'sqlite3' not in sys.modules;import monik.cli;assert 'sqlite3' not in sys.modules"
        env={**os.environ,'PYTHONPATH':str(root)}
        result=subprocess.run([sys.executable,'-S','-c',code],cwd='/tmp',env=env,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_tui_help_lists_public_flags_without_importing_tui(self):
        output=io.StringIO()
        with patch.object(sys,'argv',['monik','tui','--help']),patch.dict(sys.modules,{'monik.tui':None}), \
             contextlib.redirect_stdout(output),self.assertRaises(SystemExit) as stopped:
            cli.main()
        self.assertEqual(stopped.exception.code,0)
        for flag in ('--config','--once','--page','--profile','--at','--ca-file'):
            self.assertIn(flag,output.getvalue())

    def test_non_tui_command_does_not_import_curses(self):
        with tempfile.TemporaryDirectory() as folder:
            config=make_demo(Path(folder)/'demo')
            real_import=builtins.__import__
            def guarded(name,*args,**kwargs):
                if name=='curses' or name.startswith('monik.tui'):
                    raise AssertionError('TUI dependency imported by token command')
                return real_import(name,*args,**kwargs)
            with patch.object(sys,'argv',['monik','token','--config',str(config)]), \
                 patch('builtins.__import__',side_effect=guarded),contextlib.redirect_stdout(io.StringIO()):
                cli.main()

    def test_tui_arguments_reach_existing_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            config=make_demo(Path(folder)/'demo')
            with patch.object(sys,'argv',['monik','tui','--config',str(config),'--once','--page','tokens','--profile','sol',
                                        '--at','2026-09-10T10:00:00+03:00','--ca-file','/tmp/public-ca.crt']), \
                 patch('monik.tui.run') as run:
                cli.main()
            self.assertEqual(run.call_args.kwargs,{'once':True,'page':'tokens','profile':'sol',
                                                   'at':'2026-09-10T10:00:00+03:00','ca_file':'/tmp/public-ca.crt'})


if __name__=='__main__': unittest.main()
