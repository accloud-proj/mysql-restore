from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

from mysql_restore import backup, restore


def binlog_fixture(*, in_use: bool) -> bytes:
    body = (struct.pack("<H", 4) + b"8.4.7".ljust(50, b"\0")
            + struct.pack("<I", 1) + bytes([19]) + bytes([19] * 41) + bytes([1]))
    length = 19 + len(body) + 4
    header = struct.pack("<IBIIIH", 1, 15, 1, length, length + 4, 0)
    event = header + body
    checksum = struct.pack("<I", zlib.crc32(event))
    event = bytearray(event)
    event[17] = int(in_use)
    return b"\xfebin" + event + checksum + b"transaction payload"


class BinlogIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.local = Path(self.temporary.name).resolve()
        self.path = self.local / "binlog.000003"
        self.original = binlog_fixture(in_use=True)
        self.path.write_bytes(self.original)
        self.size = len(self.original)
        self.metadata = {
            "binlog_file": self.path.name, "binlog_position": self.size,
            "anchor_sha256": backup.sha256(self.path, self.size),
        }
        self.target = backup.parse_time("2026-11-01T00:01:00+08:00")
        self.window = {
            "covered_until": "2026-10-31T16:02:00Z",
            "files": [{"name": self.path.name, "size": self.size}],
        }

    def test_legacy_open_anchor_survives_rotation_and_repeated_restore(self):
        self.path.write_bytes(binlog_fixture(in_use=False) + b"rotate event")
        for _ in range(2):
            entries = restore.select_logs(self.local, self.metadata, self.target, self.window)
            restore.verify_logs(self.local, entries)
        self.assertEqual(self.path.read_bytes(), binlog_fixture(in_use=False) + b"rotate event")

    def test_rotation_during_restore_is_allowed_but_data_changes_are_not(self):
        entries = restore.select_logs(self.local, self.metadata, self.target, self.window)
        self.path.write_bytes(binlog_fixture(in_use=False) + b"new events")
        restore.verify_logs(self.local, entries)
        content = bytearray(self.path.read_bytes())
        content[-len(b"new events") - 1] ^= 1
        self.path.write_bytes(content)
        with self.assertRaisesRegex(backup.BackupError, "changed during recovery"):
            restore.verify_logs(self.local, entries)

    def test_header_flags_checksum_and_payload_changes_cannot_bypass_legacy_anchor(self):
        # Only the low bit at file offset 4 + 17 may change.
        for offset, mask in [(21, 2), (22, 1), (4, 1), (9, 1), (25, 1),
                             (self.size - 20, 1), (self.size - 1, 1)]:
            with self.subTest(offset=offset, mask=mask):
                changed = bytearray(binlog_fixture(in_use=False))
                changed[offset] ^= mask
                self.path.write_bytes(changed)
                with self.assertRaises(backup.BackupError):
                    restore.select_logs(self.local, self.metadata, self.target, self.window)

    def test_new_backup_stores_stable_digest_without_changing_source(self):
        source = Mock()
        source.databases.return_value = ["shop"]
        source.current_time.return_value = self.target

        def dump(path, databases, *, recovery):
            path.write_text(
                "SET @@GLOBAL.GTID_PURGED='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:1';\n"
                "-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='binlog.000003', "
                "SOURCE_LOG_POS={};\n-- Current Database: `shop`\n".format(self.size)
            )

        source.dump.side_effect = dump
        directory = self.local / "backups"
        directory.mkdir()
        with patch.object(backup, "check_client"):
            folder = backup.create_backup(source, {"mysql_version": "8.4.7"}, directory, self.local, [])
        metadata = backup.read_json(folder / "metadata.json")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertNotEqual(metadata["anchor_sha256"], backup.sha256(self.path, self.size))
        self.path.write_bytes(binlog_fixture(in_use=False))
        self.assertEqual(metadata["anchor_sha256"], backup.sha256(self.path, self.size))
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         [folder.name + ".sql", "metadata.json"])
        restore.select_logs(self.local, metadata, self.target, self.window)

    def test_legacy_closed_anchor_and_normalized_anchor_both_work(self):
        self.path.write_bytes(binlog_fixture(in_use=False))
        self.metadata["anchor_sha256"] = backup.sha256(self.path, self.size)
        restore.select_logs(self.local, self.metadata, self.target, self.window)
        self.assertEqual(backup.binlog_digests(self.path, self.size)[0], self.metadata["anchor_sha256"])

    def test_hash_boundary_ignores_appends_but_detects_truncation(self):
        expected = backup.binlog_digests(self.path, self.size)
        with self.path.open("ab") as stream:
            stream.write(b"appended transactions" * 100000)
        self.assertEqual(backup.binlog_digests(self.path, self.size), expected)
        self.path.write_bytes(self.original[:-1])
        with self.assertRaisesRegex(backup.BackupError, "truncated"):
            backup.binlog_digests(self.path, self.size)

    def test_streaming_hash_covers_bytes_after_first_megabyte(self):
        self.path.write_bytes(self.original + b"x" * 2097152)
        size = self.path.stat().st_size
        closed = binlog_fixture(in_use=False) + b"x" * 2097152
        expected_path = self.local / "closed"
        expected_path.write_bytes(closed)
        self.assertEqual(backup.binlog_digests(self.path, size, legacy=True),
                         (backup.sha256(expected_path), backup.sha256(self.path)))
        expected = backup.binlog_digests(self.path, size)
        with self.path.open("r+b") as stream:
            stream.seek(size - 1)
            stream.write(b"y")
        self.assertNotEqual(backup.binlog_digests(self.path, size), expected)

    def test_invalid_format_headers_are_not_normalized(self):
        for offset, value in [(13, 0), (23, 3), (80, 20), (0, 0)]:
            with self.subTest(offset=offset):
                changed = bytearray(self.original)
                changed[offset] = value
                self.path.write_bytes(changed)
                with self.assertRaises(backup.BackupError):
                    backup.binlog_digests(self.path, self.size)
        self.path.write_bytes(self.original)
        with self.assertRaisesRegex(backup.BackupError, "format description"):
            backup.binlog_digests(self.path, 30)
        # Do not apply the bit exception to a different event type.
        changed = bytearray(self.original)
        changed[8] = 2
        self.path.write_bytes(changed)
        self.assertEqual(backup.binlog_digests(self.path, self.size, legacy=True),
                         (backup.sha256(self.path, self.size),))


if __name__ == "__main__":
    unittest.main()
