"""GitHub Actions (.github/workflows/) без GitHub: разбор текста, без pyyaml.

Проверяется то, что обещает план 5.2 (PDL-35, п. 1 и 7): секретов нет, кроме GITHUB_TOKEN; права
минимальные; действия закреплены по SHA; триггеры; имена образов совпадают с compose.yaml и скриптами
выкатки. Первый настоящий запуск — только после слияния PR (вкладка Actions).
"""
import re
import unittest

from ._compose import INFRA, read, services

WORKFLOWS_DIR = INFRA.parent / ".github" / "workflows"
WF = {p.stem: p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS_DIR.glob("*.yml"))}
PREFIX = "ghcr.io/vitaalii/dealer-platform"
IMAGES = {"platform", "bot-stub", "postgres"}
COMPOSE = services(INFRA / "compose.yaml")
TEST_COMMANDS = ("python3 -m unittest discover -s core/tests -t core",
                 "python3 -m unittest discover -s infra/tests -t infra")


def top_block(text: str, key: str) -> list[str]:
    """Строки блока верхнего уровня `key:` (без комментариев и пустых)."""
    lines, inside = [], False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            inside = line.startswith(f"{key}:")
            continue
        if inside:
            lines.append(line.strip())
    return lines


class WorkflowsTest(unittest.TestCase):
    def test_three_workflows(self):
        self.assertEqual(set(WF), {"ci", "build", "release"})

    def test_only_github_token_secret(self):
        for name, text in WF.items():
            with self.subTest(workflow=name):
                self.assertLessEqual(set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text)), {"GITHUB_TOKEN"})

    def test_token_only_via_password_stdin(self):
        for name, text in WF.items():
            for line in text.splitlines():
                if "GHCR_TOKEN" in line and "secrets.GITHUB_TOKEN" not in line:
                    with self.subTest(workflow=name, line=line.strip()):
                        self.assertIn("--password-stdin", line)
                        self.assertNotIn("echo", line)

    def test_no_env_dump_and_no_secret_build_args(self):
        for name, text in WF.items():
            with self.subTest(workflow=name):
                self.assertNotRegex(text, r"\b(printenv|env\s*$|set\s+-x)\b")
                self.assertEqual(set(re.findall(r"--build-arg \"?([A-Z0-9_]+)=", text)) - {"PLATFORM_VERSION"}, set())

    def test_actions_pinned_by_sha(self):
        for name, text in WF.items():
            uses = re.findall(r"uses:\s*(\S+)", text)
            self.assertTrue(uses, name)
            for ref in uses:
                with self.subTest(workflow=name, uses=ref):
                    self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")

    def test_checkout_does_not_keep_token(self):
        for name, text in WF.items():
            with self.subTest(workflow=name):
                self.assertEqual(text.count("uses: actions/checkout@"), text.count("persist-credentials: false"))

    def test_minimal_permissions(self):
        expected = {
            "ci": ["contents: read"],
            "build": ["contents: read", "packages: write"],
            "release": ["contents: read", "packages: write"],
        }
        for name, perms in expected.items():
            with self.subTest(workflow=name):
                self.assertEqual(top_block(WF[name], "permissions"), perms)
                # Права только на уровне файла, задания их не расширяют.
                self.assertEqual(WF[name].count("permissions:"), 1)

    def test_triggers(self):
        self.assertEqual(top_block(WF["ci"], "on"), ["pull_request:", "branches: [main]"])
        self.assertEqual(top_block(WF["build"], "on"), ["push:", "branches: [main]"])
        self.assertEqual(top_block(WF["release"], "on"),
                         ["push:", "tags: ['v*']", "release:", "types: [published]"])

    def test_tests_run_before_images(self):
        for name in ("ci", "build"):
            with self.subTest(workflow=name):
                for cmd in TEST_COMMANDS:
                    self.assertIn(cmd, WF[name])
                self.assertIn("needs: test", WF[name])
        readme = (INFRA / "README.md").read_text(encoding="utf-8")
        self.assertIn(TEST_COMMANDS[1], readme)

    def test_ci_does_not_push(self):
        self.assertNotIn("docker push", WF["ci"])
        self.assertNotIn("docker login", WF["ci"])
        self.assertNotIn("packages: write", WF["ci"])

    def test_build_pushes_three_images_with_sha_and_main(self):
        text = WF["build"]
        self.assertIn(f"IMAGE_PREFIX: {PREFIX}", text)
        built = set(re.findall(r'-t "\$\{IMAGE_PREFIX\}/([a-z-]+):sha-\$\{GITHUB_SHA\}"', text))
        self.assertEqual(built, IMAGES)
        for img in IMAGES:
            with self.subTest(image=img):
                self.assertIn(f'-t "${{IMAGE_PREFIX}}/{img}:main"', text)
        self.assertIn("for img in platform bot-stub postgres; do", text)
        self.assertIn('docker push "${IMAGE_PREFIX}/${img}:sha-${GITHUB_SHA}"', text)

    def test_build_contexts_match_compose(self):
        # Те же Dockerfile и контексты, что в compose.yaml (сборка на сервере — запасной путь).
        self.assertIn("context: ..", COMPOSE["platform"])
        self.assertIn("dockerfile: infra/platform/Dockerfile", COMPOSE["platform"])
        self.assertIn("build: ./bot-stub", COMPOSE["bot"])
        self.assertIn("build: ./postgres", COMPOSE["postgres"])
        for name in ("ci", "build"):
            text = WF[name]
            with self.subTest(workflow=name):
                self.assertRegex(text, r"docker build -f infra/platform/Dockerfile --build-arg \"PLATFORM_VERSION=\$\{GITHUB_SHA\}\" .* \.\n")
                self.assertRegex(text, r"docker build .*bot-stub.* infra/bot-stub\n")
                self.assertRegex(text, r"docker build .*postgres.* infra/postgres\n")

    def test_release_checks_and_retags_without_rebuild(self):
        text = WF["release"]
        self.assertIn(f"IMAGE_PREFIX: {PREFIX}", text)
        self.assertIn("OWNER: Vitaalii", text)
        self.assertIn('[ "$GITHUB_ACTOR" != "$OWNER" ]', text)
        self.assertIn(r'[[ "$GITHUB_REF_NAME" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]', text)
        self.assertIn('git merge-base --is-ancestor "$commit" origin/main', text)
        self.assertIn("fetch-depth: 0", text)
        self.assertIn('docker buildx imagetools inspect "${IMAGE_PREFIX}/${img}:sha-${RELEASE_COMMIT}"', text)
        self.assertIn('docker buildx imagetools create -t "${IMAGE_PREFIX}/${img}:${GITHUB_REF_NAME}" '
                      '"${IMAGE_PREFIX}/${img}:sha-${RELEASE_COMMIT}"', text)
        self.assertNotIn("docker build ", text)
        # Проверки — раньше входа в реестр и метки.
        self.assertLess(text.index("Проверка метки"), text.index("Вход в GHCR"))
        self.assertLess(text.index("Образы sha-коммита есть"), text.index("Метка релиза на образы"))


