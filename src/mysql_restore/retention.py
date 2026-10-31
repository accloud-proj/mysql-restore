"""Delete only validated, tool-owned full snapshots beyond a retention count."""

import datetime as dt
from pathlib import Path
import re

from .backup import BackupError, UTC, backup_lock, parse_time, read_json, sha256


def validate_count(value) -> int:
    if type(value) is not int or value < 0:
        raise BackupError("max_backups must be 0 (disabled) or a positive count")
    return value


def snapshots(directory: Path) -> list:
    result = []
    for folder in directory.glob("full_*"):
        if folder.is_symlink() or not folder.is_dir():
            raise BackupError("Refusing unsafe snapshot path: " + str(folder))
        match = re.fullmatch(r"full_(\d{8}T\d{12}Z)", folder.name)
        if not match:
            raise BackupError("Invalid full backup directory name: " + folder.name)
        completed = dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        metadata_path = folder / "metadata.json"
        if metadata_path.is_symlink():
            raise BackupError("Snapshot metadata cannot be a symlink")
        metadata = read_json(metadata_path)
        if parse_time(metadata["completed_at"]) != completed:
            raise BackupError("Backup filename timestamp does not match metadata")
        if metadata["dump_file"] != folder.name + ".sql":
            raise BackupError("Invalid snapshot SQL filename")
        dump = folder / metadata["dump_file"]
        if dump.is_symlink() or not dump.is_file():
            raise BackupError("Snapshot SQL is missing or unsafe: " + str(dump))
        result.append((completed, folder, metadata, dump))
    return sorted(result, key=lambda item: item[0], reverse=True)


def prune(directory: Path, count: int, *, dry_run: bool = False) -> list:
    """Caller holds backup_lock, shared with backup and recovery preparation."""
    validate_count(count)
    if count == 0:
        return []
    inventory = snapshots(directory)
    deleted = inventory[count:]
    # Preflight the whole deletion set before unlinking anything. Never recursively
    # delete folders containing unexpected files, subdirectories or symlinks.
    for _, folder, metadata, dump in deleted:
        allowed = {dump.name, "metadata.json"}
        if "selected_sha256" in metadata:
            allowed.add("selected.sql")
        children = list(folder.iterdir())
        if {child.name for child in children} != allowed or any(
            child.is_symlink() or not child.is_file() for child in children
        ):
            raise BackupError("Refusing cleanup of snapshot with unexpected contents: " + str(folder))
    if not dry_run and deleted:
        # Do not discard old backups if the backups we intend to retain are corrupt.
        for _, _, metadata, dump in inventory:
            if sha256(dump) != metadata["dump_sha256"]:
                raise BackupError("Snapshot SHA-256 verification failed: " + str(dump))
    paths = [folder for _, folder, _, _ in deleted]
    if not dry_run:
        for folder in paths:
            for child in folder.iterdir():
                child.unlink()
            folder.rmdir()
    return paths


def cleanup(directory: Path, count: int, *, dry_run: bool = False) -> list:
    directory = directory.expanduser().resolve(strict=True)
    with backup_lock(directory):
        return prune(directory, count, dry_run=dry_run)
