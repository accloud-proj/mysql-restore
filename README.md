# mysql-restore

[中文使用说明](README_zh.md)

Schedule MySQL backups and recover to a chosen time as an SQL export.
**The production database is never overwritten.**

Full backups use `mysqldump`; recovery combines a backup with the source binlogs
inside Docker. Run on a primary or replica host.

## Before you start

- Install Docker; source checkout usage also requires [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Use Oracle MySQL 8.0.32+ with matching `mysql`, `mysqldump` and `mysqlbinlog` tools.
- Ask your database administrator to enable GTID and binlogs (ROW, MIXED or
  STATEMENT; ROW recommended) and provide
  a backup account. Replicas also need `log_replica_updates`.
- Run where source binlogs are locally readable. Use InnoDB application tables
  and avoid schema changes during backups.

## 1. Install and configure

### Option A: Download a binary

Download and extract your platform's archive from
[Releases](https://github.com/accloud-proj/mysql-restore/releases), then run:

```sh
./mysql-restore init
```

No Python or uv required. Windows, Linux and macOS builds cover
x86_64 and ARM64. Linux requires Ubuntu 22.04 or compatible; macOS requires 15+.
On Windows use `.\mysql-restore.exe`. Install MySQL clients and Docker separately
and make them available in PATH.

### Option B: Run from source

```sh
git clone https://github.com/accloud-proj/mysql-restore.git
cd mysql-restore
uv sync
uv run mysql-restore init
```

Enter the connection, credentials, backup directory, schedule and retention count.
Version, role and log directory are detected automatically.

Commands below use the source form. For binaries replace `uv run mysql-restore`
with `./mysql-restore` (Windows: `.\mysql-restore.exe`).

Setup creates `mysql-restore.json` and `mysql-restore.client.cnf`.
Do not share or commit these files.
On Windows restrict access to credentials using folder permissions.

For manual setup, use the [configuration template](config.example.json).
For Docker sources, set `binlog_dir` to the host log directory.
Backup and binlog directories must not contain each other.

## 2. Check and create your first backup

```sh
uv run mysql-restore check
uv run mysql-restore backup
```

Schedule backups only after these succeed. Keep each complete backup directory.

## 3. Schedule automatic backups

Cron applies to Linux/macOS. On Windows use Task Scheduler to run
`mysql-restore.exe --config ABSOLUTE_CONFIG_PATH backup`, with absolute paths
in the configuration.

Install into the current user's crontab:

```sh
uv run mysql-restore cron --install
```

Default: daily at 02:00 in the system timezone.
Change `schedule` in the configuration and run this command again to update it.

Or generate a script and cron fragment, then add the fragment with `crontab -e`:

```sh
uv run mysql-restore cron --script ./nightly-backup.sh --output backup.cron
```

Scheduled logs default to `mysql-backup.log`.
Reinstall the job after moving the project.

## 4. Recover to a chosen time

Start Docker, then run:

```sh
uv run mysql-restore restore \
  --target-time '2026-11-01T00:01:00+08:00' \
  --output ./restored.sql
```

All application databases are exported by default. Append parameters to select:

| Export | Parameters |
| --- | --- |
| One database | `--database shop` |
| Multiple databases | `--database shop --database billing` |
| Tables in one database | `--database shop --table orders --table order_items` |

Include a timezone in the target time. The output directory must exist and the
output file must not exist.

Steps and progress appear automatically. Wait for `Recovery export completed`.

## 5. Limit retained backups

Set `"max_backups": 7` to retain the latest seven backups after each successful
backup. `0`, `null` or omission disables cleanup. Positive integers set the
retention count; negative values are invalid.

Preview or clean manually:

```sh
uv run mysql-restore cleanup --max-backups 7 --dry-run
uv run mysql-restore cleanup --max-backups 7
```

Only old full backups are deleted, never source binlogs.

## Important

- Recovery requires an online source, an earlier full backup and continuous
  binlogs. **Lost or purged logs make the corresponding time range unrecoverable.**
- Replica targets use local replica commit time and include replication lag.
- System accounts and grants are not backed up. Allow sufficient disk space
  and rehearse recovery before production use.

See `uv run mysql-restore --help` for other options.
Place `--config PATH` before the subcommand to select another configuration.

## Build and release

Versions and tags use `yyyy.mm[.revision]`, e.g. `2026.11` or `2026.11.1`;
use two-digit months and no `v` prefix.

- Push a new tag to build all supported platforms and publish GitHub Release assets.
- Or select Actions → **Build release binaries** → **Run workflow**, choose a
  branch and enter a version. Existing tags must match the selected commit.
- Local build: `uv sync --group build`, then
  `uv run --group build python scripts/package.py --version 2026.11.1`.
  Archives are in `dist/`, built for the current platform and architecture only.

## License

[BSD-3-Clause](LICENSE)
