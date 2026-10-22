"""Help is readable and available without a configured vault or provider access."""
import contextlib
import io
import re
import unittest
from unittest.mock import patch

import claude_usage as app


class HelpTests(unittest.TestCase):
    def test_options_are_alphabetical_with_aliases_and_no_side_effects(self):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for target in ('claude_usage.Store', 'live_usage.passphrase', 'socket.socket', 'subprocess.Popen'):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            stack.enter_context(contextlib.redirect_stdout(out))
            with self.assertRaises(SystemExit) as caught:
                app.main(['--help'])
        self.assertEqual(caught.exception.code, 0)
        text = out.getvalue()
        options = text.split('options:\n', 1)[1].split('\nAccount selectors:', 1)[0]
        names = [re.search(r'--[a-z-]+', line).group() for line in options.splitlines()
                 if re.match(r'^  -(?:-|[a-z],)', line)]
        self.assertGreater(len(names), 20)
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), len(set(names)))
        self.assertIn('--change-password, --reset-password', text)
        self.assertIn('-h, --help', text)
        for phrase in ('[ACTION] [OPTIONS]', 'Examples:', 'Exit codes:', 'no device-token renewal',
                       'not a read-only API contract', 'Readiness is not renewal status', 'side the checkout'):
            self.assertIn(phrase, text)

    def test_installed_and_custom_names_work_in_examples(self):
        for program in ('quotahop', 'qh'):
            out = io.StringIO()
            with patch('sys.argv', [program]), contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                app.main(['--help'])
            self.assertIn(f'usage: {program} [ACTION] [OPTIONS]', out.getvalue())
            self.assertIn(f'  {program} --switch-next --dry-run', out.getvalue())
