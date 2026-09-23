"""RED/GREEN: the Electron payload must be seeded into the shared cache BEFORE npm ci.

Live evidence (Windows Sandbox, 2026-09-18, runs #5/#6):
    info run electron@40.10.2 postinstall apps/desktop/node_modules/electron node install.js
    info run electron@40.10.2 postinstall { code: 1 }
    error RequestError: connect ETIMEDOUT 140.82.121.3:443
@electron/get runs inside npm ci's postinstall, so retrying only the outer npm command re-ran the
same failing download. A fresh `npm install electron@40.10.2` with the mirror finished in 15s, so
seeding that payload first turns the postinstall into a local cache hit.
"""
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / 'backend' / 'upstream' / 'install.ps1').read_text(
    encoding='utf-8-sig')


class ElectronPayloadSeedTests(unittest.TestCase):
    def _seed_fn(self):
        """The whole Initialize-ElectronPayloadCache body (it grew: now a mirror loop)."""
        i = SRC.index('function Initialize-ElectronPayloadCache')
        j = SRC.index('\nfunction Get-ElectronDir', i)
        return SRC[i:j]

    def test_seed_helper_exists(self):
        self.assertIn('function Initialize-ElectronPayloadCache', SRC)

    def test_seed_uses_the_documented_cache_env(self):
        """@electron/get reads electron_config_cache; seeding any other dir would not be a hit."""
        i = SRC.index('function Initialize-ElectronPayloadCache')
        block = SRC[i:i + 3000]
        self.assertIn('electron_config_cache', block)

    def test_seed_uses_a_mirror_not_github(self):
        i = SRC.index('function Initialize-ElectronPayloadCache')
        block = SRC[i:i + 3000]
        self.assertIn('Set-ElectronMirrorEnv', block,
                      'the seed must go through the mirror, otherwise it hits the same blocked host')

    def test_seed_runs_before_the_first_npm_ci(self):
        """This is the whole point: postinstall inside npm ci must find a warm cache."""
        seed = SRC.index('Initialize-ElectronPayloadCache -NpmExe')
        first = SRC.index('& $npmExe ci --include=optional')
        self.assertLess(seed, first,
                        'seeding after npm ci cannot help the postinstall that already failed')

    def test_seed_reads_the_declared_electron_spec(self):
        """Hardcoding a version would silently stop seeding after an upstream bump."""
        i = SRC.index('function Initialize-ElectronPayloadCache')
        block = SRC[i:i + 3000]
        self.assertIn('devDependencies', block)
        self.assertIn('apps\\desktop', block)

    def test_seed_restores_the_cache_env(self):
        """Leaking electron_config_cache into later stages would change unrelated behaviour."""
        block = self._seed_fn()
        self.assertIn('$env:electron_config_cache = $prevCache', block)

    def test_seed_tries_every_mirror_not_just_the_first(self):
        """A mirror that stalls must not end the attempt while another could serve it."""
        block = self._seed_fn()
        self.assertIn('foreach ($cand in $script:DesktopElectronFallbackMirrors)', block,
                      'the pre-seed must iterate all mirrors')
        self.assertIn('if ($seeded) { break }', block,
                      'iteration must stop as soon as one mirror succeeds')

    def test_seed_failure_is_not_fatal(self):
        """A seed failure must fall through to the previous behaviour, never abort the stage."""
        seed = SRC.index('Initialize-ElectronPayloadCache -NpmExe')
        tail = SRC[seed:seed + 700]
        self.assertIn('else', tail,
                      'a failed seed must be tolerated, not treated as an install failure')


    def test_cache_dir_is_outside_the_hermes_home(self):
        """Regression: seeding %LOCALAPPDATA%\hermes\electron-cache created a FOREIGN
        directory inside the install home before the worker started, so the checkpoint
        refused the run ("Checkpoint ... принадлежит другой установке"). The cache must
        live in the standard @electron/get root, which npm ci's postinstall reads anyway.
        """
        i = SRC.index("$electronCacheDir =")
        line = SRC[i:SRC.index('\n', i)]
        self.assertIn("'electron\\Cache'", line)
        self.assertNotIn("'hermes\\electron-cache'", line)


if __name__ == '__main__':
    unittest.main()

if __name__ == '__main__':
    unittest.main()
