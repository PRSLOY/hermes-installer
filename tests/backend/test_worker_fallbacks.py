"""worker.ps1 backup-provider plumbing (issue #10), offline.

worker.ps1 is dot-sourced (its Main never runs: the same guard check_helpers.ps1
relies on), so no install request ever reaches the host. Keys are fakes.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / 'backend' / 'worker.ps1'
PS = os.path.join(os.environ['SYSTEMROOT'], 'System32/WindowsPowerShell/v1.0/powershell.exe')
KEY = 'fake-backup-key-' + '0123456789'
PRIMARY = 'https://gwarden.example/v1'

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
. '%s'
$raw = [Console]::In.ReadToEnd()
try {
    $entries = @(Get-FallbackEntries ($raw | ConvertFrom-Json))
    $out = @($entries | ForEach-Object { @{id=$_.provider_id; endpoint=$_.endpoint; model=$_.model; key_ok=($_.api_key -ceq '%s')} })
    [Console]::Out.WriteLine((@{ok=$true; entries=[object[]]$out} | ConvertTo-Json -Compress -Depth 5))
} catch { [Console]::Out.WriteLine('{"ok":false}') }
"""


def fb(provider_id='dahl', endpoint='https://dahl.example/v1', key=KEY, model='m-1'):
    return {'provider_id': provider_id, 'endpoint': endpoint, 'model': model, 'api_key': key}


class WorkerFallbackTests(unittest.TestCase):
    def check(self, fallbacks, **extra):
        payload = {'protocol': 1, 'action': 'install', 'endpoint': PRIMARY, 'api_key': 'primary-fake-key-1'}
        if fallbacks is not ...:
            payload['fallbacks'] = fallbacks
        payload.update(extra)
        script = SCRIPT % (str(WORKER).replace("'", "''"), KEY)
        run = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
                             input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(run.stderr, '')
        self.assertNotIn(KEY, run.stdout, 'a key was echoed')
        return json.loads(run.stdout.strip().splitlines()[-1])

    def test_absent_or_null_or_empty_means_no_backups(self):
        for value in (..., None, []):
            self.assertEqual(self.check(value), {'ok': True, 'entries': []})

    def test_valid_entries_pass_through(self):
        result = self.check([fb(), fb('atria', 'https://api.atria.example/v1', model=None)])
        self.assertTrue(result['ok'])
        self.assertEqual(result['entries'], [
            {'id': 'dahl', 'endpoint': 'https://dahl.example/v1', 'model': 'm-1', 'key_ok': True},
            {'id': 'atria', 'endpoint': 'https://api.atria.example/v1', 'model': '', 'key_ok': True}])

    def test_single_entry_stays_a_list(self):
        # PS 5.1 unrolls one-element arrays in some constructs; the product must not.
        result = self.check([fb()])
        self.assertEqual(result['entries'], [{'id': 'dahl', 'endpoint': 'https://dahl.example/v1', 'model': 'm-1', 'key_ok': True}])

    def test_invalid_entries_are_rejected(self):
        bad = [
            'not-a-list',
            [fb(), fb('b', 'https://b.example/v1'), fb('c', 'https://c.example/v1')],   # more than two
            [fb(), fb('dahl', 'https://other.example/v1')],                             # same provider twice
            [fb(), fb('other', 'https://DAHL.example/v1/')],                            # same endpoint twice
            [fb('p', PRIMARY + '/')],                                                    # the primary itself
            [fb(endpoint='http://dahl.example/v1')],
            [fb(endpoint='https://user:pw@dahl.example/v1')],
            [fb(endpoint='https://dahl.example/v1?x=1')],
            [fb(endpoint='dahl.example/v1')],
            [fb(key='short')],
            [fb(key='has space-' + KEY)],
            [fb(key='кириллица-' + KEY)],
            [fb(provider_id='bad id')],
            [fb(model='x' * 257)],
            [fb(model='a\nb')],
            [dict(fb(), extra=1)],
            ['string-entry'],
        ]
        for value in bad:
            self.assertEqual(self.check(value), {'ok': False}, value)

    def test_configure_never_receives_fallbacks(self):
        text = WORKER.read_text(encoding='utf-8-sig')
        strip = text.index("$inputData.PSObject.Properties.Remove('fallbacks')")
        configure = text.index("'configure.py'")
        self.assertLess(strip, configure, 'fallbacks must be stripped before configure.py runs')
        step = text.index("'fallbacks.py'")
        self.assertGreater(step, text.index("'--marketplaces'"), 'backups run after the primary and the set')
        self.assertIn("$script:ChildLabel = 'Запасные провайдеры'", text)
        self.assertIn('"Запасные провайдеры: подключено $fbDone из $fbTotal."', text)
        # The step never calls Fail: a backup failure is never terminal.
        block = text[text.index('if ($fallbackEntries.Count -gt 0)'):text.index('$successEvent =')]
        self.assertIsNone(re.search(r'\bFail\s', block))

    def test_ps1_keeps_utf8_bom(self):
        self.assertEqual(WORKER.read_bytes()[:3], b'\xef\xbb\xbf')


if __name__ == '__main__':
    unittest.main(verbosity=2)
