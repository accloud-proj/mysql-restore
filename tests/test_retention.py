import contextlib
import datetime as dt
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mysql_restore import backup, cli, config, retention


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "backups"
        self.directory.mkdir()

    def snapshot(self, day):
        stamp = dt.datetime(2026, 10, day, tzinfo=backup.UTC)
        folder = self.directory / ("full_" + stamp.strftime("%Y%m%dT%H%M%S%fZ"))
        folder.mkdir()
        dump = folder / (folder.name + ".sql")
        dump.write_text("SELECT {};\n".format(day))
        backup.atomic_json(folder / "metadata.json", {
            "format": 1, "completed_at": backup.timestamp(stamp), "dump_file": dump.name,
            "dump_sha256": backup.sha256(dump), "mysql_version": "8.4.7",
        })
        return folder

    def test_keeps_exact_latest_count_and_ignores_unrelated_files(self):
        folders = [self.snapshot(day) for day in [1, 2, 3, 4]]
        (self.directory / "notes.txt").write_text("preserve")
        (self.directory / ".pending-test").mkdir()
        removed = retention.cleanup(self.directory, 2)
        self.assertEqual(removed, [folders[1], folders[0]])
        self.assertEqual(len(list(self.directory.glob("full_*"))), 2)
        self.assertTrue(folders[3].exists())
        self.assertTrue((self.directory / "notes.txt").exists())
        self.assertTrue((self.directory / ".pending-test").exists())
        self.assertEqual(retention.cleanup(self.directory, 2), [])

    def test_dry_run_does_not_delete(self):
        folders = [self.snapshot(day) for day in [1, 2, 3]]
        self.assertEqual(retention.cleanup(self.directory, 1, dry_run=True), [folders[1], folders[0]])
        self.assertTrue(all(folder.exists() for folder in folders))

    def test_symlinks_unknown_contents_and_corrupt_retained_snapshot_fail_before_deletion(self):
        older, latest = self.snapshot(1), self.snapshot(2)
        (older / "unrelated").mkdir()
        with self.assertRaisesRegex(backup.BackupError, "unexpected"):
            retention.cleanup(self.directory, 1)
        self.assertTrue(older.exists())
        (older / "unrelated").rmdir()
        (latest / (latest.name + ".sql")).write_text("corrupt")
        with self.assertRaisesRegex(backup.BackupError, "SHA-256"):
            retention.cleanup(self.directory, 1)
        self.assertTrue(older.exists())
        external = self.root / "external"
        external.mkdir()
        (self.directory / "full_symlink").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(backup.BackupError, "unsafe"):
            retention.cleanup(self.directory, 1)
        self.assertTrue(external.exists())

    def test_count_validation_and_config_backward_compatibility(self):
        for invalid in [-1, -2, True, "3", 1.5]:
            with self.assertRaises(backup.BackupError):
                retention.validate_count(invalid)
        data = {
            "format": 1, "host": "127.0.0.1", "port": 3306, "user": "backup",
            "defaults_file": "client.cnf", "mysql_version": "8.4.7", "role": "primary",
            "backup_dir": "backups", "binlog_dir": "data", "schedule": "0 2 * * *",
            "log_file": "backup.log",
        }
        self.assertEqual(config.parse_config(self.root / "config.json", data).max_backups, 0)
        self.assertEqual(config.parse_config(self.root / "config.json", dict(data, max_backups=None)).max_backups, 0)
        self.assertEqual(config.parse_config(self.root / "config.json", dict(data, max_backups=7)).max_backups, 7)
        self.assertEqual(config.parse_config(self.root / "config.json", dict(data, max_backups=0)).max_backups, 0)
        with self.assertRaises(backup.BackupError):
            config.parse_config(self.root / "config.json", dict(data, max_backups=-2))

    def test_zero_disables_cleanup_and_one_keeps_latest_one(self):
        folders = [self.snapshot(day) for day in [1, 2, 3]]
        self.assertEqual(retention.cleanup(self.directory, 0), [])
        self.assertTrue(all(folder.exists() for folder in folders))
        self.assertEqual(retention.cleanup(self.directory, 1, dry_run=True), [folders[1], folders[0]])
        self.assertTrue(all(folder.exists() for folder in folders))
        self.assertEqual(retention.cleanup(self.directory, 1), [folders[1], folders[0]])
        self.assertTrue(folders[2].exists())
        self.assertEqual(len(list(self.directory.glob("full_*"))), 1)

    def test_cleanup_cli_does_not_connect_to_source(self):
        for day in [1, 2, 3]:
            self.snapshot(day)
        path = self.root / "config.json"
        backup.atomic_json(path, {
            "format": 1, "host": "127.0.0.1", "port": 3306, "user": "backup",
            "defaults_file": "client.cnf", "mysql_version": "8.4.7", "role": "primary",
            "backup_dir": "backups", "binlog_dir": "data", "schedule": "0 2 * * *",
            "log_file": "backup.log", "max_backups": 1,
        })
        with patch("sys.argv", ["mysql-restore", "--config", str(path), "cleanup", "--dry-run"]), \
                patch.object(backup, "source_connection") as connection, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 0)
        connection.assert_not_called()
        self.assertIn("Would delete:", output.getvalue())
        self.assertEqual(len(list(self.directory.glob("full_*"))), 3)

    def test_shared_lock_blocks_cleanup_during_backup_or_restore(self):
        self.snapshot(1)
        with backup.backup_lock(self.directory):
            with self.assertRaisesRegex(backup.BackupError, "Another backup"):
                retention.cleanup(self.directory, 1)

    def test_automatic_cleanup_occurs_only_after_successful_full_backup(self):
        from contextlib import contextmanager

        old = self.snapshot(1)
        local = self.root / "data"
        local.mkdir()
        source = SimpleNamespace(inspect=lambda role: {"mysql_version": "8.4.7"})

        @contextmanager
        def connection(args):
            yield source

        args = SimpleNamespace(
            backup_dir=self.directory, binlog_dir=local, role="primary",
            mysql_version="8.4.7", database=[], max_backups=1,
        )
        with patch.object(backup, "source_connection", side_effect=connection), \
                patch.object(backup, "check_client"), \
                patch.object(backup, "create_backup", side_effect=backup.BackupError("dump failed")):
            with self.assertRaisesRegex(backup.BackupError, "dump failed"):
                backup.execute(args)
        self.assertTrue(old.exists())
        with patch.object(backup, "source_connection", side_effect=connection), \
                patch.object(backup, "check_client"), \
                patch.object(backup, "create_backup", side_effect=lambda *unused: self.snapshot(2)), \
                contextlib.redirect_stdout(io.StringIO()):
            latest = backup.execute(args)
        self.assertTrue(latest.exists())
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
