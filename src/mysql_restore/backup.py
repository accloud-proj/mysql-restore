#!/usr/bin/env python3
"""Create mysqldump GTID snapshots without copying or rotating source binlogs."""

import argparse
import contextlib
import datetime as dt
import errno
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from typing import Iterator
from typing import Optional

if os.name == "nt":
    import msvcrt
else:
    import fcntl


UTC = dt.timezone.utc
SYSTEM_DATABASES = {"mysql", "information_schema", "performance_schema", "sys"}
LOG_NAME = re.compile(r"([A-Za-z0-9_.-]+)\.(\d+)")


class BackupError(Exception):
    pass


def timestamp(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("Time must be ISO 8601, e.g. 2026-10-31T14:00:00+08:00") from exc
    if result.tzinfo is None:
        raise BackupError("Time must include Z or an explicit UTC offset")
    return result.astimezone(UTC)


def sha256(path: Path, size: Optional[int] = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = size
        while remaining is None or remaining:
            block = stream.read(1024 * 1024 if remaining is None else min(remaining, 1024 * 1024))
            if not block:
                if remaining:
                    raise BackupError("Binlog was truncated: " + str(path))
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


def binlog_digests(path: Path, size: int, *, legacy: bool = False) -> tuple[str, ...]:
    """Hash a fixed prefix, normalizing only the FDE's binlog-in-use bit."""
    if size < 4:
        raise BackupError("Invalid binlog hash boundary")
    with path.open("rb") as stream:
        prefix_size = min(size, 80)
        prefix = bytearray(stream.read(prefix_size))
        if len(prefix) != prefix_size:
            raise BackupError("Binlog was truncated: " + str(path))
        if prefix[:4] != b"\xfebin":
            raise BackupError("Not an unencrypted MySQL binlog: " + str(path))
        has_fde = len(prefix) >= 23 and prefix[8] == 15
        if has_fde:
            event_size = int.from_bytes(prefix[13:17], "little")
            # Common-header length follows magic(4), header(19), version(2),
            # server version(50), and creation timestamp(4): file offset 79.
            if (len(prefix) < 80 or not 76 <= event_size <= size - 4
                    or prefix[23:25] != b"\x04\x00" or prefix[79] != 19):
                raise BackupError("Invalid binlog format description: " + str(path))
            # MySQL clears this bit at close/rotation; FDE CRC32 excludes it.
            # File offset = 4-byte magic + 17-byte event-header flags offset.
            prefix[21] &= ~1
        digests = [hashlib.sha256(prefix)]
        if legacy and has_fde:
            prefix[21] |= 1
            digests.append(hashlib.sha256(prefix))
        remaining = size - prefix_size
        while remaining:
            block = stream.read(min(remaining, 1024 * 1024))
            if not block:
                raise BackupError("Binlog was truncated: " + str(path))
            for digest in digests:
                digest.update(block)
            remaining -= len(block)
    return tuple(digest.hexdigest() for digest in digests)


def atomic_json(path: Path, data: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            result = json.load(stream)
    except (OSError, ValueError) as exc:
        raise BackupError("Cannot read metadata: {}".format(path)) from exc
    if not isinstance(result, dict) or result.get("format") != 1:
        raise BackupError("Unsupported metadata format: {}".format(path))
    return result


@contextlib.contextmanager
def backup_lock(directory: Path) -> Iterator[None]:
    with (directory / ".backup.lock").open("a+b") as stream:
        if os.name == "nt":
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise BackupError("Another backup job is using this directory") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def unsafe_permissions(path: Path, mask: int) -> bool:
    # Windows chmod exposes read-only flags, not ACL privacy.
    return os.name != "nt" and bool(path.stat().st_mode & mask)


def run(command: list, *, stdout=None, stdin=None, env=None, include_stderr=False) -> str:
    result = subprocess.run(
        command, stdin=stdin, stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE, env=env,
    )
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        if stdout is None and result.stdout:
            error += "\n" + result.stdout.decode("utf-8", errors="replace").strip()
        raise BackupError("{} failed ({}): {}".format(command[0], result.returncode, error))
    if stdout is None:
        text = result.stdout.decode("utf-8")
        if include_stderr:
            text += result.stderr.decode("utf-8", errors="replace")
        return text
    return ""


def supported_version(value: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-(?:commercial|0ubuntu[0-9A-Za-z.+~]*))?", value)
    if not match:
        raise BackupError("Expected an exact Oracle MySQL version, e.g. 8.4.7 or 26.10.0: " + value)
    version = match.group(1)
    numbers = tuple(int(part) for part in version.split("."))
    if numbers < (8, 0, 32):
        raise BackupError("MySQL 8.0.32+ is required for consistent GTID snapshots")
    return version


def check_client(program: str, version: str) -> None:
    text = run([program, "--version"])
    match = re.search(r"\b(?:Distrib|based on MySQL server)\s+(\d+\.\d+\.\d+)", text)
    if match is None:
        match = re.search(r"\bVer\s+(\d+\.\d+\.\d+)", text)
    if not match or match.group(1) != version:
        raise BackupError("{} must match source version {} (found {})".format(
            program, version, text.strip(),
        ))


def quote_option(value: str) -> str:
    if any(character in value for character in "\x00\r\n"):
        raise BackupError("MySQL option values cannot contain NUL or newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_client_file(path: Path, values: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write("[client]\n")
        for key, value in values.items():
            stream.write("{}={}\n".format(key, quote_option(str(value))))


def quote_identifier(value: str) -> str:
    if not value or any(character in value for character in "\x00\r\n\t"):
        raise BackupError("Database/table names must be nonempty and contain no NUL, tabs or newlines")
    return "`" + value.replace("`", "``") + "`"


class Source:
    def __init__(self, options: Path):
        self.options = options

    def query(self, sql: str) -> list:
        text = run([
            "mysql", "--defaults-file=" + str(self.options),
            "--batch", "--skip-column-names", "--execute", sql,
        ])
        escapes = {"0": "\x00", "b": "\b", "t": "\t", "n": "\n", "r": "\r", "Z": "\x1a", "\\": "\\"}

        def unescape(value: str) -> str:
            return re.sub(r"\\([0btnrZ\\])", lambda match: escapes[match.group(1)], value)

        return [[unescape(field) for field in line.split("\t")] for line in text.splitlines()]

    def inspect(self, role: Optional[str] = "primary") -> dict:
        rows = self.query(
            "SELECT @@version, @@server_uuid, @@log_bin, @@gtid_mode, "
            "@@enforce_gtid_consistency, @@binlog_format, @@binlog_encryption, "
            "@@lower_case_table_names, @@character_set_server, @@collation_server, "
            "@@sql_mode, @@binlog_transaction_compression, @@log_replica_updates, @@read_only"
        )
        if len(rows) != 1 or len(rows[0]) != 14:
            raise BackupError("Unexpected source server configuration response")
        row = rows[0]
        version = supported_version(row[0])
        if row[2:5] != ["1", "ON", "ON"] or row[6] != "0":
            raise BackupError(
                "Source requires log_bin=ON, gtid_mode=ON, enforce_gtid_consistency=ON, "
                "binlog_encryption=OFF"
            )
        if row[5] not in {"ROW", "MIXED", "STATEMENT"}:
            raise BackupError("Unsupported source binlog_format: " + row[5])
        if row[11] != "0":
            raise BackupError("binlog_transaction_compression must be OFF")
        if role is None:
            role = "replica" if self.query("SHOW REPLICA STATUS") else "primary"
        if role == "replica" and row[12] != "1":
            raise BackupError("Replica backups require log_replica_updates=ON")
        try:
            source_uuid = str(uuid.UUID(row[1]))
        except ValueError as exc:
            raise BackupError("Invalid source server_uuid") from exc
        return {
            "mysql_version": version, "source_uuid": source_uuid,
            "lower_case_table_names": int(row[7]),
            "character_set_server": row[8], "collation_server": row[9], "sql_mode": row[10],
            "role": role,
        }

    def log_directory(self) -> Path:
        rows = self.query("SELECT @@log_bin_basename")
        if len(rows) != 1 or len(rows[0]) != 1 or not rows[0][0]:
            raise BackupError("Cannot discover source binlog directory")
        basename = Path(rows[0][0])
        if not basename.is_absolute():
            raise BackupError("Source binlog path must be absolute; configure the host path explicitly")
        return basename.parent

    def current_time(self) -> dt.datetime:
        rows = self.query("SELECT DATE_FORMAT(UTC_TIMESTAMP(6), '%Y-%m-%dT%H:%i:%s.%fZ')")
        if len(rows) != 1 or len(rows[0]) != 1:
            raise BackupError("Unexpected source clock response")
        return parse_time(rows[0][0])

    def log_window(self, version: str) -> dict:
        covered = self.current_time()
        statement = "SHOW MASTER STATUS" if version.startswith("8.0.") else "SHOW BINARY LOG STATUS"
        rows = self.query(statement)
        if len(rows) != 1 or len(rows[0]) < 5:
            raise BackupError("Cannot obtain the source's committed binlog position")
        active, position = rows[0][0], int(rows[0][1])
        logs = [{"name": row[0], "size": int(row[1])} for row in self.query("SHOW BINARY LOGS")]
        names = [entry["name"] for entry in logs]
        if active not in names:
            raise BackupError("Binlogs rotated/purged during inspection; retry")
        logs = logs[:names.index(active) + 1]
        if not 4 <= position <= logs[-1]["size"]:
            raise BackupError("Invalid committed source binlog position")
        logs[-1]["size"] = position
        validate_log_chain(logs)
        return {"files": logs, "covered_until": timestamp(covered), "gtid_executed": rows[0][4]}

    def databases(self) -> list:
        grants = self.query("SHOW GRANTS")
        can_list_all = False
        for row in grants:
            match = re.match(r"GRANT ([A-Z_, ]+) ON \*\.\* TO ", row[0])
            if match and {"SHOW DATABASES", "ALL PRIVILEGES"}.intersection(
                privilege.strip() for privilege in match.group(1).split(",")
            ):
                can_list_all = True
        if not can_list_all:
            raise BackupError(
                "Full backup requires a directly granted global SHOW DATABASES privilege "
                "(or ALL PRIVILEGES); role-only grants are not supported"
            )
        names = [row[0] for row in self.query("SHOW DATABASES") if row[0] not in SYSTEM_DATABASES]
        if not names:
            raise BackupError("Source has no visible application databases")
        for name in names:
            quote_identifier(name)
            if name.startswith("-"):
                raise BackupError("Database names starting with '-' are not supported")
        unsupported = self.query(
            "SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE FROM information_schema.tables "
            "WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys') "
            "AND TABLE_TYPE='BASE TABLE' AND (ENGINE IS NULL OR ENGINE <> 'InnoDB')"
        )
        if unsupported:
            raise BackupError("Non-InnoDB tables are not supported: " + repr(unsupported))
        return names

    def dump(self, path: Path, databases: list, *, recovery: bool) -> None:
        command = [
            "mysqldump", "--defaults-file=" + str(self.options),
            "--single-transaction", "--quick", "--routines", "--events", "--triggers",
            "--hex-blob", "--no-tablespaces", "--default-character-set=utf8mb4",
            "--set-gtid-purged=" + ("ON" if recovery else "OFF"),
        ]
        if recovery:
            command.append("--source-data=2")
        command.extend(["--databases"] + databases)
        with path.open("xb") as stream:
            run(command, stdout=stream)
            stream.flush()
            os.fsync(stream.fileno())


def snapshot_coordinates(path: Path) -> dict:
    header = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("-- Current Database:"):
                break
            header.append(line)
    text = "".join(header)
    coordinate = re.search(
        r"-- CHANGE (?:REPLICATION SOURCE|MASTER) TO "
        r"(?:SOURCE|MASTER)_LOG_FILE='([^']+)', (?:SOURCE|MASTER)_LOG_POS=(\d+);", text,
    )
    gtid = re.search(r"SET @@GLOBAL\.GTID_PURGED=.*?'([^']*)';", text, re.DOTALL)
    if not coordinate or not gtid or not gtid.group(1).strip():
        raise BackupError("Snapshot is missing a binlog coordinate or GTID_PURGED set")
    name = coordinate.group(1)
    if not LOG_NAME.fullmatch(name) or int(coordinate.group(2)) < 4:
        raise BackupError("Snapshot has an invalid binlog coordinate")
    return {
        "binlog_file": name, "binlog_position": int(coordinate.group(2)),
        "gtid_executed": gtid.group(1).strip(),
    }


def validate_log_chain(entries: list) -> None:
    previous = None
    for entry in entries:
        match = LOG_NAME.fullmatch(entry["name"])
        if not match:
            raise BackupError("Unsafe or unsupported binlog filename: " + entry["name"])
        current = (match.group(1), int(match.group(2)))
        if previous and (current[0] != previous[0] or current[1] != previous[1] + 1):
            raise BackupError("Binlog sequence has a gap or changed its filename prefix")
        previous = current


def local_log(local: Path, name: str, size: int) -> Path:
    if not LOG_NAME.fullmatch(name) or size < 4:
        raise BackupError("Invalid binlog name/position")
    path = local / name
    if path.resolve().parent != local.resolve() or not path.is_file():
        raise BackupError("Required local binlog is missing: " + str(path))
    if path.stat().st_size < size:
        raise BackupError("Local binlog is shorter than the committed position: " + name)
    with path.open("rb") as stream:
        if stream.read(4) != b"\xfebin":
            raise BackupError("Not an unencrypted MySQL binlog: " + name)
    return path


def create_backup(source: Source, config: dict, directory: Path, local: Path, selected: list) -> Path:
    databases = source.databases()
    if not set(selected).issubset(databases):
        raise BackupError("Requested databases are not visible application databases")
    check_client("mysqldump", config["mysql_version"])
    with tempfile.TemporaryDirectory(prefix=".pending-", dir=directory) as temporary_name:
        temporary = Path(temporary_name)
        dump = temporary / "snapshot.sql"
        source.dump(dump, databases, recovery=True)
        coordinates = snapshot_coordinates(dump)
        completed = source.current_time()
        name = "full_" + completed.strftime("%Y%m%dT%H%M%S%fZ")
        dump_name = name + ".sql"
        dump.rename(temporary / dump_name)
        metadata = dict(config, **coordinates, format=1, completed_at=timestamp(completed),
                        databases=databases, dump_file=dump_name,
                        dump_sha256=sha256(temporary / dump_name))
        if selected:
            source.dump(temporary / "selected.sql", selected, recovery=False)
            metadata["selected_databases"] = selected
            metadata["selected_sha256"] = sha256(temporary / "selected.sql")
        anchor = local_log(local, coordinates["binlog_file"], coordinates["binlog_position"])
        metadata["anchor_sha256"] = binlog_digests(anchor, coordinates["binlog_position"])[0]
        atomic_json(temporary / "metadata.json", metadata)
        destination = directory / name
        if destination.exists():
            raise BackupError("Backup timestamp collision")
        os.rename(temporary, destination)
        # TemporaryDirectory tolerates its original path being renamed.
        return destination


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=3306)
    result.add_argument("--user", required=True)
    result.add_argument("--password", help="Prefer MYSQL_BACKUP_PASSWORD or --defaults-file")
    result.add_argument("--defaults-file", type=Path, help="Existing MySQL client option file")
    result.add_argument("--database", action="append", default=[],
                        help="Also create a selected.sql dump; the PITR snapshot remains full")
    result.add_argument("--backup-dir", required=True, type=Path)
    result.add_argument("--binlog-dir", required=True, type=Path,
                        help="Readable source datadir/binlog directory (local or bind mount)")
    result.add_argument("--role", choices=["primary", "replica"], default="primary")
    result.add_argument("--max-backups", type=int, default=0,
                        help="0: no cleanup (default); N>0: keep latest N")
    return result


@contextlib.contextmanager
def source_connection(args) -> Iterator[Source]:
    if not 1 <= args.port <= 65535:
        raise BackupError("Port must be between 1 and 65535")
    with tempfile.TemporaryDirectory(prefix="mysql-client-") as name:
        options = Path(name) / "client.cnf"
        if args.defaults_file:
            if getattr(args, "password", None) is not None:
                raise BackupError("--password and --defaults-file are mutually exclusive")
            original = args.defaults_file.expanduser().resolve(strict=True)
            if not original.is_file() or unsafe_permissions(original, 0o077):
                raise BackupError("--defaults-file must be a private file (chmod 600)")
            content = original.read_text(encoding="utf-8")
            # Explicit connection fields must not discard configured TLS settings.
            options.write_text(content + "\n[client]\nhost={}\nport={}\nuser={}\nprotocol=TCP\n".format(
                quote_option(args.host), args.port, quote_option(args.user),
            ), encoding="utf-8")
            os.chmod(options, 0o600)
        else:
            password = getattr(args, "password", None)
            if password is None:
                password = os.environ.get("MYSQL_BACKUP_PASSWORD")
            if password is None:
                if not sys.stdin.isatty():
                    raise BackupError("Provide MYSQL_BACKUP_PASSWORD or --defaults-file for cron")
                password = getpass.getpass("Source MySQL password: ")
            write_client_file(options, {
                "host": args.host, "port": args.port, "user": args.user, "password": password,
                "protocol": "TCP",
            })
        yield Source(options)


def execute(args) -> Path:
    from .retention import prune, validate_count

    limit = getattr(args, "max_backups", 0)
    validate_count(limit)
    directory = args.backup_dir.expanduser().resolve()
    local = args.binlog_dir.expanduser().resolve(strict=True)
    if not local.is_dir():
        raise BackupError("--binlog-dir must be a directory")
    if directory == local or directory in local.parents or local in directory.parents:
        raise BackupError("Backup and source binlog directories must be separate, non-nested directories")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    with backup_lock(directory), source_connection(args) as source:
        config = source.inspect(args.role)
        expected = getattr(args, "mysql_version", None)
        if expected and config["mysql_version"] != expected:
            raise BackupError("Configured MySQL version differs from the live source")
        check_client("mysql", config["mysql_version"])
        destination = create_backup(source, config, directory, local, args.database)
        print("Backup completed: " + str(destination))
        if limit > 0:
            for removed in prune(directory, limit):
                print("Deleted old full backup: " + str(removed))
        return destination


def main() -> int:
    args = parser().parse_args()
    os.umask(0o077)
    try:
        execute(args)
        return 0
    except (BackupError, OSError, ValueError, KeyError, TypeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ERROR: Backup interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
