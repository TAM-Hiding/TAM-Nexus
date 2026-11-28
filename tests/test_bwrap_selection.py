import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from nexus import sandbox


class LauncherSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bwrap"
        shutil.copyfile("/usr/bin/true", self.path)
        self.path.chmod(0o500)
        self.pin = hashlib.sha256(self.path.read_bytes()).hexdigest()
        env = patch.dict(os.environ, NEXUS_BWRAP=str(self.path), NEXUS_BWRAP_SHA256=self.pin)
        env.start()
        self.addCleanup(env.stop)

    def test_replacement_cannot_change_verified_image(self):
        with sandbox.bubblewrap() as (image, fds):
            self.path.unlink()
            self.path.write_bytes(b"replacement")
            self.assertEqual(hashlib.sha256(Path(image).read_bytes()).hexdigest(), self.pin)
            self.assertIn("--disable-userns", sandbox.missing_flags(image, fds))

    def test_unsafe_paths_and_identity_fail_closed(self):
        alias = self.path.with_name("link")
        alias.symlink_to(self.path)
        for path, pin in [(alias, self.pin), ("relative", self.pin),
                          (self.path.parent / ".." / self.path.parent.name / "bwrap", self.pin),
                          (self.path, "0" * 64), (self.path, "")]:
            with self.subTest(path=str(path)), patch.dict(os.environ, NEXUS_BWRAP=str(path),
                                                         NEXUS_BWRAP_SHA256=pin):
                with self.assertRaises(sandbox.SandboxUnavailable), sandbox.bubblewrap():
                    self.fail("unsafe launcher accepted")
        self.path.chmod(0o777)
        with self.assertRaises(sandbox.SandboxUnavailable), sandbox.bubblewrap():
            self.fail("writable launcher accepted")
