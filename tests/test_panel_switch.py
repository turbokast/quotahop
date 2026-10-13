"""Keyboard confirmations and selection binding use only fake accounts and auth."""
import datetime as dt
from unittest.mock import MagicMock, patch
import unittest

import claude_usage as app
import credential_state
import live_usage as live
import panel_switch
import usage_panel as panel
from test_panel import fixture, NOW


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.source = MagicMock()
        self.source.locked = False
        self.entries = {r['id']: {'access_token': 'FAKE-TOKEN-' + r['id']} for r in self.data['accounts']}
        self.source.load.return_value = (self.data['accounts'], self.entries)
        self.current = credential_state.fingerprint(self.entries['max-1'], 'claude')
        self.context = patch.object(credential_state, 'current', return_value={'claude': self.current})
        self.context.start()
        self.addCleanup(self.context.stop)

    def test_preview_is_read_only_and_pins_an_alternative(self):
        with patch.object(panel_switch.claude_switch, 'switch', side_effect=AssertionError('write')), \
             patch('socket.socket', side_effect=AssertionError('network')):
            result = panel_switch.prepare(self.source, self.data, 'claude', NOW)
        self.assertNotEqual(result['target']['id'], 'max-1')
        self.assertEqual(result['current'], self.current)
        self.assertEqual(result['target_state'], credential_state.fingerprint(self.entries[result['target']['id']], 'claude'))
        rendered = panel_switch.confirmation_lines(result, 28)
        self.assertIn('Enter confirm', str(rendered))
        self.assertNotIn('FAKE-TOKEN', str(rendered))

    def test_unsupported_stale_and_changed_login_do_not_switch(self):
        for provider in ('codex', 'muse_code'):
            with self.assertRaises(live.LiveError):
                panel_switch.prepare(self.source, self.data, provider, NOW)
        self.source.load.assert_not_called()
        with self.assertRaises(live.LiveError):
            panel_switch.prepare(self.source, self.data, 'claude', NOW + dt.timedelta(minutes=3))
        with patch.object(credential_state, 'current', return_value={'claude': (b'changed', None)}), self.assertRaises(live.LiveError):
            panel_switch.prepare(self.source, self.data, 'claude', NOW)

    def test_expired_or_locked_confirmation_never_writes(self):
        confirmation = panel_switch.prepare(self.source, self.data, 'claude', NOW)
        with patch.object(app, 'Store', side_effect=AssertionError('write')):
            with self.assertRaises(live.LiveError):
                panel_switch.execute(self.source, confirmation, NOW + dt.timedelta(seconds=61))
            self.source.locked = True
            with self.assertRaises(live.LiveError):
                panel_switch.execute(self.source, confirmation, NOW)

    def screen(self, keys):
        screen = MagicMock()
        screen.getmaxyx.return_value = (43, 30)
        screen.getch.side_effect = keys
        return screen

    def drive(self, keys, times):
        confirmation = {'target': self.data['accounts'][1]}
        with patch.object(panel.curses, 'curs_set'), patch.object(panel.curses, 'flushinp'), \
             patch.object(panel, 'Fetch') as fetch, \
             patch.object(panel.time, 'monotonic', side_effect=times), \
             patch.object(panel_switch, 'prepare', return_value=confirmation) as prepare, \
             patch.object(panel_switch, 'execute', return_value={'changed': True}) as execute:
            fetch.return_value.poll.return_value = self.data
            panel._screen(self.screen(keys), self.source, 60, 4, 'recommended', False, False)
        return prepare, execute

    def test_escape_cancels_confirmation_without_closing_or_switching(self):
        prepare, execute = self.drive([-1, 10, 27, ord('q')], [10, 11, 12, 13])
        prepare.assert_called_once()
        execute.assert_not_called()

    def test_enter_requires_second_deliberate_press_and_pins_provider(self):
        prepare, execute = self.drive([-1, 10, 10, 10, ord('q')], [10, 11, 11.1, 12, 12.5, 13])
        self.assertEqual(prepare.call_args.args[2], 'claude')
        execute.assert_called_once()

    def test_arrow_selects_provider_without_switching(self):
        prepare, execute = self.drive([-1, panel.curses.KEY_DOWN, 10, 27, ord('q')], [10, 11, 12, 13, 14])
        self.assertEqual(prepare.call_args.args[2], 'muse_code')
        execute.assert_not_called()
