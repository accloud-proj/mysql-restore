import contextlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mysql_restore import backup, cli, config, cron
from scripts.package import release_version


class PackagingTests(unittest.TestCase):
    def test_calendar_versions_and_tags(self):
        for value in ["2026.01", "2026.11", "2026.11.0", "2026.11.12"]:
            self.assertEqual(release_version(value), value)
        for value in ["v2026.11", "2026.1", "2026.13", "2026.00", "2026.11.01",
                      "2026.11.-1", "2026.11.1.2", "2026.11\n", "$(echo bad)"]:
            with self.assertRaises(ValueError):
                release_version(value)

    def test_cli_version(self):
        with patch("sys.argv", ["mysql-restore", "--version"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit:
                cli.main()
        self.assertEqual(exit.exception.code, 0)
        self.assertTrue(output.getvalue().startswith("mysql-restore "))

    def test_frozen_cli_version(self):
        with tempfile.TemporaryDirectory() as name:
            (Path(name) / "version.txt").write_text("2026.11.2\n")
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "_MEIPASS", name, create=True), \
                    patch("sys.argv", ["mysql-restore", "--version"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit):
                    cli.main()
            self.assertEqual(output.getvalue(), "mysql-restore 2026.11.2\n")

    def test_lock_blocks_another_process_and_releases(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            code = (
                "from pathlib import Path\n"
                "from mysql_restore.backup import backup_lock\n"
                "with backup_lock(Path(__import__('sys').argv[1])): pass\n"
            )
            with backup.backup_lock(directory):
                result = subprocess.run([sys.executable, "-c", code, name],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Another backup job", result.stderr)
            result = subprocess.run([sys.executable, "-c", code, name],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(os.name == "nt", "Windows uses Task Scheduler")
    def test_frozen_cron_runs_binary_without_extracted_python_module(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            loaded = config.parse_config(root / "config.json", {
                "format": 1, "host": "127.0.0.1", "port": 3306, "user": "backup",
                "defaults_file": "client.cnf", "backup_dir": "backups",
                "schedule": "0 2 * * *", "log_file": "backup.log",
            })
            binary = root / "mysql restore"
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(binary)):
                command = cron.backup_command(loaded)
            self.assertIn(shlex.quote(str(binary)) + " --config", command)
            self.assertNotIn("-m mysql_restore", command)
            self.assertIn("cd " + shlex.quote(str(root)), command)

    def test_windows_cron_fails_explicitly(self):
        with patch.object(cron.os, "name", "nt"):
            with self.assertRaisesRegex(backup.BackupError, "Task Scheduler"):
                cron.backup_command(None)
            with self.assertRaisesRegex(backup.BackupError, "Task Scheduler"):
                cron.render(None)


if __name__ == "__main__":
    unittest.main()
