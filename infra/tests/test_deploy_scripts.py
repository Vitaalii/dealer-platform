"""Скрипты выкатки infra/deploy/ без сервера: bash -n, shellcheck (если есть) и запуск с подменёнными командами.

Вместо git, docker, flock и sleep в PATH кладутся фальшивые команды. Фальшивый реестр — файл registry
(какие образы можно скачать), фальшивый сервер — файл local (какие образы уже скачаны), head — на какой
коммит переключены файлы, running — какой коммит запущен. /health отвечает "ok <коммит>", а для коммита
из FAKE_BAD — "error <коммит>". На сервере скрипты проверяет владелец (infra/README.md, разделы 15–17).
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from ._compose import INFRA

DEPLOY = INFRA / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))
PREFIX = "ghcr.io/vitaalii/dealer-platform"
OLD = "a" * 40
NEW = "b" * 40
TAGGED = "c" * 40
SECRET = "postgres-password-not-real-0123456789"

FAKE_GIT = r'''#!/bin/bash
echo "git $*" >> "$FAKE_LOG"
if [ "$1" = "-C" ]; then shift 2; fi
last="${!#}"
case "$1" in
  fetch) [ -z "${FAKE_FETCH_FAIL:-}" ] ;;
  rev-parse)
    ref="${last%^\{commit\}}"
    case "$ref" in
      origin/main) echo "$FAKE_MAIN" ;;
      refs/tags/*) cat "$FAKE_DIR/tags/${ref#refs/tags/}" 2>/dev/null ;;
      *) [[ "$ref" =~ ^[0-9a-f]{40}$ ]] && echo "$ref" ;;
    esac ;;
  checkout) echo "$last" > "$FAKE_DIR/head" ;;
esac
'''

FAKE_DOCKER = r'''#!/bin/bash
echo "docker $*" >> "$FAKE_LOG"
last="${!#}"
case "$1" in
  pull)
    if grep -qxF "$last" "$FAKE_DIR/registry"; then echo "$last" >> "$FAKE_DIR/local"; exit 0; fi
    echo "Error response from daemon: manifest unknown: $last" >&2; exit 1 ;;
  image) grep -qxF "$last" "$FAKE_DIR/local" ;;
  compose)
    case "$*" in
      *" up -d"*) cp "$FAKE_DIR/head" "$FAKE_DIR/running"; echo "Container dealers-platform-1 Started" ;;
      *" exec -T platform "*)
        c=$(cat "$FAKE_DIR/running")
        if [ "$c" = "${FAKE_BAD:-}" ]; then echo "error $c"; else echo "ok $c"; fi ;;
    esac ;;
esac
'''

FAKE_TRUE = "#!/bin/bash\nexit 0\n"


def images(tag: str) -> list[str]:
    return [f"{PREFIX}/{img}:{tag}" for img in ("platform", "bot-stub", "postgres")]


class FakeServer:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="deploy-test-"))
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for name, body in (("git", FAKE_GIT), ("docker", FAKE_DOCKER), ("flock", FAKE_TRUE), ("sleep", FAKE_TRUE)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        (self.dir / "tags").mkdir()
        (self.dir / "repo").mkdir()
        self.state = self.dir / "state"
        self.state.mkdir()
        self.log = self.dir / "calls.log"
        self.log.touch()
        self.env_file = self.dir / ".env"
        self.env_file.write_text(f"PLATFORM_ENV=test\nPOSTGRES_PASSWORD={SECRET}\nIMAGE_PREFIX={PREFIX}\n"
                                 f"PLATFORM_TAG=sha-{OLD}\n", encoding="utf-8")
        self.env_file.chmod(0o600)
        # Сейчас стоит OLD: образы скачаны, файлы и контейнеры — из него.
        (self.dir / "registry").write_text("\n".join(images(f"sha-{OLD}")) + "\n", encoding="utf-8")
        (self.dir / "local").write_text("\n".join(images(f"sha-{OLD}")) + "\n", encoding="utf-8")
        (self.dir / "head").write_text(OLD + "\n", encoding="utf-8")
        (self.dir / "running").write_text(OLD + "\n", encoding="utf-8")
        self.set_state("current", f"sha-{OLD}")

    def publish(self, tag: str):
        with open(self.dir / "registry", "a", encoding="utf-8") as f:
            f.write("\n".join(images(tag)) + "\n")

    def tag(self, name: str, commit: str):
        (self.dir / "tags" / name).write_text(commit + "\n", encoding="utf-8")

    def set_state(self, name: str, value: str):
        (self.state / name).write_text(value + "\n", encoding="utf-8")

    def get_state(self, name: str) -> str | None:
        path = self.state / name
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    def running(self) -> str:
        return (self.dir / "running").read_text(encoding="utf-8").strip()

    def env_tag(self) -> str:
        lines = [l for l in self.env_file.read_text(encoding="utf-8").splitlines() if l.startswith("PLATFORM_TAG=")]
        self.assertion_single = len(lines)
        return lines[-1].partition("=")[2]

    def calls(self, prefix: str = "") -> list[str]:
        return [c for c in self.log.read_text(encoding="utf-8").splitlines() if c.startswith(prefix)]

    def run(self, script: str, *args, **extra) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.dir),
            "FAKE_LOG": str(self.log),
            "FAKE_DIR": str(self.dir),
            "FAKE_MAIN": NEW,
            "INFRA_DIR": str(self.dir),
            "REPO_DIR": str(self.dir / "repo"),
            "ENV_FILE": str(self.env_file),
            "STATE_DIR": str(self.state),
            "HEALTH_TIMEOUT_SEC": "15",
            **extra,
        }
        return subprocess.run(["bash", str(DEPLOY / script), *args], env=env, capture_output=True, text=True,
                              timeout=60)

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

    def test_scripts_read_fully_before_checkout(self):
        # git checkout внутри скрипта может заменить его файл: bash должен прочитать всё до запуска.
        for name in ("update.sh", "release.sh", "rollback.sh"):
            with self.subTest(script=name):
                lines = (DEPLOY / name).read_text(encoding="utf-8").rstrip().splitlines()
                self.assertEqual(lines[-1], 'main "$@"; exit $?')


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.s = FakeServer()
        self.addCleanup(self.s.cleanup)

    def test_same_commit_does_nothing(self):
        self.s.set_state("current", f"sha-{NEW}")
        result = self.s.run("update.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.s.calls("docker"), [])

    def test_new_commit_deployed(self):
        self.s.publish(f"sha-{NEW}")
        result = self.s.run("update.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"UPDATE OK sha-{NEW}", result.stdout)
        self.assertEqual(self.s.running(), NEW)
        self.assertEqual(self.s.env_tag(), f"sha-{NEW}")
        self.assertEqual(self.s.assertion_single, 1)
        self.assertEqual(self.s.get_state("current"), f"sha-{NEW}")
        self.assertEqual(self.s.get_state("previous"), f"sha-{OLD}")
        self.assertIn(f"git -C {self.s.dir / 'repo'} checkout -q --detach {NEW}", self.s.calls("git"))
        self.assertTrue(any(c.endswith("compose --project-directory " + str(self.s.dir) + " up -d --no-build")
                            for c in self.s.calls("docker")))

    def test_images_not_ready_changes_nothing(self):
        result = self.s.run("update.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not ready yet", result.stdout)
        self.assertEqual(self.s.running(), OLD)
        self.assertEqual(self.s.env_tag(), f"sha-{OLD}")
        self.assertEqual(self.s.get_state("current"), f"sha-{OLD}")
        self.assertEqual([c for c in self.s.calls("git") if " checkout " in c], [])
        self.assertEqual([c for c in self.s.calls("docker") if " up -d" in c], [])

    def test_unhealthy_update_rolls_back_and_is_not_retried(self):
        self.s.publish(f"sha-{NEW}")
        result = self.s.run("update.sh", FAKE_BAD=NEW)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"UPDATE FAILED sha-{NEW}", result.stdout)
        self.assertIn(f"rolled back to sha-{OLD}", result.stdout)
        self.assertEqual(self.s.running(), OLD)
        self.assertEqual(self.s.env_tag(), f"sha-{OLD}")
        self.assertEqual(self.s.get_state("current"), f"sha-{OLD}")
        self.assertEqual(self.s.get_state("failed"), f"sha-{NEW}")

        again = self.s.run("update.sh", FAKE_BAD=NEW)
        self.assertEqual(again.returncode, 0)
        self.assertEqual(again.stdout, "")

    def test_fetch_failure_reported(self):
        result = self.s.run("update.sh", FAKE_FETCH_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("git fetch failed", result.stdout)
        self.assertEqual(self.s.calls("docker"), [])

    def test_missing_image_prefix_stops(self):
        self.s.env_file.write_text("PLATFORM_ENV=test\n", encoding="utf-8")
        self.s.publish(f"sha-{NEW}")
        result = self.s.run("update.sh")
        self.assertIn("CONFIG ERROR: missing IMAGE_PREFIX", result.stdout)
        self.assertEqual(self.s.running(), OLD)

    def test_secrets_not_printed_and_env_preserved(self):
        self.s.publish(f"sha-{NEW}")
        result = self.s.run("update.sh", FAKE_BAD=NEW)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        self.assertNotIn(SECRET, self.s.log.read_text(encoding="utf-8"))
        text = self.s.env_file.read_text(encoding="utf-8")
        self.assertIn(f"POSTGRES_PASSWORD={SECRET}\n", text)
        self.assertIn(f"IMAGE_PREFIX={PREFIX}\n", text)
        self.assertEqual(stat.S_IMODE(self.s.env_file.stat().st_mode), 0o600)

    def test_first_update_without_current(self):
        (self.s.state / "current").unlink()
        self.s.env_file.write_text(f"IMAGE_PREFIX={PREFIX}\n", encoding="utf-8")
        self.s.publish(f"sha-{NEW}")
        result = self.s.run("update.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.s.env_tag(), f"sha-{NEW}")
        self.assertEqual(self.s.get_state("current"), f"sha-{NEW}")
        self.assertIsNone(self.s.get_state("previous"))


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.s = FakeServer()
        self.addCleanup(self.s.cleanup)

    def test_usage(self):
        for arg in ((), ("0.1.0",), ("v0.1",), ("v0.1.0; rm -rf /",)):
            with self.subTest(arg=arg):
                result = self.s.run("release.sh", *arg)
                self.assertEqual(result.returncode, 64)
                self.assertIn("usage", result.stdout)
        self.assertEqual(self.s.calls(), [])

    def test_release_deployed(self):
        self.s.tag("v0.1.0", TAGGED)
        self.s.publish("v0.1.0")
        result = self.s.run("release.sh", "v0.1.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RELEASE OK v0.1.0", result.stdout)
        self.assertIn(f"git -C {self.s.dir / 'repo'} fetch -q --tags origin", self.s.calls("git"))
        self.assertEqual(self.s.running(), TAGGED)
        self.assertEqual(self.s.env_tag(), "v0.1.0")
        self.assertEqual(self.s.get_state("current"), "v0.1.0")
        self.assertEqual(self.s.get_state("previous"), f"sha-{OLD}")

    def test_unknown_tag(self):
        result = self.s.run("release.sh", "v9.9.9")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tag v9.9.9 not found", result.stdout)
        self.assertEqual(self.s.calls("docker"), [])

    def test_tag_without_images(self):
        self.s.tag("v0.1.0", TAGGED)
        result = self.s.run("release.sh", "v0.1.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("RELEASE FAILED v0.1.0: images not found", result.stdout)
        self.assertEqual(self.s.running(), OLD)
        self.assertEqual(self.s.get_state("current"), f"sha-{OLD}")

    def test_unhealthy_release_rolls_back(self):
        self.s.tag("v0.1.0", TAGGED)
        self.s.publish("v0.1.0")
        result = self.s.run("release.sh", "v0.1.0", FAKE_BAD=TAGGED)
        self.assertEqual(result.returncode, 1)
        self.assertIn("RELEASE FAILED v0.1.0", result.stdout)
        self.assertEqual(self.s.running(), OLD)
        self.assertEqual(self.s.env_tag(), f"sha-{OLD}")
        self.assertEqual(self.s.get_state("failed"), "v0.1.0")


class RollbackTest(unittest.TestCase):
    def setUp(self):
        self.s = FakeServer()
        self.addCleanup(self.s.cleanup)
        # Стоит NEW, до него стоял OLD; в реестре OLD уже нет — откат берёт образы с сервера.
        self.s.publish(f"sha-{NEW}")
        self.assertEqual(self.s.run("update.sh").returncode, 0)
        (self.s.dir / "registry").write_text("\n".join(images(f"sha-{NEW}")) + "\n", encoding="utf-8")

    def test_rollback_to_previous(self):
        result = self.s.run("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1].split(": ", 1)[1], f"ROLLBACK OK sha-{OLD}")
        self.assertEqual(self.s.running(), OLD)
        self.assertEqual(self.s.env_tag(), f"sha-{OLD}")
        self.assertEqual(self.s.get_state("current"), f"sha-{OLD}")
        self.assertEqual(self.s.get_state("previous"), f"sha-{NEW}")
        self.assertEqual(self.s.get_state("failed"), f"sha-{NEW}")
        # Таймер не вернёт откаченную версию.
        again = self.s.run("update.sh")
        self.assertEqual(again.returncode, 0)
        self.assertEqual(self.s.running(), OLD)

    def test_rollback_to_explicit_version(self):
        self.s.tag("v0.1.0", TAGGED)
        self.s.publish("v0.1.0")
        result = self.s.run("rollback.sh", "v0.1.0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.s.running(), TAGGED)

    def test_no_previous(self):
        (self.s.state / "previous").unlink()
        result = self.s.run("rollback.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no previous version", result.stdout)
        self.assertEqual(self.s.running(), NEW)

    def test_failed_rollback_reported(self):
        result = self.s.run("rollback.sh", FAKE_BAD=OLD)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ROLLBACK FAILED sha-{OLD}", result.stdout)
        self.assertEqual(self.s.get_state("current"), f"sha-{NEW}")


class SystemdUnitsTest(unittest.TestCase):
    def test_service(self):
        text = (DEPLOY / "dealers-update.service").read_text(encoding="utf-8")
        for line in ("Type=oneshot", "User=deploy", "WorkingDirectory=/home/deploy/dealer-platform/infra",
                     "ExecStart=/home/deploy/dealer-platform/infra/deploy/update.sh"):
            with self.subTest(line=line):
                self.assertIn(line + "\n", text)

    def test_timer(self):
        text = (DEPLOY / "dealers-update.timer").read_text(encoding="utf-8")
        for line in ("[Timer]", "OnBootSec=2min", "OnUnitActiveSec=5min", "WantedBy=timers.target"):
            with self.subTest(line=line):
                self.assertIn(line + "\n", text)


if __name__ == "__main__":
    unittest.main()
