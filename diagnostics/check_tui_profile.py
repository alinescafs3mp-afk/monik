"""Reproduce the terminal profile-filter issue without a real server or source.

This is a diagnostic, not a fix or a CLI entry point. Exit 1 means the defect is
still present. Kept outside tests/ so the existing regression suite is unchanged.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monik import tui


def check():
    response = {'profiles': [{'profile': 'astra'}, {'profile': 'sol'}]}
    config = {'profiles': [{'name': 'astra'}, {'name': 'sol'}]}
    output = io.StringIO()
    with patch.object(tui, 'Client') as client, contextlib.redirect_stdout(output):
        client.return_value.get.return_value = response
        tui.run(config, once=True, profile='sol')
    once_ok = 'SOL' in output.getvalue() and 'ASTRA' not in output.getvalue()

    client = Mock()
    client.get.return_value = response
    fetcher = tui.Fetcher(client)
    try:
        fetcher.request(7, 'overview', {'profile': 'sol'})
        version, data, error = fetcher.results.get(timeout=2)
        background_ok = version == 7 and error is None and [p['profile'] for p in data['profiles']] == ['sol']
    finally:
        fetcher.close()
    result = {
        'ok': once_ok and background_ok,
        'requested_profile': 'sol',
        'checks': {
            'once_excludes_other_profiles': once_ok,
            'background_excludes_other_profiles': background_ok,
            'client_thread_stopped': not fetcher.thread.is_alive(),
        },
        'scope': 'Synthetic response and mocked HTTP client; no source reads, network or Codex control.',
        'meaning': 'Exit 1 reproduces the known display filter defect; this script does not repair it.',
    }
    return result


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)
