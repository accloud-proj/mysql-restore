import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mysql_restore import backup


SOURCE_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
HEADER = """-- MySQL dump
SET @@GLOBAL.GTID_PURGED=/*!80000 '+'*/ 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:1-3';
-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='mysql-bin.000001', SOURCE_LOG_POS=8;
-- Current Database: `app`
CREATE DATABASE `app`;
"""


class FakeSource:
    def __init__(self, local):
        self.clock = backup.parse_time("2026-10-31T06:00:00Z")
        (local / "mysql-bin.000001").write_bytes(b"\xfebin" + b"x" * 16)

    def current_time(self):
        self.clock += dt.timedelta(seconds=1)
        return self.clock

    def databases(self):
        return ["app", "another"]

    def dump(self, path, databases, *, recovery):
        path.write_text(HEADER if recovery else "-- scoped export\n", encoding="utf-8")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.local = self.root / "source"
        self.local.mkdir()
        self.destination = self.root / "backups"
        self.destination.mkdir()
        self.source = FakeSource(self.local)
        self.config = {
            "source_uuid": SOURCE_UUID, "mysql_version": "8.4.7",
            "lower_case_table_names": 0, "character_set_server": "utf8mb4",
            "collation_server": "utf8mb4_0900_ai_ci", "sql_mode": "STRICT_TRANS_TABLES",
        }

    def test_snapshot_coordinates_accept_modern_and_legacy_headers(self):
        for header in [HEADER, HEADER.replace("REPLICATION SOURCE", "MASTER").replace("SOURCE_LOG", "MASTER_LOG")]:
            path = self.root / "dump.sql"
            path.write_text(header)
            coordinates = backup.snapshot_coordinates(path)
            self.assertEqual(coordinates["binlog_file"], "mysql-bin.000001")
            self.assertEqual(coordinates["binlog_position"], 8)
            self.assertEqual(coordinates["gtid_executed"], SOURCE_UUID + ":1-3")

    def test_snapshot_rejects_missing_gtid(self):
        path = self.root / "dump.sql"
        path.write_text("-- No GTID or coordinate\n")
        with self.assertRaises(backup.BackupError):
            backup.snapshot_coordinates(path)

    def test_full_backup_does_not_copy_or_rotate_binlogs(self):
        before = (self.local / "mysql-bin.000001").read_bytes()
        with patch.object(backup, "check_client"):
            folder = backup.create_backup(self.source, self.config, self.destination, self.local, ["app"])
        metadata = backup.read_json(folder / "metadata.json")
        self.assertEqual(folder.name, "full_20261031T060001000000Z")
        self.assertEqual(metadata["databases"], ["app", "another"])
        self.assertEqual(metadata["selected_databases"], ["app"])
        self.assertEqual(metadata["dump_sha256"], backup.sha256(folder / metadata["dump_file"]))
        self.assertEqual(metadata["anchor_sha256"], backup.sha256(self.local / "mysql-bin.000001", 8))
        self.assertTrue((folder / "selected.sql").is_file())
        self.assertEqual((self.local / "mysql-bin.000001").read_bytes(), before)
        self.assertFalse((self.destination / "binlogs").exists())
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         [folder.name + ".sql", "metadata.json", "selected.sql"])

    def test_missing_starting_log_prevents_backup_publication(self):
        (self.local / "mysql-bin.000001").unlink()
        with patch.object(backup, "check_client"):
            with self.assertRaisesRegex(backup.BackupError, "missing"):
                backup.create_backup(self.source, self.config, self.destination, self.local, [])
        self.assertFalse(any(self.destination.glob("full_*")))
        self.assertFalse(any(self.destination.glob(".pending-*")))

    def test_binlog_gaps_and_paths_rejected(self):
        for entries in [
            [{"name": "../mysql-bin.000001"}],
            [{"name": "mysql-bin.000001"}, {"name": "mysql-bin.000003"}],
            [{"name": "mysql-bin.000001"}, {"name": "other-bin.000002"}],
        ]:
            with self.assertRaises(backup.BackupError):
                backup.validate_log_chain(entries)

    def test_lock_blocks_overlapping_cron_jobs(self):
        with backup.backup_lock(self.destination):
            with self.assertRaisesRegex(backup.BackupError, "Another backup"):
                with backup.backup_lock(self.destination):
                    self.fail("Second lock must not be acquired")
        with backup.backup_lock(self.destination):
            pass

    def test_private_client_file_escapes_password(self):
        path = self.root / "client.cnf"
        backup.write_client_file(path, {"password": 'space # ";\\value'})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_text(), '[client]\npassword="space # \\";\\\\value"\n')
        with self.assertRaises(backup.BackupError):
            backup.quote_option("secret\npassword=another")

    def test_supported_versions_and_timezone_validation(self):
        self.assertEqual(backup.supported_version("8.0.32"), "8.0.32")
        self.assertEqual(backup.supported_version("8.4.7-commercial"), "8.4.7")
        for version in ["8.0.31", "5.7.44", "8.4", "8.4.7-MariaDB", "8.4.7;echo bad"]:
            with self.assertRaises(backup.BackupError):
                backup.supported_version(version)
        for version in ["8.1.0", "9.5.0", "26.10.0"]:
            self.assertEqual(backup.supported_version(version), version)
        self.assertEqual(backup.supported_version("8.0.44-0ubuntu0.24.04.1"), "8.0.44")
        self.assertEqual(backup.parse_time("2026-10-31T14:00:00+08:00"),
                         backup.parse_time("2026-10-31T06:00:00Z"))
        with self.assertRaises(backup.BackupError):
            backup.parse_time("2026-10-31 14:00:00")

    def test_partial_database_visibility_is_not_accepted_as_full_backup(self):
        source = backup.Source(self.root / "client.cnf")
        with patch.object(source, "query", return_value=[["GRANT SELECT ON `app`.* TO `backup`@`localhost`"]]):
            with self.assertRaisesRegex(backup.BackupError, "SHOW DATABASES"):
                source.databases()

    def test_bounded_hash_ignores_appends_but_rejects_truncation(self):
        path = self.local / "mysql-bin.000001"
        digest = backup.sha256(path, 20)
        with path.open("ab") as stream:
            stream.write(b"new transaction")
        self.assertEqual(backup.sha256(path, 20), digest)
        path.write_bytes(b"\xfebin")
        with self.assertRaisesRegex(backup.BackupError, "truncated"):
            backup.sha256(path, 20)

    def test_source_log_window_caps_active_file_and_does_not_flush(self):
        source = backup.Source(self.root / "client.cnf")
        with patch.object(source, "current_time", return_value=backup.parse_time("2026-10-31T06:00:00Z")), \
                patch.object(source, "query", side_effect=[
                    [["mysql-bin.000002", "20", "", "", SOURCE_UUID + ":1-3"]],
                    [["mysql-bin.000001", "100"], ["mysql-bin.000002", "50"],
                     ["mysql-bin.000003", "4"]],
                ]) as query:
            window = source.log_window("8.4.7")
        self.assertEqual(window["files"], [
            {"name": "mysql-bin.000001", "size": 100}, {"name": "mysql-bin.000002", "size": 20},
        ])
        self.assertEqual(query.call_args_list[0].args[0], "SHOW BINARY LOG STATUS")
        self.assertNotIn("FLUSH", str(query.call_args_list))

    def test_mysql80_uses_legacy_status_statement(self):
        source = backup.Source(self.root / "client.cnf")
        with patch.object(source, "current_time", return_value=backup.parse_time("2026-10-31T06:00:00Z")), \
                patch.object(source, "query", side_effect=[
                    [["mysql-bin.000001", "20", "", "", SOURCE_UUID + ":1-3"]],
                    [["mysql-bin.000001", "20"]],
                ]) as query:
            source.log_window("8.0.32")
        self.assertEqual(query.call_args_list[0].args[0], "SHOW MASTER STATUS")

    def test_binlog_directory_is_queried_and_requires_absolute_path(self):
        source = backup.Source(self.root / "client.cnf")
        with patch.object(source, "query", return_value=[["/var/lib/mysql/binlog"]]):
            self.assertEqual(source.log_directory(), Path("/var/lib/mysql"))
        for rows in [[], [[""]], [["binlog"]], [["/var/lib/mysql/binlog", "extra"]]]:
            with patch.object(source, "query", return_value=rows):
                with self.assertRaises(backup.BackupError):
                    source.log_directory()

    def test_replica_requires_local_replication_binlogs_and_readonly(self):
        source = backup.Source(self.root / "client.cnf")
        row = ["8.4.7", SOURCE_UUID, "1", "ON", "ON", "ROW", "0", "0",
               "utf8mb4", "utf8mb4_0900_ai_ci", "", "0", "1", "1"]
        with patch.object(source, "query", return_value=[row]):
            self.assertEqual(source.inspect("replica")["role"], "replica")
        for values in [["0", "1"], ["1", "0"]]:
            with patch.object(source, "query", return_value=[row[:12] + values]):
                with self.assertRaisesRegex(backup.BackupError, "log_replica_updates"):
                    source.inspect("replica")

    def test_auto_role_uses_replication_channels_and_enforces_replica_requirements(self):
        source = backup.Source(self.root / "client.cnf")
        row = ["8.4.7", SOURCE_UUID, "1", "ON", "ON", "ROW", "0", "0",
               "utf8mb4", "utf8mb4_0900_ai_ci", "", "0", "1", "1"]
        for channels, expected in [([], "primary"), ([["channel"]], "replica"),
                                   ([["first"], ["second"]], "replica")]:
            with patch.object(source, "query", side_effect=[[row], channels]) as query:
                self.assertEqual(source.inspect(None)["role"], expected)
            self.assertEqual(query.call_args_list[1].args[0], "SHOW REPLICA STATUS")
        with patch.object(source, "query", side_effect=[[row[:12] + ["0", "1"]], [["channel"]]]):
            with self.assertRaisesRegex(backup.BackupError, "log_replica_updates"):
                source.inspect(None)
        with patch.object(source, "query", side_effect=[
                [row], backup.BackupError("REPLICATION CLIENT denied")]):
            with self.assertRaisesRegex(backup.BackupError, "denied"):
                source.inspect(None)

    def test_multisource_gtid_newlines_do_not_create_fake_result_rows(self):
        source = backup.Source(self.root / "client.cnf")
        with patch.object(backup, "run", return_value="mysql-bin.000001\t200\t\t\ta:1-2,\\nb:1-3\n"):
            rows = source.query("SHOW BINARY LOG STATUS")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], "a:1-2,\nb:1-3")


if __name__ == "__main__":
    unittest.main()
