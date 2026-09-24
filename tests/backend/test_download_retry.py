"""RED/GREEN: every large asset download must survive a dropped connection.

Live evidence (Windows Sandbox run #4, 2026-09-18, package candidate-dist6):
    {"stage":"git","message":"Git: ошибка. ...","index":2}
    {"message":"...этап git, код 1..."}
The desktop stage was already hardened for a blocked Electron payload, but the FIRST stage
died first: PortableGit (~57 MB) is fetched from github.com with a single bare
`Invoke-WebRequest`. Measured throughput on this link: ~520 KB/s with frequent resets, so a
single dropped connection ends the install before anything else can be tested.

Node (~30 MB) and the repo ZIP have the same single-shot pattern.
"""
import unittest
from pathlib import Path

INSTALL_PS1 = Path(__file__).resolve().parents[2] / 'backend' / 'upstream' / 'install.ps1'
WORKER_PS1 = Path(__file__).resolve().parents[2] / 'backend' / 'worker.ps1'


class DownloadRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INSTALL_PS1.read_text(encoding='utf-8-sig')

    def test_retry_helper_exists_with_backoff(self):
        self.assertIn('function Invoke-DownloadWithRetry', self.src)
        i = self.src.index('function Invoke-DownloadWithRetry')
        body = self.src[i:i + 1600]
        self.assertIn('Start-Sleep', body, 'the retry must back off between attempts')
        self.assertIn('$Attempts', body, 'attempt count must be bounded')
        self.assertIn('catch', body, 'a failed attempt must be caught, not fatal on the first try')

    def test_retry_helper_does_not_echo_raw_download_bodies(self):
        """Logs must never carry response bodies: they can include tokens or private URLs."""
        i = self.src.index('function Invoke-DownloadWithRetry')
        body = self.src[i:i + 1600]
        self.assertNotIn('$_.Exception.Response', body)
        self.assertNotIn('GetResponseStream', body)

    def test_portable_git_download_is_retried(self):
        i = self.src.index('$downloadUrl = "https://github.com/git-for-windows/git/releases/download/')
        block = self.src[i:i + 900]
        # Routed through the multi-source helper, which retries each source via
        # Invoke-DownloadWithRetry (also covers npmmirror/huaweicloud mirrors).
        self.assertIn('Invoke-DownloadFromSources', block,
                      'PortableGit (~57MB) is the first large download of the run; one dropped '
                      'connection killed the live install at the git stage')
        self.assertIn('throw', block,
                      'after the retries are exhausted the stage must fail loudly, not continue '
                      'with a truncated archive')

    def test_node_download_is_retried(self):
        i = self.src.index('$indexUrl = "https://nodejs.org/dist/latest-v')
        block = self.src[i:i + 1400]
        self.assertIn('Invoke-DownloadWithRetry', block,
                      'the Node.js index and zip must also retry -- same single-shot pattern')

    def test_repo_archive_download_is_retried(self):
        i = self.src.index('$zipPath = "$env:TEMP\\hermes-agent-$zipLabel.zip"')
        block = self.src[i:i + 900]
        # Retried per source (direct GitHub then archive proxies).
        self.assertIn('Invoke-DownloadFromSources', block,
                      'the fallback repo ZIP download must retry as well')

    def test_no_bare_large_download_remains(self):
        """Any bare Invoke-WebRequest -OutFile would reintroduce the single-shot bug.

        The match is order-insensitive: a flag placed before -Uri (as in worker.ps1's VC
        redist) must not evade it. Two calls are legitimate and are skipped explicitly:
          - the retry helper's own inner request (install.ps1), and
          - the Microsoft-signed VC++ redist in worker.ps1, which is retried and
            Authenticode-verified before it runs.
        """
        src = INSTALL_PS1.read_text(encoding='utf-8-sig')
        lines = src.splitlines()
        helper_start = next(i for i, l in enumerate(lines, 1)
                            if l.startswith('function Invoke-DownloadWithRetry'))
        helper_end = next(i for i in range(helper_start, len(lines) + 1) if lines[i - 1] == '}')

        offenders = []
        for path in (INSTALL_PS1, WORKER_PS1):
            for n, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
                s = line.strip()
                if not (s.startswith('Invoke-WebRequest') and '-OutFile' in s and '-Uri' in s):
                    continue
                if path is INSTALL_PS1 and helper_start <= n <= helper_end:
                    continue
                if 'VcRedistUrl' in s:
                    continue
                offenders.append(f'{path.name}:{n}: {s}')

        self.assertEqual(offenders, [],
                         f'bare single-shot downloads remain: {offenders}; route them through '
                         f'a retry helper')
        # The helper's inner call must still be the one that actually retries.
        self.assertIn('Invoke-WebRequest -Uri $Uri -OutFile $OutFile', src,
                      'the retry helper must contain the single inner request')


if __name__ == '__main__':
    unittest.main(verbosity=2)
