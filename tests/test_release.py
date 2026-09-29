import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import release


class ReleaseTests(unittest.TestCase):
    def test_release_requires_matching_tag_source_and_package_versions(self):
        with patch.object(release, "__version__", "1.2.3"), \
             patch("importlib.metadata.version", return_value="1.2.3"):
            self.assertEqual(release.release_tag("v1.2.3"), "v1.2.3")
            for tag in ("v1.2.4", "v1.2.3-rc1", "1.2.3"):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    release.release_tag(tag)
        with patch.object(release, "__version__", "1.2.3"), \
             patch("importlib.metadata.version", return_value="1.2.2"):
            with self.assertRaisesRegex(ValueError, "versions differ"):
                release.release_tag("v1.2.3")

    def test_checksums_require_complete_set_and_detect_modified_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "four release targets"):
                release.checksums(root, "v1.2.3")
            for target in release.TARGETS:
                archive = root / f"ai-watch-v1.2.3-{target}.tar.gz"
                archive.write_bytes(target.encode())
                archive.with_name(archive.name + ".sha256").write_text(
                    f"{release.sha256(archive)}  {archive.name}\n")
            with contextlib.redirect_stdout(io.StringIO()):
                release.checksums(root, "v1.2.3")
            self.assertEqual(len((root / "SHA256SUMS").read_text().splitlines()), 4)
            archive.write_bytes(b"modified archive")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                release.checksums(root, "v1.2.3")


if __name__ == "__main__":
    unittest.main()
