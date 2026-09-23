"""RED/GREEN: the desktop workspace install must verify the get-windows optional
native binding actually landed, and retry that single package when it did not.

Root cause seen live in Windows Sandbox (2026-09-17 22:42): npm's postinstall for
get-windows@9.3.0 (node-pre-gyp download of the win32-x64 prebuilt) failed with a
network ECONNRESET. npm treats a failed *optional* dependency as non-fatal, dropped
node_modules/get-windows, and still exited 0. The desktop build therefore died much
later inside stage-native-deps.mjs with "get-windows is not installed; cannot stage
its win32-x64 native payload" -- far from the real cause.

The check is string-level on the vendored installer, like the sibling pin-fetch test.
"""
import re
import unittest
from pathlib import Path

INSTALL_PS1 = Path(__file__).resolve().parents[2] / "backend" / "upstream" / "install.ps1"


class GetWindowsBindingGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INSTALL_PS1.read_text(encoding="utf-8-sig")
        cls.lines = cls.text.splitlines()

    def test_verifies_get_windows_binding_after_workspace_install(self):
        self.assertIn(
            "node_modules\\get-windows",
            self.text,
            "installer does not look for the get-windows package after npm ci",
        )
        self.assertRegex(
            self.text,
            r"lib[\\/]binding",
            "installer does not check the get-windows lib/binding payload",
        )

    def test_retries_the_optional_dependency_when_binding_missing(self):
        match = re.search(
            r"& \$npmExe install get-windows@[0-9][^\r\n]*",
            self.text,
        )
        self.assertIsNotNone(
            match,
            "installer never retries the get-windows optional dependency by name",
        )
        self.assertIn("--include=optional", match.group(0))

    def test_reports_missing_binding_without_leaking_secrets(self):
        # The guard may only mention the package by name and the numeric exit code.
        guard = "\n".join(
            line for line in self.lines if "get-windows" in line
        )
        for banned in ("api_key", "apiKey", "Authorization", "Bearer ", "token="):
            self.assertNotIn(banned, guard, f"guard would leak {banned}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
