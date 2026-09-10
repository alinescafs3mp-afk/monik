"""Formatting work is bounded by changed data, not by the keyboard polling rate."""
from __future__ import annotations

import copy
import curses
import queue
import unittest
from unittest.mock import patch

from monik import tui


def event(text='unchanged', uid='a' * 64):
    return dict(uid=uid, event_at=100, time=100, profile='astra',
                kind='message:assistant', thread_id='root', text=text)


class Fetcher:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.results = queue.Queue()
        self.requests = []

    def request(self, version, page, params):
        self.requests.append((version, page, dict(params)))
        result = next(self.responses, None)
        if result is not None:
            self.results.put((version, copy.deepcopy(result), None))


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.width = 100
        self.height = 24
        self.frames = []
        self.lines = {}

    def getmaxyx(self): return self.height, self.width
    def timeout(self, value): pass
    def keypad(self, value): pass
    def erase(self): self.lines = {}
    def addstr(self, row, col, text, style=0): self.lines[row] = text
    def refresh(self): self.frames.append(dict(self.lines))

    def get_wch(self):
        key = next(self.keys, 'q')
        if callable(key): key = key()
        if key is None: raise curses.error('idle tick')
        return key


class RenderCache(unittest.TestCase):
    def play(self, screen, fetcher, page='activity', params=None):
        with patch.object(tui.curses, 'curs_set'), patch.object(tui.time, 'monotonic', return_value=0), \
             patch.object(tui, 'render', wraps=tui.render) as render:
            tui._screen(screen, fetcher, {'profiles': [{'name': 'astra'}]}, page, params or {})
        return render

    def test_long_line_is_sanitized_once_per_block_not_per_wrapped_row(self):
        data = {'items': [event('Пример e\u0301 你好 ' * 500)]}
        with patch.object(tui, 'terminal_text', wraps=tui.terminal_text) as clean:
            lines = tui.render('activity', data, width=20)
        self.assertGreater(len(lines), 100)
        self.assertLess(clean.call_count, 20)

    def test_wrapping_matches_clipping_contract_for_unicode_and_controls(self):
        for width in (1, 2, 3, 17, 99):
            for text in ('', 'Русский текст', 'e\u0301你好🙂' * 8, 'a\t你好\nnext',
                         '\x1b[31mредактор\x1b[0m', '\u202eabc\x00', '\u0301\u0301abc'):
                with self.subTest(width=width, text=text):
                    data = {'items': [event(text)]}
                    # A wide render supplies sanitized logical lines. Rewrap with
                    # the public clipping contract used by the previous renderer.
                    logical = tui.render('activity', data, width=10000)
                    expected = []
                    for line in logical:
                        if not line:
                            expected.append('')
                        while line:
                            part = tui.clip(line, max(2, width))
                            expected.append(part)
                            line = line[len(part):]
                    self.assertEqual(tui.render('activity', data, width=width), expected)

    def test_twenty_idle_ticks_format_once(self):
        screen = Screen([None] * 20 + ['q'])
        fetcher = Fetcher({'items': [event()]})
        render = self.play(screen, fetcher)
        self.assertEqual(render.call_count, 1)
        self.assertEqual(len(screen.frames), 21)
        self.assertEqual(len(fetcher.requests), 1)

    def test_scroll_pause_reuses_formatted_lines(self):
        screen = Screen([curses.KEY_DOWN, None, curses.KEY_UP, None, 'q'])
        render = self.play(screen, Fetcher({'items': [event('line\n' * 12)]}))
        self.assertEqual(render.call_count, 1)
        self.assertIn('ПАУЗА ЭКРАНА', str(screen.frames))

    def test_selected_event_invalidates_cache(self):
        screen = Screen(['j', None, None, 'q'])
        render = self.play(screen, Fetcher({'items': [event('FIRST'), event('SECOND', 'b' * 64)]}))
        self.assertEqual(render.call_count, 2)
        self.assertEqual([call.kwargs['selected'] for call in render.call_args_list], [0, 1])
        self.assertIn('Выбрано событие 2/2', str(screen.frames[-1]))

    def test_width_change_reflows_once(self):
        screen = Screen([])
        def resize():
            screen.width = 40
            return curses.KEY_RESIZE
        screen.keys = iter([None, resize, None, None, 'q'])
        render = self.play(screen, Fetcher({'items': [event('word ' * 80)]}))
        self.assertEqual(render.call_count, 2)
        self.assertEqual([call.args[2] for call in render.call_args_list], [99, 39])

    def test_new_response_is_rendered_even_when_equal_by_value(self):
        data = {'items': [event()]}
        render = self.play(Screen(['r', None, None, 'q']), Fetcher(data, data))
        self.assertEqual(render.call_count, 2)
        self.assertIsNot(render.call_args_list[0].args[1], render.call_args_list[1].args[1])

    def test_changed_response_is_visible_without_stale_cache(self):
        screen = Screen(['r', None, 'q'])
        render = self.play(screen, Fetcher({'items': [event('OLD')]}, {'items': [event('NEW')]}))
        self.assertEqual(render.call_count, 2)
        self.assertIn('OLD', str(screen.frames[0]))
        self.assertIn('NEW', str(screen.frames[-1]))
        self.assertNotIn('OLD', str(screen.frames[-1]))

    def test_help_overlay_does_not_discard_cached_data(self):
        screen = Screen(['?', None, None, '?', None, 'q'])
        render = self.play(screen, Fetcher({'items': [event('RETAINED')]}))
        self.assertEqual(render.call_count, 1)
        self.assertIn('Управление monik', str(screen.frames))
        self.assertIn('RETAINED', str(screen.frames[-1]))

    def test_fixed_time_idle_keeps_one_format_and_one_request(self):
        screen = Screen([None] * 5 + ['q'])
        fetcher = Fetcher({'items': [event()]})
        render = self.play(screen, fetcher, params={'at': '2026-09-10T12:00:00Z'})
        self.assertEqual(render.call_count, 1)
        self.assertEqual(len(fetcher.requests), 1)


if __name__ == '__main__':
    unittest.main()
