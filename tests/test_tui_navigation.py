"""Navigate out of event details without retaining their private request state."""
from __future__ import annotations

import copy
import curses
import queue
import unittest
from unittest.mock import patch

from monik import tui

EVENT = {
    'uid': 'a' * 64, 'profile': 'sol', 'thread_id': 'root', 'kind': 'message:assistant',
    'event_at': 100, 'time': 100, 'ingested_at': 101, 'text': 'Содержимое события',
}
CONFIG = {'profiles': [{'name': 'astra'}, {'name': 'sol'}]}


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.frames = []
        self.rows = {}
    def timeout(self, value): pass
    def keypad(self, value): pass
    def getmaxyx(self): return (28, 100)
    def erase(self): self.rows = {}
    def addstr(self, y, x, text, style=0): self.rows[y] = text
    def refresh(self): self.frames.append(dict(self.rows))
    def get_wch(self): return next(self.keys, 'q')


class Fetcher:
    def __init__(self):
        self.results = queue.Queue()
        self.requests = []
    def request(self, version, page, params):
        self.requests.append((page, dict(params)))
        if page == 'detail':
            result = {**EVENT, 'payload_page': 'Данные выбранного события', 'next_offset': 32768, 'provenance': []}
        else:
            result = {'items': [copy.deepcopy(EVENT)], 'next_before': 7}
        self.results.put((version, result, None))


class TerminalNavigation(unittest.TestCase):
    def screen(self, keys, params=None):
        screen, fetcher = Screen(keys), Fetcher()
        with patch.object(tui.curses, 'curs_set'), patch.object(tui.time, 'monotonic', return_value=100):
            tui._screen(screen, fetcher, CONFIG, 'activity', dict(params or {}))
        return screen, fetcher.requests

    def test_live_from_detail_returns_to_activity(self):
        screen, requests = self.screen(['\n', 'l', 'q'])
        self.assertEqual([page for page, _ in requests], ['activity', 'detail', 'activity'])
        self.assertNotIn('_event', requests[-1][1])
        self.assertNotIn('offset', requests[-1][1])
        self.assertNotIn('Событие: ' + EVENT['uid'], '\n'.join(screen.frames[-1].values()))

    def test_live_clears_historical_cutoff_and_page_but_keeps_profile_and_search(self):
        params = {'profile': 'sol', 'q': 'содержимое', 'search_mode': 'substring', 'at': '2026-09-10T12:00:00+03:00', 'before': 9}
        _, requests = self.screen(['\n', 'l', 'q'], params)
        self.assertEqual(requests[-1], ('activity', {'profile': 'sol', 'q': 'содержимое', 'search_mode': 'substring'}))

    def test_russian_keyboard_live_key_has_same_behavior(self):
        _, requests = self.screen(['\n', 'д', 'q'])
        self.assertEqual(requests[-1], ('activity', {}))

    def test_search_started_from_detail_discards_event_and_payload_page(self):
        _, requests = self.screen(['\n', 'n', '/', 'т', 'е', 'с', 'т', '\n', 'q'], {'profile': 'sol'})
        self.assertEqual(requests[-1], ('activity', {'profile': 'sol', 'q': 'тест', 'search_mode': 'substring'}))

    def test_search_keeps_historical_cutoff_intentionally(self):
        at = '2026-09-10T12:00:00+03:00'
        _, requests = self.screen(['\n', '/', 'x', '\n', 'q'], {'at': at, 'before': 9})
        self.assertEqual(requests[-1], ('activity', {'at': at, 'q': 'x', 'search_mode': 'substring'}))

    def test_escape_keeps_parent_history_instead_of_forcing_live(self):
        params = {'profile': 'sol', 'at': '2026-09-10T12:00:00+03:00', 'before': 9}
        _, requests = self.screen(['\n', '\x1b', 'q'], params)
        self.assertEqual(requests[-1], ('activity', params))

    def test_escape_from_search_keeps_open_detail(self):
        _, requests = self.screen(['\n', '/', 'x', '\x1b', 'q'])
        self.assertEqual([page for page, _ in requests], ['activity', 'detail'])

    def test_detail_pagination_still_requests_same_event(self):
        _, requests = self.screen(['\n', 'n', 'q'], {'profile': 'sol'})
        self.assertEqual(requests[-1][0], 'detail')
        self.assertEqual(requests[-1][1]['_event'], EVENT['uid'])
        self.assertEqual(requests[-1][1]['offset'], 32768)


if __name__ == '__main__':
    unittest.main()
