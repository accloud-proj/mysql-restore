#!/usr/bin/env python3
"""Restore a GTID snapshot in an isolated Docker container and export a PITR dump."""

import argparse
from contextlib import contextmanager
import datetime as dt
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
from typing import BinaryIO, Iterable, Iterator, Optional

from .backup import (
    BackupError, SYSTEM_DATABASES, UTC, parse_time, quote_identifier, read_json,
    run, sha256, supported_version, validate_log_chain, write_client_file, local_log,
    check_client,
)


GTID_HEADER = re.compile(r"^#\d{6}\s+\d{1,2}:\d{2}:\d{2}.*\b(?:GTID|Anonymous_GTID)\b")
COMMIT_TIME = re.compile(r"\bimmediate_commit_timestamp=(\d+)")
AT_POSITION = re.compile(r"^# at (\d+)$")
PROGRESS_INTERVAL = 5.0


def progress_log(message: str) -> None:
    print("[{}] {}".format(dt.datetime.now().astimezone().isoformat(timespec="seconds"), message),
          file=sys.stderr, flush=True)


@contextmanager
def progress_step(label: str, *, stream: Optional[BinaryIO] = None,
                  total: Optional[int] = None, reading: bool = False) -> Iterator[None]:
    started = time.monotonic()
    stopped = threading.Event()
    errors = []

    def status() -> str:
        detail = "{:.1f}s elapsed".format(time.monotonic() - started)
        if stream is not None:
            descriptor = stream.fileno()
            count = os.lseek(descriptor, 0, os.SEEK_CUR) if reading else os.fstat(descriptor).st_size
            detail += "; {} {:.1f} MiB".format("SQL input read" if reading else "written", count / 1048576)
            if total is not None:
                percentage = min(100, count * 100 / total) if total else 100
                detail += "/{:.1f} MiB ({:.1f}%)".format(total / 1048576, percentage)
        return detail

    def monitor() -> None:
        while not stopped.wait(PROGRESS_INTERVAL):
            try:
                progress_log("RUNNING: {} ({})".format(label, status()))
            except OSError as exc:
                errors.append(exc)
                progress_log("ERROR: Progress monitoring failed: " + str(exc))
                return

    progress_log("START: " + label)
    worker = threading.Thread(target=monitor, name="restore-progress", daemon=True)
    worker.start()
    try:
        yield
    except BaseException:
        stopped.set()
        worker.join()
        progress_log("FAILED: {} ({:.1f}s elapsed)".format(label, time.monotonic() - started))
        raise
    else:
        stopped.set()
        worker.join()
        if errors:
            raise errors[0]
        progress_log("DONE: {} ({})".format(label, status()))


def select_snapshot(directory: Path, target: dt.datetime, version: str) -> tuple:
    from .retention import snapshots

    candidates = []
    for named_time, folder, metadata, _ in snapshots(directory):
        if named_time <= target and metadata["mysql_version"] == version:
            candidates.append((named_time, folder, metadata))
    if not candidates:
        raise BackupError("No completed snapshot at or before target with the exact MySQL version")
    _, folder, metadata = max(candidates, key=lambda entry: entry[0])
    dump = folder / metadata["dump_file"]
    if dump.name != folder.name + ".sql" or dump.parent != folder:
        raise BackupError("Invalid snapshot SQL filename")
    if not dump.is_file() or sha256(dump) != metadata["dump_sha256"]:
        raise BackupError("Snapshot SQL is missing or failed SHA-256 verification")
    return folder, metadata, dump


