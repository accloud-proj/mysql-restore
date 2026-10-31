"""Opt in with MYSQL_PITR_INTEGRATION=1 uv run python -m unittest discover -s tests."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from mysql_restore import backup, cli, restore


@unittest.skipUnless(os.environ.get("MYSQL_PITR_INTEGRATION") == "1", "Opt-in Docker/MySQL integration")
class DockerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mysql-direct-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.version = backup.supported_version(os.environ.get("MYSQL_PITR_VERSION", "8.4.7"))
        self.settings = {
            "lower_case_table_names": 1, "character_set_server": "utf8mb4",
            "collation_server": "utf8mb4_0900_ai_ci", "sql_mode": "STRICT_TRANS_TABLES",
        }

    def source_container(self, label, network=None):
        work = self.root / (label + "-work")
        work.mkdir()
        data = self.root / (label + "-data")
        data.mkdir()
        container = restore.Recovery(work, self.version, self.settings)
        self.addCleanup(container.cleanup)
        real_run = restore.run

        def start_run(command, **kwargs):
            if command[:2] == ["docker", "run"] and "--detach" in command:
                command = list(command)
                offset = command.index("type=volume,source={},target=/var/lib/mysql".format(container.volume))
                command[offset] = "type=bind,source={},target=/var/lib/mysql".format(data)
                if network:
                    command[command.index("--network") + 1] = network
                    command.remove("--skip-networking")
                    command.insert(command.index("--env"), "--env")
                    command.insert(command.index("--env") + 1, "MYSQL_ROOT_HOST=%")
            return real_run(command, **kwargs)

        with patch.object(restore, "run", side_effect=start_run):
            container.start(180)
        return container, data

    def routed_source(self, container):
        real_run = backup.run

        def source_run(command, **kwargs):
            if command[0] in {"mysql", "mysqldump"}:
                translated = [
                    "--defaults-file=/work/client.cnf" if argument.startswith("--defaults-file=")
                    else argument for argument in command
                ]
                return real_run(["docker", "exec", "-i", container.name] + translated, **kwargs)
            return real_run(command, **kwargs)

        return patch.object(backup, "run", side_effect=source_run)

    def write_config(self, container, data, role):
        path = self.root / (role + ".json")
        backup.atomic_json(path, {
            "format": 1, "host": "127.0.0.1", "port": 3306, "user": "root",
            "defaults_file": str(container.work / "client.cnf"), "mysql_version": self.version,
            "role": role, "backup_dir": str(self.root / (role + "-backups")),
            "binlog_dir": str(data), "binlog_index": str(data / "restore-bin.index"),
            "schedule": "0 2 * * *", "log_file": str(self.root / "backup.log"),
        })
        return path

    def invoke(self, config, *arguments):
        with patch("sys.argv", ["mysql-restore", "--config", str(config), *arguments]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)

    def test_primary_recovery_allows_rotation_before_and_during_recovery(self):
        container, data = self.source_container("rotation")
        config = self.write_config(container, data, "primary")
        container.query(
            "CREATE DATABASE rotation_test; "
            "CREATE TABLE rotation_test.items (id INT PRIMARY KEY, value VARCHAR(32)) ENGINE=InnoDB; "
            "INSERT INTO rotation_test.items VALUES (1,'before');"
        )
        with self.routed_source(container):
            self.invoke(config, "backup")
            container.query("FLUSH BINARY LOGS; UPDATE rotation_test.items SET value='included' WHERE id=1;")
            target = backup.Source(container.work / "client.cnf").current_time()
            container.query("DO SLEEP(0.05); UPDATE rotation_test.items SET value='excluded' WHERE id=1;")
            original_cutoff = restore.Recovery.find_cutoff

            def rotate_after_cutoff(recovery, entries, start, time_target):
                cutoff = original_cutoff(recovery, entries, start, time_target)
                container.query("FLUSH BINARY LOGS")
                return cutoff

            output = self.root / "rotated.sql"
            with patch.object(restore.Recovery, "find_cutoff", new=rotate_after_cutoff):
                self.invoke(config, "restore", "--target-time", backup.timestamp(target),
                            "--output", str(output))
            self.assertIn("included", output.read_text())
            self.assertNotIn("excluded", output.read_text())
            # Reusing the same backup must still work after both rotations.
            repeated = self.root / "repeated.sql"
            self.invoke(config, "restore", "--target-time", backup.timestamp(target),
                        "--output", str(repeated))
            self.assertIn("included", repeated.read_text())
            self.assertNotIn("excluded", repeated.read_text())

    def test_primary_cli_uses_active_binlog_and_exports_reimportable_sql(self):
        container, data = self.source_container("primary")
        config = self.write_config(container, data, "primary")
        container.query(
            "CREATE DATABASE alpha; CREATE DATABASE beta; "
            "CREATE TABLE alpha.orders (id INT PRIMARY KEY, value VARCHAR(32)) ENGINE=InnoDB; "
            "CREATE TABLE alpha.other (id INT PRIMARY KEY) ENGINE=InnoDB; "
            "CREATE TABLE beta.audit (id INT PRIMARY KEY) ENGINE=InnoDB; "
            "INSERT INTO alpha.orders VALUES (1,'before');"
        )
        with self.routed_source(container):
            self.invoke(config, "check")
            self.invoke(config, "backup")
            container.query(
                "BEGIN; UPDATE alpha.orders SET value='included' WHERE id=1; "
                "INSERT INTO beta.audit VALUES (1); COMMIT;"
            )
            source = backup.Source(container.work / "client.cnf")
            target = source.current_time()
            container.query("DO SLEEP(0.05); UPDATE alpha.orders SET value='excluded' WHERE id=1;")
            # No FLUSH or copy: restore must read the active, growing source file.
            active_before = source.log_window(self.version)["files"][-1]["name"]
            output = self.root / "all.sql"
            self.invoke(config, "restore", "--target-time", backup.timestamp(target), "--output", str(output))
            active_after = source.log_window(self.version)["files"][-1]["name"]
            self.assertEqual(active_before, active_after)
            selected = self.root / "orders.sql"
            self.invoke(config, "restore", "--target-time", backup.timestamp(target),
                        "--output", str(selected), "--database", "alpha", "--table", "orders")
        backups = self.root / "primary-backups"
        self.assertFalse((backups / "binlogs").exists())
        self.assertNotIn("excluded", selected.read_text())
        self.assertNotIn("CREATE TABLE `other`", selected.read_text())
        verify_work = self.root / "verify"
        verify_work.mkdir()
        verify = restore.Recovery(verify_work, self.version, self.settings)
        self.addCleanup(verify.cleanup)
        verify.start(180)
        verify.import_sql(output)
        self.assertEqual(verify.query("SELECT value FROM alpha.orders WHERE id=1").strip(), "included")
        self.assertEqual(verify.query("SELECT COUNT(*) FROM beta.audit").strip(), "1")
        with self.assertRaisesRegex(backup.BackupError, "Access denied"):
            backup.run(verify.mysql_command() + ["--password=invalid-test-password", "--execute", "SELECT 1"])

    def wait_for(self, container, sql, expected):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if container.query(sql).strip() == expected:
                return
            time.sleep(0.1)
        self.fail("Replication failed to reach expected state: " + container.query("SHOW REPLICA STATUS"))

    def test_readonly_replica_backups_replay_local_replication_binlog(self):
        network = "mysql-pitr-test-" + uuid.uuid4().hex
        backup.run(["docker", "network", "create", network])
        self.addCleanup(backup.run, ["docker", "network", "rm", network])
        primary, _ = self.source_container("upstream", network)
        replica, data = self.source_container("replica", network)
        # MySQL replication requires distinct server IDs and UUIDs.
        replica.query("SET GLOBAL server_id=2;")
        password = uuid.uuid4().hex
        primary.query(
            "CREATE USER 'replicator'@'%' IDENTIFIED BY '{}'; "
            "GRANT REPLICATION SLAVE ON *.* TO 'replicator'@'%';".format(password)
        )
        replica.query(
            "CHANGE REPLICATION SOURCE TO SOURCE_HOST='{}', SOURCE_PORT=3306, "
            "SOURCE_USER='replicator', SOURCE_PASSWORD='{}', SOURCE_AUTO_POSITION=1, "
            "GET_SOURCE_PUBLIC_KEY=1; START REPLICA; SET GLOBAL read_only=ON;".format(primary.name, password)
        )
        primary.query(
            "CREATE DATABASE app; CREATE TABLE app.items (id INT PRIMARY KEY, value VARCHAR(32)) "
            "ENGINE=InnoDB; INSERT INTO app.items VALUES (1,'before');"
        )
        self.wait_for(replica, "SELECT COUNT(*) FROM information_schema.tables WHERE TABLE_SCHEMA='app'", "1")
        self.wait_for(replica, "SELECT value FROM app.items WHERE id=1", "before")
        config = self.write_config(replica, data, "replica")
        with self.routed_source(replica):
            self.invoke(config, "check")
            self.invoke(config, "backup")
            primary.query("UPDATE app.items SET value='included' WHERE id=1;")
            self.wait_for(replica, "SELECT value FROM app.items WHERE id=1", "included")
            source = backup.Source(replica.work / "client.cnf")
            target = source.current_time()
            primary.query("DO SLEEP(0.05); UPDATE app.items SET value='excluded' WHERE id=1;")
            self.wait_for(replica, "SELECT value FROM app.items WHERE id=1", "excluded")
            output = self.root / "replica.sql"
            self.invoke(config, "restore", "--target-time", backup.timestamp(target), "--output", str(output))
        verify_work = self.root / "replica-verify"
        verify_work.mkdir()
        verify = restore.Recovery(verify_work, self.version, self.settings)
        self.addCleanup(verify.cleanup)
        verify.start(180)
        verify.import_sql(output)
        self.assertEqual(verify.query("SELECT value FROM app.items WHERE id=1").strip(), "included")
        self.assertEqual(replica.query("SELECT @@read_only").strip(), "1")


if __name__ == "__main__":
    unittest.main()
