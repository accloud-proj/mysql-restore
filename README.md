# mysql-restore tool

[中文使用说明](README_zh.md)

Create scheduled **mysqldump GTID full backups**, then recover to a chosen time
using the source's existing binlogs in an official MySQL Docker container.
The result is an SQL export; the production database is never overwritten.
Run on either the primary host or a read-only replica host.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.14+
  (managed by uv).
- Oracle MySQL 8.0.32+, with matching `mysql` and `mysqldump` clients.
  Primary/replica recovery is tested on 8.4.7 and 26.10.0; test other releases first.
  MySQL 5.x, MariaDB and Percona are unsupported.
- Docker running for recovery. If the official image lacks `mysqlbinlog`, install
  the matching host tool; both tested image versions require it.
- InnoDB application tables, no schema changes during backup, and readable local
  source binlogs. Enable GTID and ROW binary logging; see the Chinese guide for
  server configuration and backup-account grants.
- Replicas need `log-replica-updates=ON`, `read-only=ON`, a unique server ID and
  working replication. Use their local binlogs, not relay logs.

## 1. Clone and configure

```sh
git clone https://github.com/accloud-proj/mysql-restore.git
cd mysql-restore
uv sync
uv run mysql-restore init
```

Enter the source connection, backup destination, nightly schedule and retention
count. Password input is hidden. Version, role and binlog paths are discovered
from the source without prompts. Setup validates the source before saving files;
on failure it removes newly created credentials, never an existing credential file.
Setup creates `mysql-restore.json` and a private `mysql-restore.client.cnf`.
Never commit real credentials.

Alternatively edit [config.example.json](config.example.json) and provide a MySQL
client credential file with mode `600`.

Important settings:

| Field           | Meaning                                                       |
| --------------- | ------------------------------------------------------------- |
| `role`          | Optional; detected from replication channels; override with `primary`/`replica` |
| `mysql_version` | Optional; queried from source; explicit versions must match |
| `backup_dir`    | Full-backup destination                                       |
| `binlog_dir`    | Optional; discovered server directory; override with host path |
| `binlog_index`  | Optional local index; omitted/`null` uses the source log list |
| `schedule`      | Cron schedule, default `0 2 * * *`                            |
| `max_backups`   | Positive count to retain; `0`/`null`/omitted disables cleanup |
| `log_file`      | Scheduled-backup log                                          |

Configuration paths are relative to the configuration file. Keep backup and
source-log directories separate and non-nested. For Docker sources, use the
host bind-mount path.

Omit automatic fields or set them to `null`. New configurations do not persist
discovered values: `check`, `backup` and `restore` resolve them on each run.
Cron generation and cleanup remain offline.
Server paths cannot reveal Docker mount mappings. Inaccessible paths produce an
explicit error; use the template and set `binlog_dir` (and `binlog_index` if needed)
to the correct host paths. With an explicit directory and no index, logs are
listed from the source.

## 2. Check and back up

```sh
uv run mysql-restore check
uv run mysql-restore backup
```

Each full backup contains a timestamped SQL dump and recovery metadata.
Keep the complete backup directory.

Select another configuration before the subcommand:

```sh
uv run mysql-restore --config /etc/mysql/pitr.json backup
```

## 3. Schedule nightly backups

```sh
# Preview
uv run mysql-restore cron

# Install into the current user's crontab after confirmation
uv run mysql-restore cron --install

# Or generate a script and fragment to add manually with crontab -e
uv run mysql-restore cron --script ./nightly-backup.sh --output backup.cron
```

Default: daily at 02:00 in the host timezone. Change `schedule` and reinstall to
update it; unrelated jobs are preserved. The log parent must exist and be
writable. Regenerate jobs after moving the project or rebuilding its environment.

## 4. Recover and export

```sh
mkdir -p exports
uv run mysql-restore restore \
  --target-time '2026-10-31T14:30:00+08:00' \
  --output ./exports/restored.sql
```

For selected databases, append `--database shop` (repeatable).
For tables, append `--database shop --table orders --table order_items`;
table exports require exactly one database.

Time requires an explicit timezone. The output parent must exist and the output
file must not exist. Temporary containers/volumes are removed after recovery.
Replica targets use local replica commit time, including replication lag.

Recovery logs steps to stderr automatically, with elapsed-time updates every
five seconds. Copying shows byte percentages; binlog processing shows file
counts; decoding/exporting shows bytes written. Import percentages measure SQL
input consumption, **not database execution**: wait for `DONE`, even at 100%.
Steps without measurable percentages show a running status instead.
Use `2>restore.log` to save the logs.

## 5. Retain or clean backups

Set `"max_backups": 7` to automatically retain the latest seven snapshots after
each successful backup. `0`, `null` or an omitted field disables deletion;
`1` retains only the latest snapshot. Negative values are invalid.

```sh
uv run mysql-restore cleanup --max-backups 7 --dry-run
uv run mysql-restore cleanup --max-backups 7
```

Cleanup deletes old full backups only, never source binlogs.

## Important

- Recovery requires an online source, an eligible full backup and continuous logs.
- **Purged or lost source binlogs make that time range unrecoverable.**
  Set source log retention to match your recovery window.
- Application databases only: system accounts, grants, plugins and keys are not
  backed up. Manage special DEFINER/plugin dependencies separately.
- Allow enough recovery disk space and rehearse recovery before production use.
  Use a new backup directory after upgrading the source version.

```sh
uv run mysql-restore --help
uv run mysql-restore restore --help
```

## LICENSE

[BSD-3-Clause](LICENSE)
