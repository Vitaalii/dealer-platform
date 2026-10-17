"""Бэкапы 5.3 без сервера: настройки pgBackRest, архив журнала в compose, образ, расписание."""
import re
import unittest

from ._compose import INFRA, read, services

CONF = (INFRA / "postgres" / "pgbackrest" / "pgbackrest.conf").read_text(encoding="utf-8")
DOCKERFILE = read(INFRA / "postgres" / "Dockerfile")
PROD = services(INFRA / "compose.yaml")
TEST = services(INFRA / "compose.test.yaml")
CRONTAB = (INFRA / "backup" / "crontab").read_text(encoding="utf-8")
RESTORE = read(INFRA / "backup" / "restore-check.sh")
ARCHIVE_ARGS = ('"archive_mode=on"', '"archive_timeout=60"',
                '"archive_command=pgbackrest --stanza=dealers archive-push %p"')
SECRET_ENV = ("PGBACKREST_REPO1_S3_BUCKET", "PGBACKREST_REPO1_S3_KEY", "PGBACKREST_REPO1_S3_KEY_SECRET",
              "PGBACKREST_REPO1_CIPHER_PASS", "DUMP_CIPHER_PASS")


def conf_options(text: str) -> dict[str, str]:
    """Опции раздела [global] и [dealers]: имя -> значение."""
    found, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        name, _, value = line.partition("=")
        found[f"{section}/{name}"] = value
    return found


def cron_jobs(text: str) -> list[tuple[str, str, str, str]]:
    """(минута, час, день недели, скрипт) для каждой строки расписания."""
    jobs = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#") or "=" in line.split()[0]:
            continue
        fields = line.split(None, 5)
        script = re.search(r"\./backup/([a-z-]+\.sh)( full| diff)?", fields[5])
        jobs.append((fields[0], fields[1], fields[4], script.group(1) + (script.group(2) or "")))
    return jobs


class PgBackRestConfTest(unittest.TestCase):
    def test_options_from_plan(self):
        opts = conf_options(CONF)
        expected = {
            "global/repo1-type": "s3",
            "global/repo1-s3-endpoint": "storage.yandexcloud.net",
            "global/repo1-s3-region": "ru-central1",
            "global/repo1-s3-uri-style": "path",
            "global/repo1-retention-full-type": "time",
            "global/repo1-retention-full": "30",
            "global/repo1-cipher-type": "aes-256-cbc",
            "global/archive-async": "y",
            "global/process-max": "1",
            "dealers/pg1-path": "/var/lib/postgresql/data",
        }
        self.assertEqual({k: opts.get(k) for k in expected}, expected)

    def test_no_secrets_in_conf(self):
        opts = conf_options(CONF)
        for name in ("repo1-s3-bucket", "repo1-s3-key", "repo1-s3-key-secret", "repo1-cipher-pass"):
            with self.subTest(option=name):
                self.assertNotIn(f"global/{name}", opts)

    def test_archive_queue_limited(self):
        self.assertIn("global/archive-push-queue-max", conf_options(CONF))


class ComposeArchiveTest(unittest.TestCase):
    def test_archive_on_in_both_compose_files(self):
        # compose.test.yaml заменяет command целиком — архив должен быть в обоих файлах.
        for name, block in (("compose.yaml", PROD["postgres"]), ("compose.test.yaml", TEST["postgres"])):
            for arg in ARCHIVE_ARGS:
                with self.subTest(file=name, arg=arg):
                    self.assertIn(arg, block)

    def test_backup_secrets_required(self):
        for name in SECRET_ENV:
            with self.subTest(var=name):
                self.assertIn(f"{name}: ${{{name}:?", PROD["postgres"])
        self.assertIn("DUMP_RETENTION_DAYS: ${DUMP_RETENTION_DAYS:-30}", PROD["postgres"])

    def test_spool_and_dump_volumes(self):
        self.assertIn("pg_spool:/var/spool/pgbackrest", PROD["postgres"])
        self.assertIn("pg_dump:/var/backups/dealers-dump", PROD["postgres"])