class ImageNamesTest(unittest.TestCase):
    def test_compose_images_from_prefix_and_tag(self):
        expected = {
            "platform": "${IMAGE_PREFIX:-dealers}/platform:${PLATFORM_TAG:-latest}",
            "migrate": "${IMAGE_PREFIX:-dealers}/platform:${PLATFORM_TAG:-latest}",
            "bot": "${IMAGE_PREFIX:-dealers}/bot-stub:${PLATFORM_TAG:-latest}",
            "postgres": "${IMAGE_PREFIX:-dealers}/postgres:${PLATFORM_TAG:-17}",
        }
        for service, image in expected.items():
            with self.subTest(service=service):
                self.assertIn(f"image: {image}", COMPOSE[service])

    def test_migrate_can_pull(self):
        self.assertNotIn("pull_policy", COMPOSE["migrate"])

    def test_third_party_versions_pinned(self):
        self.assertIn("image: caddy:${CADDY_VERSION:-2.11.7}", COMPOSE["caddy"])
        self.assertIn("image: n8nio/n8n:${N8N_VERSION:-2.43.0}", COMPOSE["n8n"])

    def test_deploy_scripts_use_same_images(self):
        lib = read(INFRA / "deploy" / "lib.sh")
        self.assertIn("IMAGES=(platform bot-stub postgres)", lib)

    def test_prefix_documented(self):
        env_example = (INFRA / ".env.example").read_text(encoding="utf-8")
        self.assertIn(f"# Реестр образов платформы, бота и базы. Тест и бой: {PREFIX}", env_example)
        self.assertIn(f"IMAGE_PREFIX={PREFIX}", (INFRA / "README.md").read_text(encoding="utf-8"))

    def test_dockerfiles_link_package_to_repo(self):
        for path in ("platform/Dockerfile", "bot-stub/Dockerfile", "postgres/Dockerfile"):
            with self.subTest(dockerfile=path):
                self.assertIn("LABEL org.opencontainers.image.source=https://github.com/Vitaalii/dealer-platform",
                              (INFRA / path).read_text(encoding="utf-8"))

    def test_restore_check_uses_compose_image(self):
        text = read(INFRA / "backup" / "restore-check.sh")
        self.assertIn("IMAGE=${RESTORE_IMAGE:-${IMAGE_PREFIX:-dealers}/postgres:${PLATFORM_TAG:-17}}", text)


if __name__ == "__main__":
    unittest.main()
