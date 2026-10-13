import importlib.util
import json
import pathlib
import subprocess
import unittest
from unittest.mock import patch

path = pathlib.Path(__file__).parents[1] / 'scripts' / 'plazmi-status.py'
spec = importlib.util.spec_from_file_location('status', path)
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class StatusTests(unittest.TestCase):
    def test_literal_argv_and_success_payload(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({'notable': 0, 'letters': 1}), '')
        with patch.object(status.subprocess, 'run', return_value=completed) as run:
            code, value = status.read_status('pulse', '/tmp/plazmi cli')
        self.assertEqual(0, code)
        self.assertEqual(1, value['letters'])
        self.assertEqual(['/tmp/plazmi cli', '--json', 'pulse'], run.call_args.args[0])
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_domain_rejection_is_not_success(self):
        completed = subprocess.CompletedProcess([], 0, '{"ok": false, "text": "not allowed"}', '')
        with patch.object(status.subprocess, 'run', return_value=completed):
            code, value = status.read_status('digest')
        self.assertEqual(1, code)
        self.assertFalse(value['ok'])

    def test_failure_does_not_relay_stderr_or_stdout(self):
        completed = subprocess.CompletedProcess([], 2, 'private output', 'private diagnostics')
        with patch.object(status.subprocess, 'run', return_value=completed):
            code, value = status.read_status()
        self.assertEqual(2, code)
        self.assertNotIn('private', json.dumps(value))

    def test_game_actions_rejected_before_spawn(self):
        with patch.object(status.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                status.read_status('do')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
