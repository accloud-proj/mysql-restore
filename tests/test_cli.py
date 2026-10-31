import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mysql_restore import backup, cli, config, cron


class ConfigurationCronTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "mysql-restore.json"
        self.data = {
            "format": 1, "host": "127.0.0.1", "port": 3306, "user": "backup",
            "defaults_file": "mysql-restore.client.cnf", "mysql_version": "8.4.7",
            "role": "primary", "backup_dir": "backups", "binlog_dir": "data",
            "binlog_index": "data/mysql-bin.index", "schedule": "0 2 * * *",
            "log_file": "backup.log",
        }
        backup.atomic_json(self.path, self.data)

    def test_relative_paths_are_relative_to_config_not_working_directory(self):
        loaded = config.load_config(self.path)
        self.assertEqual(loaded.backup_dir, self.root / "backups")
        self.assertEqual(loaded.binlog_dir, self.root / "data")
        self.assertEqual(loaded.defaults_file, self.root / "mysql-restore.client.cnf")

    def test_config_rejects_unknown_keys_invalid_port_and_nested_paths(self):
        for data in [
            dict(self.data, typo="oops"), dict(self.data, port=True), dict(self.data, port=0),
            dict(self.data, role="slave"), dict(self.data, binlog_dir="backups/data"),
            dict(self.data, mysql_version="latest"),
        ]:
            with self.assertRaises(backup.BackupError):
                config.parse_config(self.path, data)
        self.path.chmod(0o666)
        with self.assertRaisesRegex(backup.BackupError, "writable"):
            config.load_config(self.path)

    def test_schedule_accepts_normal_numeric_cron_and_rejects_injection(self):
        for value in ["0 2 * * *", "*/5 1-3 * * 1,2,3", "0 23 1 12 7"]:
            self.assertEqual(config.validate_schedule(value), value)
        for value in ["@daily", "0 24 * * *", "60 * * * *", "*/0 * * * *",
                      "0 2 * * *\necho bad", "0 2 * * MON", "* * * *"]:
            with self.assertRaises(backup.BackupError):
                config.validate_schedule(value)

    def test_interactive_init_produces_private_files_and_no_plaintext_in_json(self):
        self.path.unlink()
        answers = ["", "", "", "", "", "backups", "data", "", "", "", ""]
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=answers), \
                patch.object(config.getpass, "getpass", return_value="test-secret"):
            generated = config.initialize(self.path)
        self.assertEqual(generated.schedule, "0 2 * * *")
        self.assertEqual(generated.max_backups, 0)
        self.assertEqual(json.loads(self.path.read_text())["max_backups"], 0)
        self.assertNotIn("test-secret", self.path.read_text())
        self.assertIn("test-secret", generated.defaults_file.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(generated.defaults_file.stat().st_mode & 0o777, 0o600)
        with patch("sys.stdin.isatty", return_value=True):
            with self.assertRaisesRegex(backup.BackupError, "already exists"):
                config.initialize(self.path)

    def test_init_noninteractive_requires_manual_config(self):
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(backup.BackupError, "interactive terminal"):
                config.initialize(self.root / "new.json")

    def test_publication_does_not_overwrite_existing_file_or_symlink(self):
        for target in [self.path, self.root / "link"]:
            if target.name == "link":
                target.symlink_to(self.path)
            with self.assertRaises(FileExistsError):
                config.publish_text(target, "replacement")
        self.assertEqual(json.loads(self.path.read_text()), self.data)

    def test_cron_preview_does_not_call_crontab_and_export_is_noclobber(self):
        output = self.root / "backup.cron"
        with patch("sys.argv", ["mysql-restore", "--config", str(self.path), "cron", "--output", str(output)]), \
                patch.object(cron.subprocess, "run") as command, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        command.assert_not_called()
        self.assertIn("0 2 * * *", output.read_text())
        self.assertIn("-m mysql_restore", output.read_text())
        self.assertIn("--config", output.read_text())
        self.assertNotIn("password", output.read_text())

    def test_cron_merge_is_idempotent_preserving_other_jobs(self):
        block = cron.render(config.load_config(self.path))
        unrelated = "# existing\n15 3 * * * /opt/another-job\n"
        merged = cron.merge(unrelated, block)
        self.assertEqual(cron.merge(merged, block), merged)
        changed = block.replace("0 2 * * *", "0 1 * * *")
        updated = cron.merge(merged, changed)
        self.assertTrue(updated.startswith(unrelated))
        self.assertEqual(updated.count("# BEGIN mysql-restore"), 1)
        self.assertNotIn("0 2 * * *", updated)
        with self.assertRaisesRegex(backup.BackupError, "malformed"):
            cron.merge(block.splitlines()[0] + "\n", block)

    def test_cron_quotes_spaces_and_escapes_percent_and_preserves_client_path(self):
        spaced = self.root / "config 10%.json"
        backup.atomic_json(spaced, self.data)
        with patch.dict(os.environ, {"PATH": "/opt/mysql/bin:/usr/bin"}):
            block = cron.render(config.load_config(spaced))
        self.assertIn(r"10\%.json", block)
        self.assertIn("export PATH=/opt/mysql/bin:/usr/bin", block)

    def test_cron_install_is_verified_and_noop_on_second_run(self):
        block = cron.render(config.load_config(self.path))
        existing = "# keep me\n"
        updated = cron.merge(existing, block)
        with patch.object(cron, "read_crontab", side_effect=[existing, existing, updated]), \
                patch.object(cron.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as command:
            self.assertTrue(cron.install(block))
        self.assertEqual(command.call_args.kwargs["input"], updated)
        with patch.object(cron, "read_crontab", return_value=updated), \
                patch.object(cron.subprocess, "run") as command:
            self.assertFalse(cron.install(block))
        command.assert_not_called()

    def test_crontab_access_failure_is_not_treated_as_empty(self):
        with patch.object(cron.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 1, "", "permission denied",
        )):
            with self.assertRaisesRegex(backup.BackupError, "Cannot read"):
                cron.read_crontab()
        with patch.object(cron.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 1, "", "no crontab for tester\n",
        )):
            self.assertEqual(cron.read_crontab(), "")

    def test_cron_install_refuses_concurrent_change(self):
        block = cron.render(config.load_config(self.path))
        with patch.object(cron, "read_crontab", side_effect=["", "# concurrent\n"]), \
                patch.object(cron.subprocess, "run") as command:
            with self.assertRaisesRegex(backup.BackupError, "concurrently"):
                cron.install(block)
        command.assert_not_called()

    def test_cron_noninteractive_install_requires_yes_and_never_installs(self):
        with patch("sys.argv", ["mysql-restore", "--config", str(self.path), "cron", "--install"]), \
                patch("sys.stdin.isatty", return_value=False), patch.object(cron, "install") as install, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(), 1)
        install.assert_not_called()

    def test_generated_shell_script_executes_saved_command_and_has_private_permissions(self):
        interpreter = self.root / "test-python"
        captured = self.root / "arguments"
        interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "{}"\n'.format(captured))
        interpreter.chmod(0o700)
        script = self.root / "nightly backup.sh"
        with patch.object(cron.sys, "executable", str(interpreter)):
            config.publish_text(script, cron.script_text(config.load_config(self.path)), 0o700)
        result = subprocess.run([str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(captured.read_text().splitlines(),
                         ["-m", "mysql_restore", "--config", str(self.path), "backup"])
        self.assertEqual(script.stat().st_mode & 0o777, 0o700)
        block = cron.render(config.load_config(self.path), script)
        self.assertIn("'" + str(script) + "'", block)


if __name__ == "__main__":
    unittest.main()
