# mysql-restore tool

定期使用 `mysqldump` 生成 GTID 全量备份。恢复时自动选择最近的备份，在官方 MySQL Docker 容器中导入数据，再用现有 binlog 恢复到指定时间，最后导出 SQL 文件，**不覆盖生产库**。

可以部署在生产主库主机，也可以部署在只读从库主机。不需要单独复制或归档 binlog。

## 1. 使用前准备

- 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)；Python 3.14+ 由 uv 管理。
- 使用 Oracle MySQL 8.0.32+。已验证 8.4.7、26.10.0；其他版本建议先测试，不支持 MySQL 5.x、MariaDB、Percona。
- 安装与源库**相同补丁版本**的 `mysql`、`mysqldump`。
- 恢复时启动 Docker。官方镜像没有 `mysqlbinlog` 时，宿主机也需安装同版本 `mysqlbinlog`（已验证的两个版本均需要）。
- 全部应用表使用 InnoDB，备份期间不要修改表结构。
- 工具能连接数据库，并读取该实例的本地 binlog 目录。

### 数据库配置

确认源库配置满足以下要求；已有生产库请在维护窗口或按官方流程调整，不要贸然重启：

```ini
[mysqld]
server-id=1
log-bin=mysql-bin
binlog-format=ROW
gtid-mode=ON
enforce-gtid-consistency=ON
binlog-expire-logs-seconds=2592000
sync-binlog=1
innodb-flush-log-at-trx-commit=1
binlog-encryption=OFF
binlog-transaction-compression=OFF
```

只读从库还需：

```ini
server-id=2
log-replica-updates=ON
read-only=ON
```

从库 `server-id` 必须唯一，并已配置正常运行的复制通道。读取从库自己的 **binlog，不是 relay log**；恢复时间按从库本地提交时间计算，包含复制延迟。

### 备份账号权限

管理员先创建备份账号，再按实际连接来源授权：

```sql
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, RELOAD,
      REPLICATION CLIENT, SHOW DATABASES ON *.* TO 'backup'@'localhost';
GRANT SHOW_ROUTINE ON *.* TO 'backup'@'localhost';
```

## 2. 克隆并配置

```sh
git clone https://github.com/accloud-proj/mysql-restore.git
cd mysql-restore
uv sync
uv run mysql-restore init
```

按提示填写数据库连接、备份目录、夜间备份时间和保留数量。密码隐藏输入。

MySQL 版本、主/从库角色和 binlog 路径自动从数据库获取，不再提问。init 会连接源库验证，失败时不保存新配置或新密码文件；已有的密码文件不会被删除。

自动生成：

- `mysql-restore.json`：工具配置。
- `mysql-restore.client.cnf`：密码配置，权限 `600`。

不要提交真实配置和密码文件。也可手工复制 [config.example.json](config.example.json)，并配置权限为 `600` 的 MySQL 客户端密码文件。

主要配置：

| 字段                     | 用途                                            |
| ------------------------ | ----------------------------------------------- |
| `host` / `port` / `user` | 数据库连接                                      |
| `mysql_version`          | 可选，自动查询源库准确版本；填写则校验版本一致 |
| `role`                   | 可选，按复制通道自动识别；可指定 `primary`/`replica` |
| `backup_dir`             | 全量备份保存目录                                |
| `binlog_dir`             | 可选，自动查询源库 binlog 目录；可覆盖为宿主机路径 |
| `binlog_index`           | 可选，省略或 `null` 时直接查询源库日志列表；可指定本地 index |
| `schedule`               | cron 时间，默认 `0 2 * * *`                     |
| `max_backups`            | 保留份数；`0`、`null` 或未配置表示不清理        |
| `log_file`               | 定时备份的运行日志                              |

配置中的相对路径以配置文件所在目录为准。备份目录与源 binlog 目录必须分开且互不嵌套；Docker 源库填写**宿主机挂载路径**。

上述自动字段省略或设为 `null` 即可。新生成配置不保存探测值，`check`、`backup`、`restore` 每次执行时查询；生成 cron 或清理备份不需要连接数据库。

数据库只能提供服务器内路径，无法知道 Docker 挂载映射。若自动路径在本机不可访问，工具会明确报错；请复制配置模板并手工设置 `binlog_dir`（必要时也设置 `binlog_index`），不会猜测路径。

使用其他配置文件，在子命令前指定：

```sh
uv run mysql-restore --config /etc/mysql/pitr.json check
```

## 3. 执行第一次备份

```sh
uv run mysql-restore check
uv run mysql-restore backup
```

检查通过后再配置定时任务。备份目录会生成带 UTC 时间的全量 SQL 和恢复元数据，请保留整份目录。

## 4. 设置夜间定时备份

预览 cron 条目：

```sh
uv run mysql-restore cron
```

自动添加到当前用户的 crontab，执行时会询问确认：

```sh
uv run mysql-restore cron --install
```

或者生成脚本和 cron 片段，自行加入 `crontab -e`：

```sh
uv run mysql-restore cron --script ./nightly-backup.sh --output backup.cron
```

默认每天凌晨 2 点，按系统时区执行。修改 `schedule` 后重新安装即可，其他 cron 任务不会被覆盖。日志父目录需已存在且可写；移动项目或重建虚拟环境后重新生成任务。

## 5. 恢复并导出

全库恢复：

```sh
mkdir -p exports
uv run mysql-restore restore \
  --target-time '2026-10-31T14:30:00+08:00' \
  --output ./exports/restored.sql
```

只导出指定库，在上述命令后添加：

```sh
--database shop
```

只导出指定表：

```sh
--database shop --table orders --table order_items
```

`--database` 可重复；指定表时只能选择一个库。时间必须带时区，输出目录必须存在，输出文件不能已存在。

工具自动读取配置中的备份目录和原始 binlog，恢复完成后清理临时容器与数据卷。官方镜像内有 `mysqlbinlog` 就优先使用，没有才使用宿主机工具。

恢复默认实时输出步骤日志（无需额外参数）：备份选择、日志校验、启动容器、定位时间点、导入全量、重放 binlog、导出和清理。耗时步骤每 5 秒显示已用时间；复制显示字节百分比，binlog 显示文件序号，解析/导出显示已写入大小。

导入百分比表示 **SQL 输入读取进度，不是数据库执行进度**；即使读取达到 100%，仍需等待步骤显示 `DONE`。无法准确计算百分比的步骤只显示运行状态，不估算虚假进度。日志输出到 stderr，重定向保存可使用 `2>restore.log`。

## 6. 保留与清理备份

例如在配置中设置 `"max_backups": 7`，每次新备份成功后自动保留最新 7 份。

- `0`、`null` 或省略：不清理。
- 正整数 `N`：保留最新 N 份，`1` 表示仅保留最新一份。
- 负数：不允许。

手动预览和清理：

```sh
uv run mysql-restore cleanup --max-backups 7 --dry-run
uv run mysql-restore cleanup --max-backups 7
```

清理仅删除旧全量备份，不删除源 binlog。

## 注意事项

- 源库必须在线，目标时间必须有可用全量备份和连续 binlog。
- **源 binlog 被清理或丢失后，对应时间段无法恢复**；根据恢复窗口设置日志保留期。
- “全库”仅包含应用数据库，不包含系统账号、授权、插件和密钥；特殊 DEFINER/插件依赖应单独管理。
- 预留恢复数据库及临时 SQL 所需磁盘空间。正式使用前先演练恢复，升级源库版本后使用新的备份目录。

更多命令参数：

```sh
uv run mysql-restore --help
uv run mysql-restore restore --help
```

## LICENSE

[BSD-3-Clause](LICENSE)
