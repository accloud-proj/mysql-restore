"""Build and smoke-test a native release archive."""

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def release_version(value: str) -> str:
    if not re.fullmatch(r"[1-9]\d{3}\.(?:0[1-9]|1[0-2])(?:\.(?:0|[1-9]\d*))?", value):
        raise ValueError("Version/tag must be yyyy.mm[.revision], e.g. 2026.11 or 2026.11.1")
    return value


def build(version: str) -> Path:
    version = release_version(version)
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}[platform.system()]
    architecture = {"AMD64": "x86_64", "x86_64": "x86_64",
                    "aarch64": "arm64", "arm64": "arm64", "ARM64": "arm64"}[platform.machine()]
    work = ROOT / "build"
    work.mkdir(exist_ok=True)
    (work / "version.txt").write_text(version + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                    str(ROOT / "mysql-restore.spec")], cwd=ROOT, check=True)
    binary = ROOT / "dist" / ("mysql-restore.exe" if system == "windows" else "mysql-restore")
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True)
    if result.stdout.strip() != "mysql-restore " + version:
        raise RuntimeError("Bundled version does not match the release")
    subprocess.run([str(binary), "--help"], check=True)
    for command in ["init", "check", "backup", "cleanup", "restore", "cron"]:
        subprocess.run([str(binary), command, "--help"], check=True, stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="mysql-binary-smoke-") as name:
        directory = Path(name)
        configuration = directory / "config.json"
        configuration.write_text(json.dumps({
            "format": 1, "host": "127.0.0.1", "port": 3306, "user": "backup",
            "defaults_file": "client.cnf", "backup_dir": "backups",
            "schedule": "0 2 * * *", "log_file": "backup.log",
        }), encoding="utf-8")
        os.chmod(configuration, 0o600)
        subprocess.run([str(binary), "--config", str(configuration), "cleanup"],
                       cwd=directory, check=True)
        if system != "windows":
            script = directory / "backup.sh"
            subprocess.run([str(binary), "--config", str(configuration), "cron",
                            "--script", str(script)], cwd=directory, check=True)
            contents = script.read_text()
            if "-m mysql_restore" in contents or str(binary) not in contents:
                raise RuntimeError("Bundled cron script must invoke the installed executable")
        else:
            result = subprocess.run([str(binary), "--config", str(configuration), "cron"],
                                    cwd=directory, capture_output=True, text=True)
            if result.returncode != 1 or "Task Scheduler" not in result.stderr:
                raise RuntimeError("Windows must explicitly direct cron users to Task Scheduler")
    name = "mysql-restore-{}-{}-{}".format(version, system, architecture)
    staging = work / name
    staging.mkdir(exist_ok=True)
    shutil.copy2(binary, staging / binary.name)
    for filename in ["README.md", "README_zh.md", "config.example.json", "LICENSE"]:
        shutil.copy2(ROOT / filename, staging / filename)
    archive = Path(shutil.make_archive(
        str(ROOT / "dist" / name), "zip" if system == "windows" else "gztar",
        root_dir=work, base_dir=name,
    ))
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(
        "{}  {}\n".format(digest, archive.name), encoding="ascii",
    )
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, type=release_version)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if not args.validate_only:
        print(build(args.version))


if __name__ == "__main__":
    main()
