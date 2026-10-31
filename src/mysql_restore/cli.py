"""Configuration-driven MySQL backup and recovery commands."""

import argparse
import os
from pathlib import Path
import sys

from . import backup, cron, restore
from .backup import BackupError
from .config import initialize, load_config, publish_text


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Mysqldump GTID backup and direct-binlog Docker PITR")
    result.add_argument("--config", type=Path, default=Path("mysql-restore.json"))
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Interactively generate private configuration")
    init.add_argument("--defaults-file", type=Path, help="Reuse a private MySQL client option file")
    commands.add_parser("check", help="Check source settings, clients and local binlog accessibility")
    full = commands.add_parser("backup", help="Create a mysqldump full GTID snapshot")
    full.add_argument("--database", action="append", default=[], help="Also produce selected.sql")
    full.add_argument("--max-backups", type=int, help="Override retention: 0 disabled, N>0 latest N")
    clean = commands.add_parser("cleanup", help="Delete old full snapshots only; never delete source binlogs")
    clean.add_argument("--max-backups", type=int, help="Override retention: 0 disabled, N>0 latest N")
    clean.add_argument("--dry-run", action="store_true", help="List deletion candidates without deleting")
    recover = commands.add_parser("restore", help="Recover using the same local source binlogs")
    recover.add_argument("--target-time", required=True)
    recover.add_argument("--output", required=True, type=Path)
    recover.add_argument("--database", action="append", default=[])
    recover.add_argument("--table", action="append", default=[])
    recover.add_argument("--startup-timeout", type=int, default=180)
    schedule = commands.add_parser("cron", help="Preview/export/install the full-backup crontab entry")
    action = schedule.add_mutually_exclusive_group()
    action.add_argument("--output", type=Path, help="Write a new crontab fragment (never overwrite)")
    action.add_argument("--install", action="store_true", help="Install/update current user's crontab")
    schedule.add_argument("--script", type=Path, help="Generate a new executable backup shell script")
    schedule.add_argument("--yes", action="store_true", help="Confirm --install without prompting")
    return result


def main() -> int:
    args = parser().parse_args()
    os.umask(0o077)
    try:
        if args.command == "init":
            config = initialize(args.config, args.defaults_file)
            print("Configuration created: " + str(config.path))
            print("Next: mysql-restore --config {} check".format(config.path))
            return 0
        config = load_config(args.config)
        options = config.arguments()
        if args.command == "check":
            with backup.source_connection(options) as source:
                settings = source.inspect(config.role)
                if settings["mysql_version"] != config.mysql_version:
                    raise BackupError("Configured MySQL version differs from source")
                backup.check_client("mysql", config.mysql_version)
                backup.check_client("mysqldump", config.mysql_version)
                names = source.databases()
                window = source.log_window(config.mysql_version)
                for entry in window["files"]:
                    backup.local_log(config.binlog_dir, entry["name"], entry["size"])
            if config.binlog_index and not config.binlog_index.is_file():
                raise BackupError("Configured binlog index is not a readable file")
            print("Source check passed: {} {}, {} application databases".format(
                config.role, config.mysql_version, len(names),
            ))
            print("Committed local log coverage: " + window["covered_until"])
        elif args.command == "backup":
            options.database = args.database
            if args.max_backups is not None:
                options.max_backups = args.max_backups
            backup.execute(options)
        elif args.command == "cleanup":
            from .retention import cleanup
            limit = args.max_backups if args.max_backups is not None else config.max_backups
            if limit == 0:
                print("Cleanup disabled (max_backups=0)")
                return 0
            removed = cleanup(config.backup_dir, limit, dry_run=args.dry_run)
            for path in removed:
                print(("Would delete: " if args.dry_run else "Deleted: ") + str(path))
            print("{} old full backups {}".format(len(removed), "selected" if args.dry_run else "deleted"))
        elif args.command == "restore":
            options.target_time = args.target_time
            options.output = args.output
            options.database = args.database
            options.table = args.table
            options.startup_timeout = args.startup_timeout
            restore.execute(options)
        else:
            if args.yes and not args.install:
                raise BackupError("--yes requires --install")
            script = args.script.expanduser().absolute() if args.script else None
            if script:
                publish_text(script, cron.script_text(config), mode=0o700)
            block = cron.render(config, script)
            print(block, end="")
            if args.output:
                publish_text(args.output.expanduser().absolute(), block)
                print("Cron fragment written: " + str(args.output))
            if args.install:
                if not args.yes:
                    if not sys.stdin.isatty():
                        raise BackupError("Non-interactive cron installation requires --yes")
                    if input("Install this entry into your current user's crontab? [y/N]: ").lower() != "y":
                        print("Crontab unchanged")
                        return 0
                changed = cron.install(block)
                print("Crontab updated" if changed else "Crontab already up to date")
        return 0
    except (BackupError, OSError, ValueError, KeyError, TypeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("ERROR: Operation interrupted", file=sys.stderr)
        return 130
