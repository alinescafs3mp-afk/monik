"""Terminal rendering/state regressions. No CLI, source ingestion or network."""
from __future__ import annotations

import copy
import curses
import queue
import unittest
from unittest.mock import patch

from monik.tui import _screen, clip, number, render, run, terminal_text


def event(uid='a' * 64, text='synthetic message'):
    return dict(uid=uid, id=1, event_at=100, time=100, profile='astra',
                kind='message:assistant', thread_id='root', text=text)


class ScriptedFetcher:
    def __init__(self, result=None):
        self.results = queue.Queue()
        self.requests = []
        self.initial = result

    def request(self, version, page, params):
        self.requests.append((version, page, copy.deepcopy(params)))
        if len(self.requests) == 1 and self.initial is not None:
            self.results.put((version, copy.deepcopy(self.initial), None))


class ScriptedScreen:
    def __init__(self, keys, clock=None):
        self.keys = iter(keys)
        self.lines = {}
        self.frames = []
        self.clock = clock

    def getmaxyx(self): return 24, 100
    def timeout(self, value): pass
    def keypad(self, value): pass
    def erase(self): self.lines = {}
    def addstr(self, row, col, text, style=0): self.lines[row] = text
    def refresh(self): self.frames.append(dict(self.lines))

    def get_wch(self):
        item = next(self.keys, 'q')
        if callable(item): item = item()
        if self.clock is not None: self.clock[0] += 3
        if item is None: raise curses.error('scripted tick')
        return item


class TerminalUnit(unittest.TestCase):
    def test_invalid_options_are_rejected_before_network(self):
        with patch('monik.tui.Client') as client:
            for options in ({'page': 'not-a-page'}, {'profile': 'foreign'}, {'at': '2026-09-10'}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    run({'profiles': [{'name': 'astra'}]}, once=True, **options)
            client.assert_not_called()

    def test_untrusted_numeric_values_do_not_crash_renderer(self):
        for value in ({}, [], True, float('inf'), float('nan'), '32'):
            with self.subTest(value=value): self.assertEqual(number(value), 'нет измерения')
        self.assertEqual(number(0), '0')
        self.assertEqual(number(1234567), '1 234 567')
        data = {'latest': [dict(profile='astra', limit_id='codex', role='primary',
                    remaining_percent=None, window_minutes=[], resets_at='bad', age_seconds=3, stale=False)]}
        self.assertIn('нет измерения', '\n'.join(render('limits', data)))

    def test_control_sequences_are_inert_and_unicode_width_is_respected(self):
        raw = '\x1b]52;c;clipboard\x07ok\x1b[2J\u202eРусский\x00\ntext'
        cleaned = terminal_text(raw)
        self.assertNotIn('clipboard', cleaned)
        for char in ('\x1b', '\x07', '\x00', '\u202e'): self.assertNotIn(char, cleaned)
        self.assertEqual(clip('你好ABC', 4), '你好')
        self.assertEqual(clip('е\u0301ABC', 2), 'е\u0301A')

    def test_protocol_values_are_not_translated(self):
        data={'profiles': [dict(profile='astra', root_id='ROOT_UUID', usage={},
                settings={'data': {'model': 'gpt-6-astra', 'effort': 'ultra', 'service_tier': 'default'}})]}
        text='\n'.join(render('overview', data, 200))
        for expected in ('gpt-6-astra', 'ultra', 'default', 'ROOT_UUID'): self.assertIn(expected, text)

    def test_render_marks_selected_not_first_event(self):
        text=render('activity', {'items': [event('a'*64, 'FIRST'), event('b'*64, 'SECOND')]}, 120, selected=1)
        self.assertEqual(sum(line.startswith('> ') for line in text), 1)
        self.assertIn('SECOND', '\n'.join(text[text.index(next(x for x in text if x.startswith('> '))):]))

    def scripted(self, fetcher, screen, page='activity', params=None, clock=None):
        with patch('monik.tui.curses.curs_set'), patch('monik.tui.time.monotonic', side_effect=lambda: (clock or [0])[0]):
            _screen(screen, fetcher, {'profiles': [{'name': 'astra'}, {'name': 'sol'}]}, page, params or {})

    def test_scroll_discards_inflight_response(self):
        old={'items': [event(text='OLD_RECORD')]}
        fetcher=ScriptedFetcher(old)
        clock=[0]
        def response_after_scroll():
            # The second automatic request was already in flight when scrolling paused the screen.
            fetcher.results.put((fetcher.requests[-1][0], {'items': [event(text='SHOULD_NOT_APPEAR')]}, None))
            return None
        screen=ScriptedScreen([None, curses.KEY_DOWN, response_after_scroll, 'q'], clock)
        self.scripted(fetcher, screen, clock=clock)
        self.assertEqual(len(fetcher.requests), 2)
        self.assertNotIn('SHOULD_NOT_APPEAR', str(screen.frames))
        self.assertIn('OLD_RECORD', str(screen.frames))

    def test_prompt_freezes_automatic_refresh(self):
        fetcher=ScriptedFetcher({'items': [event()]})
        clock=[0];screen=ScriptedScreen(['/', 'a', 'b', 'c', '\x1b', 'q'], clock)
        self.scripted(fetcher, screen, clock=clock)
        # One initial request, one after leaving the input. No requests during editing.
        self.assertEqual(len(fetcher.requests), 2)

    def test_help_preserves_page_and_has_no_network_activity(self):
        fetcher=ScriptedFetcher({'latest': []});clock=[0]
        screen=ScriptedScreen(['?', None, None, '?', 'q'], clock)
        self.scripted(fetcher, screen, page='limits', clock=clock)
        self.assertTrue(all(item[1]=='limits' for item in fetcher.requests))
        self.assertEqual(len(fetcher.requests), 2)
        self.assertIn('Управление monik', str(screen.frames))

    def test_fixed_history_does_not_refresh_in_background(self):
        fetcher=ScriptedFetcher({'items': [event()]});clock=[0]
        screen=ScriptedScreen([None, None, None, 'q'], clock)
        self.scripted(fetcher, screen, params={'at':'2026-09-10T10:00:00Z'}, clock=clock)
        self.assertEqual(len(fetcher.requests), 1)

    def test_selected_event_is_the_one_opened(self):
        fetcher=ScriptedFetcher({'items': [event('a'*64), event('b'*64)]})
        screen=ScriptedScreen(['j', '\n', 'q'])
        self.scripted(fetcher, screen)
        self.assertEqual(fetcher.requests[-1][1], 'detail')
        self.assertEqual(fetcher.requests[-1][2]['_event'], 'b'*64)



if __name__=='__main__': unittest.main(verbosity=2)