def select_logs(local: Path, metadata: dict, target: dt.datetime, window: dict,
                index: Optional[Path] = None) -> list:
    if parse_time(window["covered_until"]) < target:
        raise BackupError("Target is newer than the source's inspected committed log coverage")
    entries = window["files"]
    validate_log_chain(entries)
    positions = [index for index, entry in enumerate(entries)
                 if entry["name"] == metadata["binlog_file"]]
    if len(positions) != 1:
        raise BackupError("Snapshot's starting binlog was purged from the source")
    selected = [dict(entry) for entry in entries[positions[0]:]]
    if index is not None:
        # Server paths in the index may differ from host bind-mount paths; use basenames.
        names = [Path(line.strip()).name for line in index.read_text().splitlines() if line.strip()]
        expected = [entry["name"] for entry in selected]
        if any(names.count(name) != 1 for name in expected):
            raise BackupError("Configured binlog index does not contain the required log chain")
        offsets = [names.index(name) for name in expected]
        if offsets != list(range(offsets[0], offsets[0] + len(offsets))):
            raise BackupError("Configured binlog index has a gap or incorrect order")
    for number, entry in enumerate(selected, 1):
        with progress_step("Hashing binlog {}/{}: {}".format(number, len(selected), entry["name"])):
            path = local_log(local, entry["name"], entry["size"])
            entry["sha256"] = sha256(path, entry["size"])
    if not 4 <= metadata["binlog_position"] <= selected[0]["size"]:
        raise BackupError("Snapshot binlog position is outside the source file")
    if "anchor_sha256" not in metadata:
        raise BackupError("This snapshot predates direct-binlog validation; create a new full backup")
    anchor = local_log(local, metadata["binlog_file"], metadata["binlog_position"])
    if sha256(anchor, metadata["binlog_position"]) != metadata["anchor_sha256"]:
        raise BackupError("Starting binlog was rewritten or belongs to a different instance")
    return selected


def verify_logs(local: Path, entries: list) -> None:
    for number, entry in enumerate(entries, 1):
        with progress_step("Verifying binlog {}/{}: {}".format(number, len(entries), entry["name"])):
            path = local_log(local, entry["name"], entry["size"])
            if sha256(path, entry["size"]) != entry["sha256"]:
                raise BackupError("Source binlog changed during recovery: " + entry["name"])


def commit_boundaries(lines, *, initial_position: int = 4):
    """Yield GTID start positions and immediate-source commit timestamps."""
    position = None
    pending = False
    parsed = False
    for raw in lines:
        line = raw.rstrip("\r\n")
        match = AT_POSITION.fullmatch(line)
        if match:
            if pending and not parsed:
                raise BackupError("GTID event has no immediate_commit_timestamp")
            position = int(match.group(1))
            pending = False
            parsed = False
            continue
        if GTID_HEADER.match(line):
            if "Anonymous_GTID" in line:
                raise BackupError("Anonymous transactions cannot be used for GTID PITR")
            if position is None or position < initial_position:
                raise BackupError("Invalid GTID event start position")
            pending = True
            parsed = False
        if pending and not parsed and line.startswith("#"):
            match = COMMIT_TIME.search(line)
            if match:
                value = int(match.group(1))
                if value <= 0:
                    raise BackupError("GTID commit timestamp is unavailable")
                parsed = True
                yield position, value
    if pending and not parsed:
        raise BackupError("GTID event has no immediate_commit_timestamp")


def plan_replay(events: Iterable[tuple], target_microseconds: int) -> Optional[tuple]:
    cutoff = None
    for filename, position, committed in events:
        if committed > target_microseconds and cutoff is None:
            cutoff = (filename, position)
        elif committed <= target_microseconds and cutoff is not None:
            raise BackupError("Commit timestamps cross target out of order; safe PITR is impossible")
    return cutoff


