"""Скрипты бэкапов без сервера: bash -n, shellcheck (если есть) и запуск с подменёнными командами.

Вместо docker, curl, df, flock, pg_dump, openssl и rclone в PATH кладутся фальшивые команды:
они пишут свои аргументы в журнал и отвечают так, как задано в тесте. Настоящие Docker, база
и хранилище здесь не нужны; на сервере скрипты проверяет владелец (infra/README.md, раздел 13).
"""
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from ._compose import INFRA

BACKUP = INFRA / "backup"
DUMP_SCRIPT = INFRA / "postgres" / "bin" / "dealers-dump.sh"
SCRIPTS = sorted(BACKUP.glob("*.sh")) + [DUMP_SCRIPT]
PING_KEY = "test-ping-key-not-real"

# Фальшивый docker. Отвечает на вызовы скриптов; FAKE_FAIL — подстрока аргументов, на которой упасть.
FAKE_DOCKER = r'''#!/bin/bash
args="$*"
echo "docker $args" >> "$FAKE_LOG"
if [ -n "${FAKE_FAIL:-}" ] && [[ "$args" == *"$FAKE_FAIL"* ]]; then echo "fake failure: $args" >&2; exit 1; fi
case "$args" in
  *"--target="*)
    for a in "$@"; do case "$a" in --target=*) echo "${a#--target=}" > "$FAKE_DIR/target";; esac; done ;;
  "inspect "*) echo true ;;
  "logs "*) echo "LOG:  recovery stopping at restore point \"$(cat "$FAKE_DIR/target")\", time now" ;;
  *"pg_is_in_recovery"*) echo f ;;
  *"string_agg(nspname"*) echo "${FAKE_SCHEMAS:-catalog,content,platform_ro}" ;;
  *"exec -T -u postgres postgres psql"*)
    cat > /dev/null
    printf 'platform_ro.rights_check 1\nrestore_point 0/3000090\n' ;;
  *"query_to_xml"*) printf 'platform_ro.rights_check %s\n' "${FAKE_RESTORED_ROWS:-1}" ;;
esac
exit 0
'''

FAKE_CURL = r'''#!/bin/bash
url=$(sed -n 's/^url = "\(.*\)"$/\1/p')
echo "curl $url $*" >> "$FAKE_LOG"
'''

FAKE_DF = r'''#!/bin/bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/vda1 100 ${FAKE_DISK_USED:-50} 50 ${FAKE_DISK_USED:-50}% /"
'''

FAKE_FLOCK = "#!/bin/bash\nexit 0\n"

# Для dealers-dump.sh: базы, шифрование, хранилище.
FAKE_PG_DUMPALL = '#!/bin/bash\necho "pg_dumpall $*" >> "$FAKE_LOG"\necho "-- roles"\n'
FAKE_PG_DUMP = r'''#!/bin/bash
echo "pg_dump $*" >> "$FAKE_LOG"
if [ -n "${FAKE_FAIL:-}" ] && [[ "$*" == *"$FAKE_FAIL"* ]]; then exit 1; fi
echo "dump of $*"
'''
FAKE_OPENSSL = r'''#!/bin/bash
echo "openssl $*" >> "$FAKE_LOG"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-out" ] && out=$2; shift; done
cat > "$out"
'''
FAKE_RCLONE = '#!/bin/bash\necho "rclone $*" >> "$FAKE_LOG"\n'
FAKE_SHA256SUM = '#!/bin/bash\nfor f in "$@"; do echo "0000  $f"; done\n'


