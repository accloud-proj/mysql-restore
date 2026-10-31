"""Private JSON configuration and interactive setup."""

import argparse
from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Optional

from .backup import BackupError, quote_option, read_json, supported_version, write_client_file


def validate_schedule(value: str) -> str:
    fields = value.split()
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    if len(fields) != 5:
        raise BackupError("Cron schedule must contain five numeric fields")
    for field, (low, high) in zip(fields, bounds):
        for part in field.split(","):
            match = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", part)
            if not match:
                raise BackupError("Invalid cron field: " + field)
            base, step = match.groups()
            if step is not None and not 1 <= int(step) <= high - low + 1:
                raise BackupError("Invalid cron step: " + part)
            if base != "*":
                numbers = [int(number) for number in base.split("-")]
                if any(number < low or number > high for number in numbers) or numbers != sorted(numbers):
                    raise BackupError("Cron field outside allowed range: " + part)
    return " ".join(fields)


def path_value(base: Path, value: str) -> Path:
    if not value or any(character in value for character in "\x00\r\n"):
        raise BackupError("Configuration paths cannot be empty or contain NUL/newlines")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


@dataclass(frozen=True)
class Config:
    path: Path
    host: str
    port: int
    user: str
    defaults_file: Path
    mysql_version: str
    role: str
    backup_dir: Path
    binlog_dir: Path
    binlog_index: Optional[Path]
    schedule: str
    log_file: Path
    max_backups: int = 0

    def arguments(self) -> argparse.Namespace:
        return argparse.Namespace(
            host=self.host, port=self.port, user=self.user, defaults_file=self.defaults_file,
            mysql_version=self.mysql_version, role=self.role, backup_dir=self.backup_dir,
            binlog_dir=self.binlog_dir, binlog_index=self.binlog_index, database=[], password=None,
            max_backups=self.max_backups,
        )


def parse_config(path: Path, data: dict) -> Config:
    required = {
        "format", "host", "port", "user", "defaults_file", "mysql_version", "role",
        "backup_dir", "binlog_dir", "schedule", "log_file",
    }
    if missing := required - data.keys():
        raise BackupError("Missing configuration fields: " + ", ".join(sorted(missing)))
    if extra := data.keys() - required - {"binlog_index", "max_backups"}:
        raise BackupError("Unknown configuration fields: " + ", ".join(sorted(extra)))
    if type(data["format"]) is not int or data["format"] != 1:
        raise BackupError("Unsupported configuration format")
    for key in required - {"format", "port"}:
        if not isinstance(data[key], str) or not data[key]:
            raise BackupError("Configuration field must be a nonempty string: " + key)
    if type(data["port"]) is not int or not 1 <= data["port"] <= 65535:
        raise BackupError("Configuration port must be an integer from 1 to 65535")
    if data["role"] not in {"primary", "replica"}:
        raise BackupError("role must be primary or replica")
    quote_option(data["host"])
    quote_option(data["user"])
    version = supported_version(data["mysql_version"])
    limit = data.get("max_backups", 0)
    if limit is None:
        limit = 0
    from .retention import validate_count
    validate_count(limit)
    base = path.parent
    index = data.get("binlog_index")
    if index is not None and not isinstance(index, str):
        raise BackupError("binlog_index must be a path string or null")
    backup_dir = path_value(base, data["backup_dir"])
    binlog_dir = path_value(base, data["binlog_dir"])
    if backup_dir == binlog_dir or backup_dir in binlog_dir.parents or binlog_dir in backup_dir.parents:
        raise BackupError("Backup and source binlog directories must be separate and non-nested")
    # Docker --mount uses comma-separated fields.
    if "," in str(binlog_dir):
        raise BackupError("Docker binlog mount paths cannot contain commas")
    return Config(
        path=path, host=data["host"], port=data["port"], user=data["user"],
        defaults_file=path_value(base, data["defaults_file"]), mysql_version=version, role=data["role"],
        backup_dir=backup_dir, binlog_dir=binlog_dir,
        binlog_index=path_value(base, index) if index else None,
        schedule=validate_schedule(data["schedule"]), log_file=path_value(base, data["log_file"]),
        max_backups=limit,
    )


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve(strict=True)
    if path.stat().st_mode & 0o022:
        raise BackupError("Configuration must not be writable by group/others")
    return parse_config(path, read_json(path))


def publish_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Publish without overwriting any existing path, including symlinks."""
    descriptor, name = tempfile.mkstemp(prefix=".mysql-setup-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(path: Path, defaults_file: Optional[Path] = None) -> Config:
    if not sys.stdin.isatty():
        raise BackupError("init requires an interactive terminal; alternatively copy config.example.json")
    path = path.expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise BackupError("Configuration already exists; edit it or select another --config")
    if not path.parent.is_dir():
        raise BackupError("Configuration parent directory does not exist")

    def prompt(label: str, default: str = "") -> str:
        response = input("{}{}: ".format(label, " [" + default + "]" if default else "")).strip()
        return response or default

    data = {
        "format": 1,
        "host": prompt("Source host", "127.0.0.1"),
        "port": int(prompt("Source port", "3306")),
        "user": prompt("Backup user", "backup"),
        "mysql_version": prompt("Exact MySQL patch version", "8.4.7"),
        "role": prompt("Source role (primary/replica)", "primary"),
        "backup_dir": prompt("Full backup directory", "./backups"),
        "binlog_dir": prompt("Local source binlog directory (not relay logs)", "/var/lib/mysql"),
        "binlog_index": prompt("Optional source binlog .index file (empty = query source)") or None,
        "schedule": prompt("Full backup cron schedule", "0 2 * * *"),
        "log_file": prompt("Backup cron log file", "./mysql-backup.log"),
    }
    limit = prompt("Maximum full backups (0 = no cleanup)", "0")
    data["max_backups"] = int(limit)
    client = defaults_file.expanduser().resolve(strict=True) if defaults_file else path.with_suffix(".client.cnf")
    data["defaults_file"] = str(client)
    config = parse_config(path, data)
    created_client = False
    try:
        if defaults_file:
            if not client.is_file() or client.stat().st_mode & 0o077:
                raise BackupError("Existing MySQL client file must have mode 600")
        else:
            if client.exists() or client.is_symlink():
                raise BackupError("Client credential file already exists: " + str(client))
            password = getpass.getpass("Source password (hidden): ")
            # Stage the secret then publish without races or overwriting an existing file.
            with tempfile.TemporaryDirectory(prefix="mysql-secret-") as directory:
                temporary = Path(directory) / "client.cnf"
                write_client_file(temporary, {"password": password})
                publish_text(client, temporary.read_text())
            created_client = True
        publish_text(path, json.dumps(data, indent=2) + "\n")
    except BaseException:
        if created_client:
            client.unlink()
        raise
    return config
