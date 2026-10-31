# mysql-restore

定时备份 MySQL，恢复到指定时间并导出 SQL 文件，**不会覆盖生产库**。

原理：使用 `mysqldump` 做全量备份，恢复时在 Docker 中结合原始 binlog 还原数据。可在主库或只读从库主机上使用。

## 使用前准备

- 安装 Docker；源码运行方式还需安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
- 使用 Oracle MySQL 8.0.32+，安装同版本的 `mysql`、`mysqldump`、`mysqlbinlog`。
- 请数据库管理员开启 GTID 和 ROW 格式 binlog，提供有备份权限的账号；从库还需开启 `log_replica_updates` 并保持只读。
- 在能读取源库 binlog 文件的主机上运行；应用表使用 InnoDB，备份期间不要修改表结构。

## 1. 安装和配置

### 方式一：下载可执行文件

从 [Releases](https://github.com/accloud-proj/mysql-restore/releases) 下载对应系统和架构的压缩包，解压后执行，无需安装 Python 或 uv：

```sh
./mysql-restore init
```

Windows、Linux、macOS 均提供 x86_64／ARM64 版本。Linux 需要 Ubuntu 22.04 或兼容系统，macOS 需要 15+；Windows 使用 `.\mysql-restore.exe`。MySQL 客户端和 Docker 仍需自行安装，并加入 PATH。

### 方式二：克隆源码运行

```sh
git clone https://github.com/accloud-proj/mysql-restore.git
cd mysql-restore
uv sync
uv run mysql-restore init
```

按提示填写连接地址、账号、密码、备份目录、定时计划和保留数量。数据库版本、主从角色和日志目录自动获取。

下文使用源码方式展示命令；使用可执行文件时，将 `uv run mysql-restore` 替换为 `./mysql-restore`（Windows：`.\mysql-restore.exe`）即可。

配置保存在 `mysql-restore.json`，密码保存在 `mysql-restore.client.cnf`，请勿分享或提交这两个文件。

Windows 用户请通过文件夹权限限制其他用户读取密码文件。

也可参考 [配置模板](config.example.json) 手工配置。若源库运行在 Docker 中，需要手工将 `binlog_dir` 设置为宿主机上的日志目录。备份目录和 binlog 目录不能互相包含。

## 2. 检查并执行一次备份

```sh
uv run mysql-restore check
uv run mysql-restore backup
```

检查通过、首次备份成功后，再设置定时任务。请完整保留生成的备份目录。

## 3. 设置自动备份

以下 cron 操作适用于 Linux／macOS。Windows 请在“任务计划程序”中定时执行 `mysql-restore.exe --config 配置文件绝对路径 backup`，并使用绝对路径填写配置中的目录。

添加到当前用户的 crontab：

```sh
uv run mysql-restore cron --install
```

默认每天凌晨 2 点（系统时区）。修改配置中的 `schedule` 后重新执行此命令即可。

如需自行添加任务，生成脚本和 cron 片段，再把片段加入 `crontab -e`：

```sh
uv run mysql-restore cron --script ./nightly-backup.sh --output backup.cron
```

定时备份日志默认写入 `mysql-backup.log`。移动项目目录后请重新安装定时任务。

## 4. 恢复到指定时间

先启动 Docker，再执行：

```sh
uv run mysql-restore restore \
  --target-time '2026-11-01T00:01:00+08:00' \
  --output ./restored.sql
```

默认导出全部应用数据库。在命令后添加参数可限定导出内容：

| 需要导出的内容 | 添加参数 |
| --- | --- |
| 一个数据库 | `--database shop` |
| 多个数据库 | `--database shop --database billing` |
| 一个库中的指定表 | `--database shop --table orders --table order_items` |

时间必须带时区；`+08:00` 表示北京时间。输出目录必须存在，输出文件不能已存在。

执行时自动显示步骤和进度，请等待最终的 `Recovery export completed` 提示。

## 5. 设置备份保留数量

在配置中设置 `"max_backups": 7`，每次新备份成功后自动保留最新 7 份。

`0`、`null` 或省略表示不清理；正整数表示保留份数，负数不允许。

也可手动预览和清理：

```sh
uv run mysql-restore cleanup --max-backups 7 --dry-run
uv run mysql-restore cleanup --max-backups 7
```

仅清理旧全量备份，不删除源 binlog。

## 重要提醒

- 恢复时源库必须在线，且有目标时间之前的全量备份和连续 binlog。**日志被清理或丢失后，对应时间段无法恢复。**
- 从库恢复时间按从库本地提交时间计算，可能与主库存在复制延迟。
- 备份不包含系统账号和授权；预留足够磁盘空间，正式使用前先演练恢复。

其他参数查看 `uv run mysql-restore --help`。使用其他配置时，将 `--config 路径` 放在子命令前。

## 打包与发布

版本号和 tag 使用 `yyyy.mm[.修订号]`，例如 `2026.11`、`2026.11.1`，月份为两位，不加 `v`。

- 推送新 tag 后，GitHub Actions 自动打包全部支持的平台并上传到 Releases。
- 也可在 Actions → **Build release binaries** → **Run workflow** 中选择分支并填写版本号；已有 tag 必须对应所选提交。
- 本机打包：`uv sync --group build`，再执行 `uv run --group build python scripts/package.py --version 2026.11.1`。产物位于 `dist/`，仅适用于本机系统和架构。

## License

[BSD-3-Clause](LICENSE)