class FakeEnv:
    def __init__(self, ping_key=PING_KEY):
        self.dir = Path(tempfile.mkdtemp(prefix="backup-test-"))
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.log = self.dir / "calls.log"
        self.log.touch()
        for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("df", FAKE_DF), ("flock", FAKE_FLOCK),
                           ("pg_dumpall", FAKE_PG_DUMPALL), ("pg_dump", FAKE_PG_DUMP), ("openssl", FAKE_OPENSSL),
                           ("rclone", FAKE_RCLONE), ("sha256sum", FAKE_SHA256SUM), ("sleep", FAKE_FLOCK)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        self.env_file = self.dir / ".env"
        self.env_file.write_text(textwrap.dedent(f"""\
            PLATFORM_ENV=test
            HEALTHCHECKS_PING_KEY={ping_key}
            PGBACKREST_REPO1_S3_BUCKET=bucket-test
            PGBACKREST_REPO1_S3_KEY=key-id
            PGBACKREST_REPO1_S3_KEY_SECRET=key-secret-value
            PGBACKREST_REPO1_CIPHER_PASS=cipher-pass-value
            """), encoding="utf-8")

    def run(self, script: Path, *args, **extra) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.dir),
            "FAKE_LOG": str(self.log),
            "FAKE_DIR": str(self.dir),
            "ENV_FILE": str(self.env_file),
            "LOCK_FILE": str(self.dir / "lock"),
            "DISK_PATH": str(self.dir),
            **extra,
        }
        return subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def docker_calls(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("docker ")]

    def pings(self) -> list[str]:
        prefix = f"curl https://hc-ping.com/{PING_KEY}/"
        return [c[len(prefix):].split()[0] for c in self.calls() if c.startswith(prefix)]

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class SyntaxTest(unittest.TestCase):
    def test_bash_syntax(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                subprocess.run(["bash", "-n", str(script)], check=True)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck не установлен")
    def test_shellcheck(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                subprocess.run(["shellcheck", "-x", "-e", "SC1091", str(script)], check=True)

    def test_scripts_executable(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                self.assertTrue(os.access(script, os.X_OK))


class ScriptRunTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEnv()
        self.addCleanup(self.fake.cleanup)

    def test_backup_full_success(self):
        result = self.fake.run(BACKUP / "backup.sh", "full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pgbackrest --stanza=dealers --type=full backup", "\n".join(self.fake.calls()))
        self.assertEqual(self.fake.pings(), ["test-backup-full/start", "test-backup-full/0"])

    def test_backup_failure_reported(self):
        result = self.fake.run(BACKUP / "backup.sh", "diff", FAKE_FAIL="backup")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fake.pings(), ["test-backup-diff/start", "test-backup-diff/1"])

    def test_backup_wrong_type(self):
        result = self.fake.run(BACKUP / "backup.sh", "incr")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.fake.calls(), [])

    def test_ping_key_not_on_command_line(self):
        self.fake.run(BACKUP / "wal-check.sh")
        curl_calls = [c for c in self.fake.calls() if c.startswith("curl ")]
        self.assertTrue(curl_calls)
        for call in curl_calls:
            # В журнале фальшивого curl URL — из stdin (-K -), аргументы — после него.
            self.assertNotIn(PING_KEY, call.split(" ", 2)[2])

    def test_no_ping_key_runs_job_without_signals(self):
        fake = FakeEnv(ping_key="")
        self.addCleanup(fake.cleanup)
        result = fake.run(BACKUP / "wal-check.sh")
        self.assertEqual(result.returncode, 0)
        self.assertIn("pgbackrest --stanza=dealers check", "\n".join(fake.calls()))
        self.assertFalse([c for c in fake.calls() if c.startswith("curl ")])

    def test_dump_runs_in_container(self):
        result = self.fake.run(BACKUP / "dump.sh")
        self.assertEqual(result.returncode, 0)
        self.assertIn("docker compose --project-directory", self.fake.docker_calls()[0])
        self.assertTrue(self.fake.docker_calls()[0].endswith("exec -T -u postgres postgres dealers-dump"))
        self.assertEqual(self.fake.pings(), ["test-dump/start", "test-dump/0"])

    def test_disk_below_limit(self):
        result = self.fake.run(BACKUP / "disk-check.sh", FAKE_DISK_USED="79")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.fake.pings(), ["test-disk/start", "test-disk/0"])

    def test_disk_over_limit(self):
        result = self.fake.run(BACKUP / "disk-check.sh", FAKE_DISK_USED="80")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.fake.pings(), ["test-disk/start", "test-disk/1"])
        self.assertIn("used 80%, limit 80%", result.stdout)


class RestoreCheckRunTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEnv()
        self.addCleanup(self.fake.cleanup)

    def assert_cleaned_up(self):
        calls = self.fake.docker_calls()
        self.assertEqual(calls[-1], "docker volume rm -f dealers_restore_check")
        self.assertEqual(calls[-2], "docker rm -f dealers-restore-check dealers-restore-check-restore")

    def test_success(self):
        result = self.fake.run(BACKUP / "restore-check.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.fake.calls()
        restore = next(c for c in calls if c.endswith(" restore"))
        self.assertIn("--pg1-path=/restore", restore)
        self.assertIn("--archive-mode=off", restore)
        self.assertIn("--type=name --target=check-", restore)
        self.assertIn("-v dealers_restore_check:/restore", restore)
        # Ключи хранилища — только именами (-e ИМЯ), значения не попадают в командную строку.
        self.assertNotIn("key-secret-value", "\n".join(calls))
        self.assertNotIn("cipher-pass-value", "\n".join(calls))
        self.assertIn("pgbackrest --stanza=dealers verify", "\n".join(calls))
        for db in ("platform", "n8n", "nocodb_meta"):
            self.assertIn(f"docker exec dealers-restore-check pg_dump -Fc -d {db} -f /dev/null", calls)
        self.assert_cleaned_up()
        self.assertEqual(self.fake.pings(), ["test-restore-check/start", "test-restore-check/0"])
        self.assertIn("1 tables, row counts equal", result.stdout)

    def test_restore_failure_cleans_up_and_reports(self):
        result = self.fake.run(BACKUP / "restore-check.sh", FAKE_FAIL="--archive-mode=off restore")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL: pgbackrest restore", result.stdout)
        self.assert_cleaned_up()
        self.assertEqual(self.fake.pings(), ["test-restore-check/start", "test-restore-check/1"])

    def test_row_count_mismatch_fails(self):
        result = self.fake.run(BACKUP / "restore-check.sh", FAKE_RESTORED_ROWS="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL: row counts differ", result.stdout)
        self.assert_cleaned_up()

    def test_missing_schema_fails(self):
        result = self.fake.run(BACKUP / "restore-check.sh", FAKE_SCHEMAS="catalog,content")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL: schemas", result.stdout)

    def test_wal_archive_failure_stops_before_restore(self):
        result = self.fake.run(BACKUP / "restore-check.sh", FAKE_FAIL="dealers check")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse([c for c in self.fake.calls() if c.endswith(" restore")])
        self.assert_cleaned_up()


class DumpScriptTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEnv()
        self.addCleanup(self.fake.cleanup)
        self.root = self.fake.dir / "dump-root"
        self.root.mkdir()
        (self.root / "2000-01-01").mkdir()
        self.env = {
            "DUMP_ROOT": str(self.root),
            "PGBACKREST_REPO1_S3_BUCKET": "bucket-test",
            "PGBACKREST_REPO1_S3_KEY": "key-id",
            "PGBACKREST_REPO1_S3_KEY_SECRET": "key-secret-value",
            "DUMP_CIPHER_PASS": "dump-pass-value",
        }

    def test_success(self):
        result = self.fake.run(DUMP_SCRIPT, **self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        days = [p.name for p in self.root.iterdir()]
        self.assertEqual(len(days), 1)
        day = days[0]
        self.assertNotEqual(day, "2000-01-01")
        files = sorted(p.name for p in (self.root / day).iterdir())
        self.assertEqual(files, ["SHA256SUMS", "globals.sql.enc", "n8n.dump.enc", "nocodb_meta.dump.enc",
                                 "platform.dump.enc"])
        calls = self.fake.calls()
        self.assertIn(f"rclone copy --retries 3 {self.root}/.partial-{day} yos:bucket-test/dump/{day}", calls)
        self.assertIn("rclone delete --min-age 30d yos:bucket-test/dump", calls)
        self.assertIn("pg_dumpall --globals-only", calls)
        openssl = [c for c in calls if c.startswith("openssl ")]
        self.assertEqual(len(openssl), 4)
        for call in openssl:
            self.assertIn("-aes-256-cbc -pbkdf2", call)
            self.assertIn("-pass env:DUMP_CIPHER_PASS", call)
        self.assertNotIn("dump-pass-value", "\n".join(calls))
        self.assertNotIn("key-secret-value", "\n".join(calls))

    def test_retention_variable(self):
        result = self.fake.run(DUMP_SCRIPT, DUMP_RETENTION_DAYS="14", **self.env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("rclone delete --min-age 14d yos:bucket-test/dump", self.fake.calls())

    def test_bad_retention_rejected(self):
        result = self.fake.run(DUMP_SCRIPT, DUMP_RETENTION_DAYS="0", **self.env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("DUMP_RETENTION_DAYS", result.stderr)

    def test_pg_dump_failure_uploads_nothing(self):
        result = self.fake.run(DUMP_SCRIPT, FAKE_FAIL="nocodb_meta", **self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse([c for c in self.fake.calls() if c.startswith("rclone ")])
        # Прошлый дамп на диске остаётся, раз новый не загружен.
        self.assertIn("2000-01-01", [p.name for p in self.root.iterdir()])

    def test_missing_cipher_pass(self):
        env = dict(self.env, DUMP_CIPHER_PASS="")
        result = self.fake.run(DUMP_SCRIPT, **env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "CONFIG ERROR: missing DUMP_CIPHER_PASS")
        self.assertEqual(self.fake.calls(), [])


if __name__ == "__main__":
    unittest.main()