class Recovery:
    def __init__(self, work: Path, version: str, metadata: dict, binlog_dir: Optional[Path] = None):
        suffix = uuid.uuid4().hex
        self.name = "mysql-pitr-" + suffix
        self.volume = self.name + "-data"
        self.work = work
        self.version = version
        self.metadata = metadata
        self.binlog_dir = binlog_dir
        self.container_created = False
        self.volume_created = False
        self.binlog_in_container = False

    def start(self, timeout: int) -> None:
        image = self.ensure_image()
        password = secrets.token_urlsafe(36)
        (self.work / "root-password").write_text(password, encoding="utf-8")
        write_client_file(self.work / "client.cnf", {
            "user": "root", "password": password, "protocol": "SOCKET",
        })
        run(["docker", "volume", "create", self.volume])
        self.volume_created = True
        # Mark before run: Docker may create the container even if starting it fails.
        self.container_created = True
        mounts = []
        if self.binlog_dir is not None:
            mounts = ["--mount", "type=bind,source={},target=/source-binlogs,readonly".format(
                self.binlog_dir,
            )]
        run([
            "docker", "run", "--detach", "--name", self.name, "--network", "none",
            "--mount", "type=volume,source={},target=/var/lib/mysql".format(self.volume),
            "--mount", "type=bind,source={},target=/work,readonly".format(self.work),
        ] + mounts + [
            "--env", "MYSQL_ROOT_PASSWORD_FILE=/work/root-password",
            image,
            "--server-id=2147483647", "--log-bin=restore-bin", "--binlog-format=ROW",
            "--gtid-mode=ON", "--enforce-gtid-consistency=ON",
            "--event-scheduler=DISABLED", "--skip-networking", "--local-infile=OFF",
            "--lower-case-table-names=" + str(self.metadata["lower_case_table_names"]),
            "--character-set-server=" + self.metadata["character_set_server"],
            "--collation-server=" + self.metadata["collation_server"],
            "--sql-mode=" + self.metadata["sql_mode"],
        ])
        progress_log("Container started; waiting for MySQL readiness (timeout {}s)".format(timeout))
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                # The entrypoint's temporary initialization server also accepts SQL.
                process = run(["docker", "exec", self.name, "cat", "/proc/1/comm"]).strip()
                if process != "mysqld":
                    raise BackupError("Official image initialization is still running (PID 1: {})".format(process))
                actual = self.query("SELECT @@version").strip()
            except BackupError as exc:
                last_error = str(exc)
                state = run(["docker", "inspect", "--format", "{{.State.Running}}", self.name]).strip()
                if state != "true":
                    logs = run(["docker", "logs", self.name], include_stderr=True)
                    raise BackupError("Recovery container exited:\n" + logs) from exc
                time.sleep(1)
                continue
            if actual != self.version:
                raise BackupError("Recovery image version differs from the source: " + actual)
            self.configure_binlog_tool()
            progress_log("Final MySQL server is ready")
            return
        logs = run(["docker", "logs", "--tail", "80", self.name], include_stderr=True)
        raise BackupError("Recovery MySQL readiness timed out: " + last_error + "\nContainer logs:\n" + logs)

    def ensure_image(self) -> str:
        return "mysql:" + self.version

    def configure_binlog_tool(self) -> None:
        presence = run([
            "docker", "exec", self.name, "sh", "-c",
            "if command -v mysqlbinlog >/dev/null 2>&1; then printf present; else printf absent; fi",
        ]).strip()
        if presence == "present":
            actual = run(["docker", "exec", self.name, "mysqlbinlog", "--version"])
            distribution = re.search(r"\b(?:Distrib|based on MySQL server)\s+(\d+\.\d+\.\d+)", actual)
            if distribution is None:
                distribution = re.search(r"\bVer\s+(\d+\.\d+\.\d+)", actual)
            if distribution is None or distribution.group(1) != self.version:
                raise BackupError("Container mysqlbinlog version mismatch: " + actual.strip())
            self.binlog_in_container = True
            progress_log("Using official image mysqlbinlog")
        elif presence == "absent":
            check_client("mysqlbinlog", self.version)
            self.binlog_in_container = False
            progress_log("Official image has no mysqlbinlog; using matching host mysqlbinlog")
        else:
            raise BackupError("Unexpected mysqlbinlog availability response: " + presence)

    def mysql_command(self) -> list:
        return [
            "docker", "exec", "-i", self.name, "mysql",
            "--defaults-file=/work/client.cnf", "--no-login-paths", "--binary-mode",
            "--batch", "--raw", "--skip-column-names",
        ]

    def query(self, sql: str) -> str:
        return run(self.mysql_command() + ["--execute", sql])

    def import_sql(self, path: Path) -> None:
        with path.open("rb") as stream:
            with progress_step("Importing " + path.name, stream=stream,
                               total=os.fstat(stream.fileno()).st_size, reading=True):
                run(self.mysql_command(), stdin=stream)

    def binlog_command(self, name: str, position: int, *, decode: bool,
                       stop: Optional[int] = None) -> list:
        if self.binlog_in_container:
            command = ["docker", "exec", self.name, "mysqlbinlog"]
            path = "/source-binlogs/" + name
        else:
            if self.binlog_dir is None:
                raise BackupError("Host mysqlbinlog requires a local binlog directory")
            command = ["mysqlbinlog", "--no-defaults"]
            path = str(self.binlog_dir / name)
        command += [
            "--verify-binlog-checksum",
            "--start-position=" + str(position),
        ]
        if decode:
            command.extend(["--base64-output=DECODE-ROWS", "--verbose"])
        if stop is not None:
            command.append("--stop-position=" + str(stop))
        command.append(path)
        return command

    def find_cutoff(self, entries: list, start: int, target: dt.datetime) -> Optional[tuple]:
        epoch = dt.datetime(1970, 1, 1, tzinfo=UTC)
        delta = target - epoch
        target_us = ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds)

        def events():
            for index, entry in enumerate(entries):
                position = start if index == 0 else 4
                if position == entry["size"]:
                    continue
                # Decode to disk rather than RAM; validate the process before trusting output.
                decoded = self.work / "decoded.txt"
                with decoded.open("wb") as stream:
                    with progress_step("Decoding binlog {}/{}: {}".format(
                            index + 1, len(entries), entry["name"]), stream=stream):
                        run(self.binlog_command(entry["name"], position, decode=True, stop=entry["size"]),
                            stdout=stream)
                        stream.flush()
                with progress_step("Scanning transaction timestamps: " + entry["name"]):
                    with decoded.open(encoding="utf-8", errors="surrogateescape") as stream:
                        for event_pos, committed in commit_boundaries(stream, initial_position=position):
                            yield entry["name"], event_pos, committed
                decoded.unlink()

        return plan_replay(events(), target_us)

    def replay(self, entries: list, start: int, cutoff: Optional[tuple]) -> None:
        replay = self.work / "replay.sql"
        with replay.open("wb") as stream:
            for index, entry in enumerate(entries):
                position = start if index == 0 else 4
                at_cutoff = cutoff is not None and cutoff[0] == entry["name"]
                stop = cutoff[1] if at_cutoff else entry["size"]
                if stop == position:
                    break
                with progress_step("Preparing replay {}/{}: {} (positions {}-{})".format(
                        index + 1, len(entries), entry["name"], position, stop), stream=stream):
                    run(self.binlog_command(entry["name"], position, decode=False, stop=stop),
                        stdout=stream)
                    stream.flush()
                if at_cutoff:
                    break
        if replay.stat().st_size:
            self.import_sql(replay)
        else:
            progress_log("No binlog transactions need replay at the requested target")
        replay.unlink()

    def export(self, output: Path, databases: list, tables: list) -> None:
        available = [line for line in self.query("SHOW DATABASES").splitlines()
                     if line not in SYSTEM_DATABASES]
        selected = databases or available
        if not selected or not set(selected).issubset(available):
            raise BackupError("Requested databases do not exist at the recovery target")
        for name in selected + tables:
            quote_identifier(name)
            if name.startswith("-"):
                raise BackupError("Database/table names starting with '-' are not supported")
        if tables:
            if len(selected) != 1:
                raise BackupError("--table requires exactly one --database")
            available_tables = self.query("SHOW TABLES FROM " + quote_identifier(selected[0])).splitlines()
            for table in tables:
                if table not in available_tables:
                    raise BackupError("Table does not exist at target: " + table)
        command = [
            "docker", "exec", self.name, "mysqldump", "--defaults-file=/work/client.cnf",
            "--no-login-paths",
            "--single-transaction", "--quick", "--hex-blob", "--no-tablespaces",
            "--triggers", "--set-gtid-purged=OFF", "--default-character-set=utf8mb4",
        ]
        if tables:
            command.extend(selected + tables)
        else:
            command.extend(["--routines", "--events", "--databases"] + selected)
        descriptor, name = tempfile.mkstemp(prefix=".mysql-export-", dir=output.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                if tables:
                    definition = self.query("SHOW CREATE DATABASE " + quote_identifier(selected[0]))
                    parts = definition.rstrip("\n").split("\t", 1)
                    if len(parts) != 2:
                        raise BackupError("Unexpected database definition response")
                    stream.write((parts[1] + ";\nUSE " + quote_identifier(selected[0]) + ";\n").encode("utf-8"))
                with progress_step("Exporting recovered SQL", stream=stream):
                    run(command, stdout=stream)
                    stream.flush()
                os.fsync(stream.fileno())
            # Atomic, no-clobber publication, even if another process creates output meanwhile.
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)

    def cleanup(self) -> None:
        errors = []
        if self.container_created:
            try:
                run(["docker", "rm", "--force", self.name])
            except BackupError as exc:
                errors.append(str(exc))
        if self.volume_created:
            try:
                run(["docker", "volume", "rm", self.volume])
            except BackupError as exc:
                errors.append(str(exc))
        if errors:
            raise BackupError("Cleanup failed; remove these specific resources manually: "
                              + self.name + ", " + self.volume + "\n" + "\n".join(errors))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--mysql-version", required=True, help="Exact source patch version, e.g. 8.4.7")
    result.add_argument("--target-time", required=True, help="ISO 8601 time with Z or UTC offset")
    result.add_argument("--backup-dir", required=True, type=Path)
    result.add_argument("--binlog-dir", required=True, type=Path)
    result.add_argument("--binlog-index", type=Path)
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=3306)
    result.add_argument("--user", required=True)
    result.add_argument("--defaults-file", required=True, type=Path)
    result.add_argument("--role", choices=["primary", "replica"], default="primary")
    result.add_argument("--output", required=True, type=Path, help="New SQL file; never overwritten")
    result.add_argument("--database", action="append", default=[])
    result.add_argument("--table", action="append", default=[])
    result.add_argument("--startup-timeout", type=int, default=180)
    return result


