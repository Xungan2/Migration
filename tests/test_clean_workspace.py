import fcntl
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'clean_workspace', Path(__file__).resolve().parents[1] / 'scripts/clean-workspace.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTest(unittest.TestCase):
    def test_preview_lock_and_cleanup_preserve_intent(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cleanup.shutil, 'which', return_value=None):
            root = Path(directory)
            workspace = root / 'migrations/spi-nor'
            workspace.mkdir(parents=True)
            intent = root / 'intend/spi-nor-intent.md'
            intent.parent.mkdir()
            intent.write_text('keep this intent')
            cleanup.clean(root)
            self.assertTrue(workspace.exists())
            with (workspace / '.porter.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self.assertRaisesRegex(ValueError, 'Stop the running Porter'):
                    cleanup.clean(root, apply=True)
                self.assertTrue(workspace.exists())
            cleanup.clean(root, apply=True)
            self.assertFalse(workspace.exists())
            self.assertEqual(intent.read_text(), 'keep this intent')
            with patch.object(cleanup.shutil, 'which', return_value='/usr/bin/docker'), \
                    patch.object(cleanup.subprocess, 'check_output', return_value=(
                        'porter-test\tasterinas/dev:test\n'
                        'ollama-qwen3\tollama/ollama:latest\n'
                        'porter-unrelated\tother/image:latest\n')), \
                    patch.object(cleanup.subprocess, 'run') as run:
                cleanup.clean(root, apply=True)
                run.assert_called_once_with(
                    ['docker', 'rm', '-f', '-v', 'porter-test'], check=True)
