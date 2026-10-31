"""Preview, export or install one managed full-backup cron entry."""

import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .backup import BackupError
from .config import Config


def backup_command(config: Config) -> str:
    if os.name == "nt":
        raise BackupError("cron is unavailable on Windows; schedule the backup command with Task Scheduler")
    if not config.log_file.parent.is_dir():
        raise BackupError("Cron log parent directory must exist")
    frozen = getattr(sys, "frozen", False)
    executable = [os.path.abspath(sys.executable)]
    if not frozen:
        executable += ["-m", "mysql_restore"]
    command = shlex.join(executable + ["--config", str(config.path), "backup"])
    # Ensure source checkout/editable installs work independent of cron's working directory.
    source_dir = config.path.parent if frozen else Path(__file__).resolve().parents[1]
    command = "export PATH=" + shlex.quote(os.environ.get("PATH", "/usr/bin:/bin")) + "; cd " \
        + shlex.quote(str(source_dir)) + " && " + command
    command += " >> " + shlex.quote(str(config.log_file)) + " 2>&1"
    if any(character in command for character in "\x00\r\n"):
        raise BackupError("Cron command paths cannot contain NUL or newlines")
    return command


def render(config: Config, script: Optional[Path] = None) -> str:
    if os.name == "nt":
        raise BackupError("cron is unavailable on Windows; schedule the backup command with Task Scheduler")
    identifier = hashlib.sha256(str(config.path).encode()).hexdigest()[:16]
    command = shlex.quote(str(script)) if script is not None else backup_command(config)
    if any(character in command for character in "\x00\r\n"):
        raise BackupError("Cron script path cannot contain NUL or newlines")
    command = command.replace("%", r"\%")
    return (
        "# BEGIN mysql-restore " + identifier + "\n"
        + config.schedule + " " + command + "\n"
        + "# END mysql-restore " + identifier + "\n"
    )


def script_text(config: Config) -> str:
    return "#!/bin/sh\nset -eu\n" + backup_command(config) + "\n"


def merge(existing: str, block: str) -> str:
    lines = block.splitlines()
    begin, end = lines[0], lines[-1]
    original = existing.splitlines(keepends=True)
    starts = [index for index, line in enumerate(original) if line.rstrip("\r\n") == begin]
    ends = [index for index, line in enumerate(original) if line.rstrip("\r\n") == end]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise BackupError("Existing managed crontab block is malformed; repair manually")
        return "".join(original[:starts[0]]) + block + "".join(original[ends[0] + 1:])
    return existing + ("\n" if existing and not existing.endswith("\n") else "") + block


def read_crontab() -> str:
    env = dict(os.environ, LC_ALL="C")
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, env=env)
    if result.returncode == 0:
        return result.stdout
    error = result.stderr.strip()
    if result.returncode == 1 and error.startswith("no crontab for "):
        return ""
    raise BackupError("Cannot read crontab: " + error)


def install(block: str) -> bool:
    existing = read_crontab()
    updated = merge(existing, block)
    if updated == existing:
        return False
    # Refuse to overwrite a concurrent manual/other-tool edit detected before publication.
    if read_crontab() != existing:
        raise BackupError("Crontab changed concurrently; retry installation")
    result = subprocess.run(["crontab", "-"], input=updated, capture_output=True, text=True,
                            env=dict(os.environ, LC_ALL="C"))
    if result.returncode:
        raise BackupError("Cannot install crontab: " + result.stderr.strip())
    if read_crontab() != updated:
        raise BackupError("Installed crontab did not match the requested content")
    return True
