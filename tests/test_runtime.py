import os
import unittest
from unittest.mock import patch

from ai_watch.runtime import external_command_env


class RuntimeTests(unittest.TestCase):
    def test_source_execution_preserves_library_environment(self):
        original = {"LD_LIBRARY_PATH": "/system/libs", "LD_LIBRARY_PATH_ORIG": "/another", "PATH": "/bin"}
        with patch.dict(os.environ, original, clear=True):
            self.assertEqual(external_command_env(), original)

    def test_frozen_execution_restores_original_library_path(self):
        frozen = {"LD_LIBRARY_PATH": "/tmp/_MEI123:/system/libs", "LD_LIBRARY_PATH_ORIG": "/system/libs", "PATH": "/bin"}
        with patch.dict(os.environ, frozen, clear=True), patch("sys.frozen", True, create=True):
            self.assertEqual(external_command_env(), {"LD_LIBRARY_PATH": "/system/libs", "PATH": "/bin"})
            self.assertEqual(dict(os.environ), frozen)

    def test_frozen_execution_removes_path_when_original_was_unset(self):
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI123"}, clear=True), patch("sys.frozen", True, create=True):
            self.assertNotIn("LD_LIBRARY_PATH", external_command_env())


if __name__ == "__main__":
    unittest.main()