def execute(args) -> None:
    from .backup import backup_lock

    # Protect snapshot selection/copy from automatic or standalone retention cleanup.
    started = time.monotonic()
    progress_log("Recovery requested: target={}, output={}".format(args.target_time, args.output))
    progress_log("Acquiring backup directory lock")
    with backup_lock(args.backup_dir.expanduser().resolve(strict=True)):
        _execute(args)
    progress_log("Recovery finished ({:.1f}s total)".format(time.monotonic() - started))


def _execute(args) -> None:
    from .backup import check_client, source_connection

    version = supported_version(args.mysql_version)
    target = parse_time(args.target_time)
    if args.startup_timeout <= 0:
        raise BackupError("--startup-timeout must be positive")
    if args.table and len(args.database) != 1:
        raise BackupError("--table requires exactly one --database")
    directory = args.backup_dir.expanduser().resolve(strict=True)
    local = args.binlog_dir.expanduser().resolve(strict=True)
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise BackupError("Output already exists; refusing to overwrite it")
    if not output.parent.is_dir():
        raise BackupError("Output parent directory does not exist")
    with progress_step("[1/9] Selecting and verifying full backup"):
        folder, metadata, dump = select_snapshot(directory, target, version)
    progress_log("Using snapshot: " + str(folder))
    with progress_step("[2/9] Checking live source and binlog coverage"):
        with source_connection(args) as source:
            check_client("mysql", version)
            config = source.inspect(args.role)
            for key in ("source_uuid", "mysql_version"):
                if metadata[key] != config[key]:
                    raise BackupError("Snapshot and live source belong to different instances/versions")
            window = source.log_window(version)
        entries = select_logs(local, metadata, target, window, args.binlog_index)
        run(["docker", "info", "--format", "{{.ServerVersion}}"])
    progress_log("Binlogs selected: {} files, {:.1f} MiB inspected".format(
        len(entries), sum(entry["size"] for entry in entries) / 1048576,
    ))
    with tempfile.TemporaryDirectory(prefix="mysql-pitr-") as name:
        work = Path(name)
        snapshot = work / dump.name
        with dump.open("rb") as source, snapshot.open("wb") as destination:
            with progress_step("[3/9] Copying full backup", stream=destination,
                               total=os.fstat(source.fileno()).st_size):
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
        with progress_step("Verifying prepared full backup"):
            if sha256(snapshot) != metadata["dump_sha256"]:
                raise BackupError("Snapshot changed during preparation")
        recovery = Recovery(work, version, metadata, local)
        primary_error = None
        try:
            with progress_step("[4/9] Starting mysql:{} (Docker may pull the image)".format(version)):
                recovery.start(args.startup_timeout)
            with progress_step("[5/9] Locating target transaction boundary"):
                cutoff = recovery.find_cutoff(entries, metadata["binlog_position"], target)
                verify_logs(local, entries)
            progress_log("Replay boundary: " + (str(cutoff) if cutoff else "inspected committed log position"))
            with progress_step("[6/9] Restoring full backup"):
                recovery.import_sql(snapshot)
            with progress_step("[7/9] Replaying binlog transactions"):
                recovery.replay(entries, metadata["binlog_position"], cutoff)
            with progress_step("[8/9] Verifying binlogs and exporting recovered data"):
                verify_logs(local, entries)
                recovery.export(output, args.database, args.table)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                with progress_step("[9/9] Removing temporary container and data volume"):
                    recovery.cleanup()
            except BackupError as exc:
                if primary_error is None:
                    raise
                print("ERROR: " + str(exc), file=sys.stderr)
    print("Recovery export completed: " + str(output), flush=True)


def main() -> int:
    args = parser().parse_args()
    args.password = None
    os.umask(0o077)
    try:
        execute(args)
        return 0
    except (BackupError, OSError, ValueError, KeyError, TypeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ERROR: Recovery interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