class ImageTest(unittest.TestCase):
    def test_image_has_tools_and_config(self):
        self.assertRegex(DOCKERFILE, r"apt-get install[^\n]*pgbackrest[^\n]*rclone[^\n]*openssl")
        self.assertIn("COPY pgbackrest/pgbackrest.conf /etc/pgbackrest/pgbackrest.conf", DOCKERFILE)
        self.assertIn("COPY bin/dealers-dump.sh /usr/local/bin/dealers-dump", DOCKERFILE)

    def test_writable_dirs_for_postgres_user(self):
        for path in ("/var/spool/pgbackrest", "/var/backups/dealers-dump", "/restore"):
            with self.subTest(path=path):
                self.assertRegex(DOCKERFILE, rf"chown postgres:postgres[^\n]*{re.escape(path)}")


class RestoreSafetyTest(unittest.TestCase):
    def test_restore_does_not_write_shared_archive(self):
        self.assertIn("--archive-mode=off restore", RESTORE)
        self.assertIn("-c archive_mode=off", RESTORE)

    def test_restore_goes_to_separate_volume(self):
        path = re.search(r"^RESTORE_PATH=(\S+)", RESTORE, re.M).group(1)
        self.assertNotEqual(path, "/var/lib/postgresql/data")
        self.assertIn('--pg1-path="$RESTORE_PATH"', RESTORE)
        # Временный контейнер — отдельный docker run, без рабочего тома pg_data.
        self.assertNotIn("pg_data", RESTORE)
        self.assertNotIn("compose run", RESTORE)

    def test_temporary_postgres_has_memory_limit_and_no_ports(self):
        self.assertIn('-m "$RESTORE_MEM"', RESTORE)
        self.assertIn("RESTORE_MEM=${RESTORE_MEM_LIMIT:-512m}", RESTORE)
        self.assertNotRegex(RESTORE, r"\s-p\s|--publish")
        self.assertIn("listen_addresses=", RESTORE)

    def test_cleanup_on_exit(self):
        self.assertIn("trap cleanup EXIT", RESTORE)


class CrontabTest(unittest.TestCase):
    JOBS = cron_jobs(CRONTAB)

    def test_every_script_scheduled_once(self):
        scripts = sorted(job[3] for job in self.JOBS)
        self.assertEqual(scripts, sorted(["backup.sh full", "backup.sh diff", "dump.sh", "restore-check.sh",
                                          "wal-check.sh", "disk-check.sh"]))
        for job in self.JOBS:
            with self.subTest(script=job[3]):
                self.assertTrue((INFRA / "backup" / job[3].split()[0]).is_file())

    def test_weekly_full_daily_diff(self):
        jobs = {job[3]: job for job in self.JOBS}
        self.assertEqual(jobs["backup.sh full"][2], "0")
        self.assertEqual(jobs["backup.sh diff"][2], "1-6")
        self.assertEqual(jobs["dump.sh"][2], "*")
        self.assertEqual(jobs["restore-check.sh"][2], "1")
        self.assertEqual((jobs["wal-check.sh"][1], jobs["disk-check.sh"][1]), ("*", "*"))

    def test_order_backup_dump_restore(self):
        jobs = {job[3]: job for job in self.JOBS}
        hours = [int(jobs[name][1]) for name in ("backup.sh full", "dump.sh", "restore-check.sh")]
        self.assertEqual(hours, sorted(set(hours)))
        self.assertEqual(jobs["backup.sh full"][1], jobs["backup.sh diff"][1])

    def test_output_goes_to_log(self):
        for line in CRONTAB.splitlines():
            if "./backup/" in line and not line.startswith("#"):
                with self.subTest(line=line):
                    self.assertIn(">> ~/dealers-backup.log 2>&1", line)


if __name__ == "__main__":
    unittest.main()
