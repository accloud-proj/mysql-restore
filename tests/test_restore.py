from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from mysql_restore import backup, restore


DECODED = """# at 120
#261031  6:00:01 server id 1  end_log_pos 210 CRC32 0x12345678 GTID last_committed=0 immediate_commit_timestamp=1000001
# original_commit_timestamp=999999 (not used)
# immediate_commit_timestamp=1000001 (UTC)
SET @@SESSION.GTID_NEXT= 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:4'/*!*/;
# at 210
#261031  6:00:01 server id 1 end_log_pos 300 Query thread_id=1
BEGIN
# at 300
#261031  6:00:01 server id 1 end_log_pos 331 Xid = 1
COMMIT/*!*/;
# at 331
#261031  6:00:02 server id 1 end_log_pos 421 GTID last_committed=1
# original_commit_timestamp=1000000 (not used)
# immediate_commit_timestamp=2000000 (UTC)
# at 421
"""


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.local = self.root / "source"
        self.local.mkdir()
        self.config = {
            "mysql_version": "8.4.7", "source_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "lower_case_table_names": 0, "character_set_server": "utf8mb4",
            "collation_server": "utf8mb4_0900_ai_ci", "sql_mode": "STRICT_TRANS_TABLES",
        }

    def snapshot(self, stamp):
        completed = backup.parse_time(stamp)
        name = "full_" + completed.strftime("%Y%m%dT%H%M%S%fZ")
        folder = self.root / name
        folder.mkdir()
        dump = folder / (name + ".sql")
        dump.write_text("-- full snapshot\n")
        metadata = dict(
            self.config, format=1, completed_at=stamp, dump_file=dump.name,
            dump_sha256=backup.sha256(dump), binlog_file="mysql-bin.000001", binlog_position=4,
            anchor_sha256=hashlib.sha256(b"\xfebin").hexdigest(),
        )
        backup.atomic_json(folder / "metadata.json", metadata)
        return folder, metadata

    def window(self, covered_until="2026-10-31T08:00:00Z"):
        entries = []
        for index in range(1, 4):
            name = "mysql-bin.%06d" % index
            path = self.local / name
            path.write_bytes(b"\xfebin" + bytes([index]) * 20)
            entries.append({"name": name, "size": path.stat().st_size, "sha256": backup.sha256(path)})
        return {"covered_until": covered_until, "files": entries}

    def test_nearest_completed_snapshot_selected_not_future_snapshot(self):
        first, _ = self.snapshot("2026-10-31T06:00:00.000000Z")
        latest, _ = self.snapshot("2026-10-31T07:00:00.000000Z")
        target = backup.parse_time("2026-10-31T07:30:00Z")
        folder, _, _ = restore.select_snapshot(self.root, target, "8.4.7")
        self.assertEqual(folder, latest)
        earlier, _, _ = restore.select_snapshot(
            self.root, backup.parse_time("2026-10-31T06:30:00Z"), "8.4.7",
        )
        self.assertEqual(earlier, first)
        with self.assertRaises(backup.BackupError):
            restore.select_snapshot(self.root, target, "8.4.6")
        with self.assertRaises(backup.BackupError):
            restore.select_snapshot(self.root, backup.parse_time("2026-10-31T05:00:00Z"), "8.4.7")

    def test_corrupt_snapshot_rejected_without_older_fallback(self):
        folder, _ = self.snapshot("2026-10-31T06:00:00.000000Z")
        next(folder.glob("*.sql")).write_text("corrupt")
        with self.assertRaisesRegex(backup.BackupError, "SHA-256"):
            restore.select_snapshot(self.root, backup.parse_time("2026-10-31T07:00:00Z"), "8.4.7")

    def test_filename_timestamp_mismatch_rejected(self):
        folder, metadata = self.snapshot("2026-10-31T06:00:00.000000Z")
        metadata["completed_at"] = "2026-10-31T05:00:00Z"
        backup.atomic_json(folder / "metadata.json", metadata)
        with self.assertRaisesRegex(backup.BackupError, "does not match"):
            restore.select_snapshot(self.root, backup.parse_time("2026-10-31T07:00:00Z"), "8.4.7")

    def test_log_coverage_and_integrity_are_enforced(self):
        _, metadata = self.snapshot("2026-10-31T06:00:00.000000Z")
        window = self.window()
        selected = restore.select_logs(self.local, metadata, backup.parse_time("2026-10-31T07:00:00Z"), window)
        self.assertEqual(selected, window["files"])
        with self.assertRaisesRegex(backup.BackupError, "committed log coverage"):
            restore.select_logs(self.local, metadata, backup.parse_time("2026-10-31T09:00:00Z"), window)
        (self.local / selected[-1]["name"]).write_bytes(b"bad")
        with self.assertRaisesRegex(backup.BackupError, "shorter"):
            restore.select_logs(self.local, metadata, backup.parse_time("2026-10-31T07:00:00Z"), window)

    def test_commit_timestamp_is_not_event_start_or_original_source_time(self):
        events = list(restore.commit_boundaries(DECODED.splitlines(), initial_position=120))
        self.assertEqual(events, [(120, 1000001), (331, 2000000)])
        indexed = [("mysql-bin.000001", position, committed) for position, committed in events]
        self.assertEqual(restore.plan_replay(indexed, 1500000), ("mysql-bin.000001", 331))
        self.assertEqual(restore.plan_replay(indexed, 1000000), ("mysql-bin.000001", 120))
        self.assertIsNone(restore.plan_replay(indexed, 2000000))

    def test_missing_timestamps_and_anonymous_transactions_rejected(self):
        for text in [
            DECODED.replace("immediate_commit_timestamp=", "other="),
            DECODED.replace(" GTID ", " Anonymous_GTID "),
            DECODED.replace("immediate_commit_timestamp=1000001", "immediate_commit_timestamp=0"),
        ]:
            with self.assertRaises(backup.BackupError):
                list(restore.commit_boundaries(text.splitlines()))

    def test_clock_reversal_across_target_rejected(self):
        with self.assertRaisesRegex(backup.BackupError, "out of order"):
            restore.plan_replay([("a", 100, 200), ("b", 100, 50)], 100)

    def test_replay_uses_start_coordinate_and_transaction_boundary(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config, self.local)
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            kwargs["stdout"].write(b"SELECT 1;\n")
            return ""

        entries = [{"name": "mysql-bin.000001", "size": 900}, {"name": "mysql-bin.000002", "size": 900},
                   {"name": "mysql-bin.000003", "size": 900}]
        with patch.object(restore, "run", side_effect=fake_run), patch.object(recovery, "import_sql") as imported:
            recovery.replay(entries, 120, ("mysql-bin.000002", 331))
        self.assertEqual(len(commands), 2)
        self.assertIn("--start-position=120", commands[0])
        self.assertIn("--stop-position=900", commands[0])
        self.assertIn("--start-position=4", commands[1])
        self.assertIn("--stop-position=331", commands[1])
        self.assertTrue(all("--base64-output=DECODE-ROWS" not in command for command in commands))
        imported.assert_called_once()
        self.assertFalse((self.root / "replay.sql").exists())

    def test_replay_at_first_gtid_can_be_empty(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config, self.local)
        with patch.object(restore, "run") as command, patch.object(recovery, "import_sql") as imported:
            recovery.replay([{"name": "mysql-bin.000001", "size": 900}], 120, ("mysql-bin.000001", 120))
        command.assert_not_called()
        imported.assert_not_called()

    def test_binlog_failure_prevents_import(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config, self.local)
        with patch.object(restore, "run", side_effect=backup.BackupError("checksum mismatch")), \
                patch.object(recovery, "import_sql") as imported:
            with self.assertRaises(backup.BackupError):
                recovery.replay([{"name": "mysql-bin.000001", "size": 900}], 4, None)
        imported.assert_not_called()

    def test_selected_table_export_is_no_clobber_and_contains_database(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config)
        output = self.root / "output.sql"
        responses = {
            "SHOW DATABASES": "mysql\napp\nanother\n",
            "SHOW TABLES FROM `app`": "orders\nunwanted\n",
            "SHOW CREATE DATABASE `app`": "app\tCREATE DATABASE `app` DEFAULT CHARACTER SET utf8mb4\n",
        }

        def fake_run(command, **kwargs):
            self.assertNotIn("--databases", command)
            self.assertEqual(command[-2:], ["app", "orders"])
            self.assertIn("--set-gtid-purged=OFF", command)
            kwargs["stdout"].write(b"CREATE TABLE orders (id INT);\n")
            return ""

        with patch.object(recovery, "query", side_effect=lambda sql: responses[sql]), \
                patch.object(restore, "run", side_effect=fake_run):
            recovery.export(output, ["app"], ["orders"])
            content = output.read_bytes()
            self.assertIn(b"USE `app`;", content)
            self.assertIn(b"CREATE TABLE orders", content)
            with self.assertRaises(FileExistsError):
                recovery.export(output, ["app"], ["orders"])
        self.assertEqual(output.read_bytes(), content)
        self.assertFalse(any(self.root.glob(".mysql-export-*")))

    def test_export_failure_never_publishes_partial_sql(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config)
        output = self.root / "output.sql"
        with patch.object(recovery, "query", return_value="app\n"), \
                patch.object(restore, "run", side_effect=backup.BackupError("dump failed")):
            with self.assertRaises(backup.BackupError):
                recovery.export(output, [], [])
        self.assertFalse(output.exists())
        self.assertFalse(any(self.root.glob(".mysql-export-*")))

    def test_container_is_isolated_and_event_scheduler_disabled(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config, self.local)
        with patch.object(restore, "run", return_value="") as command, \
                patch.object(recovery, "configure_binlog_tool"), \
                patch.object(recovery, "query", return_value="8.4.7\n"):
            recovery.start(1)
        start = command.call_args_list[1].args[0]
        self.assertIn("--network", start)
        self.assertEqual(start[start.index("--network") + 1], "none")
        self.assertIn("--event-scheduler=DISABLED", start)
        self.assertIn("--gtid-mode=ON", start)
        self.assertIn("mysql:8.4.7", start)
        self.assertNotIn("--publish", start)
        self.assertNotIn("-p", start)
        self.assertIn("type=bind,source={},target=/source-binlogs,readonly".format(self.local), start)
        self.assertNotIn((self.root / "root-password").read_text(), " ".join(start))

    def test_cleanup_attempts_both_resources_and_surfaces_failure(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config)
        recovery.container_created = True
        recovery.volume_created = True
        with patch.object(restore, "run", side_effect=[backup.BackupError("container busy"), ""]) as command:
            with self.assertRaisesRegex(backup.BackupError, "Cleanup failed"):
                recovery.cleanup()
        self.assertEqual(command.call_count, 2)

    def test_official_image_never_requires_custom_build(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config)
        with patch.object(restore, "run") as command:
            self.assertEqual(recovery.ensure_image(), "mysql:8.4.7")
        command.assert_not_called()

    def test_container_with_wrong_binlog_tool_version_rejected(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config)
        with patch.object(restore, "run", side_effect=["present", "mysqlbinlog Ver 8.4.6 for Linux\n"]):
            with self.assertRaisesRegex(backup.BackupError, "version mismatch"):
                recovery.configure_binlog_tool()

    def test_container_tool_preferred_and_host_fallback_only_when_absent(self):
        recovery = restore.Recovery(self.root, "8.4.7", self.config, self.local)
        with patch.object(restore, "run", side_effect=["present", "mysqlbinlog Ver 8.4.7 for Linux\n"]), \
                patch.object(restore, "check_client") as host:
            recovery.configure_binlog_tool()
        host.assert_not_called()
        command = recovery.binlog_command("mysql-bin.000001", 4, decode=False)
        self.assertEqual(command[:2], ["docker", "exec"])
        with patch.object(restore, "run", return_value="absent"), \
                patch.object(restore, "check_client") as host:
            recovery.configure_binlog_tool()
        host.assert_called_once_with("mysqlbinlog", "8.4.7")
        command = recovery.binlog_command("mysql-bin.000001", 4, decode=False)
        self.assertEqual(command[:2], ["mysqlbinlog", "--no-defaults"])
        self.assertEqual(command[-1], str(self.local / "mysql-bin.000001"))

    def test_appending_live_file_is_allowed_but_rewriting_prefix_fails(self):
        _, metadata = self.snapshot("2026-10-31T06:00:00.000000Z")
        window = self.window()
        entries = restore.select_logs(self.local, metadata, backup.parse_time("2026-10-31T07:00:00Z"), window)
        last = self.local / entries[-1]["name"]
        with last.open("ab") as stream:
            stream.write(b"next transactions")
        restore.verify_logs(self.local, entries)
        last.write_bytes(b"\xfebin" + b"z" * 20)
        with self.assertRaisesRegex(backup.BackupError, "changed"):
            restore.verify_logs(self.local, entries)

    def test_wrong_index_or_missing_anchor_fails(self):
        _, metadata = self.snapshot("2026-10-31T06:00:00.000000Z")
        window = self.window()
        index = self.local / "mysql-bin.index"
        index.write_text("./mysql-bin.000001\n./mysql-bin.000003\n")
        with self.assertRaisesRegex(backup.BackupError, "index"):
            restore.select_logs(self.local, metadata, backup.parse_time("2026-10-31T07:00:00Z"), window, index)
        index.write_text("".join("/var/lib/mysql/" + entry["name"] + "\n" for entry in window["files"]))
        self.assertEqual(len(restore.select_logs(
            self.local, metadata, backup.parse_time("2026-10-31T07:00:00Z"), window, index,
        )), 3)
        metadata["anchor_sha256"] = "wrong"
        with self.assertRaisesRegex(backup.BackupError, "rewritten"):
            restore.select_logs(self.local, metadata, backup.parse_time("2026-10-31T07:00:00Z"), window)


if __name__ == "__main__":
    unittest.main()
