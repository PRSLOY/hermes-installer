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

    def test_node_download_is_retried_and_pinned(self):
        # issue #15: Node.js is now pinned to an exact build from the release manifest
        # instead of scraping the "latest" index, so it must be hash-checked too.
        i = self.src.index("Get-ArtifactEntry 'node'")
        block = self.src[i:i + 1600]
        self.assertIn('Invoke-DownloadFromSources', block,
                      'the pinned Node.js zip must still retry across sources')
        self.assertIn("Assert-ArtifactHash -Id 'node'", block,
                      'the pinned Node.js zip must be verified against the manifest')

    def test_repo_archive_download_is_retried(self):
        i = self.src.index('$zipPath = "$env:TEMP\\hermes-agent-$zipLabel.zip"')
        block = self.src[i:i + 900]
        # Retried per source (direct GitHub then archive proxies).
        self.assertIn('Invoke-DownloadFromSources', block,
                      'the fallback repo ZIP download must retry as well')

    def test_no_bare_large_download_remains(self):
        """Any remaining bare Invoke-WebRequest -OutFile would reintroduce the single-shot bug.

        The retry helper's own inner call is the one legitimate occurrence, so the check
        ignores everything inside `function Invoke-DownloadWithRetry { ... }`.
        """
        start = self.src.index('function Invoke-DownloadWithRetry')
        depth = 0
        end = len(self.src)
        for i in range(start, len(self.src)):
            ch = self.src[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        helper = self.src[start:end]

        offenders = []
        for n, line in enumerate(self.src.splitlines(), 1):
            s = line.strip()
            if s.startswith('Invoke-WebRequest -Uri') and '-OutFile' in s:
                offenders.append((n, s))
        # Only the helper's internal call may remain.
        self.assertEqual(
            len(offenders), 1,
            f'bare single-shot downloads remain at {offenders}; route them through '
            f'Invoke-DownloadWithRetry')
        self.assertIn('Invoke-WebRequest -Uri $Uri -OutFile $OutFile', helper,
                      'the single remaining bare download must be the retry helper itself')


if __name__ == '__main__':
    unittest.main(verbosity=2)
