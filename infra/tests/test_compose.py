import re
import unittest

from ._compose import INFRA, services

PROD = services(INFRA / "compose.yaml")
TEST = services(INFRA / "compose.test.yaml")
EXPECTED = {"caddy", "postgres", "migrate", "platform", "nocodb", "n8n", "bot"}


def mem_mib(block: str) -> int:
    value = re.search(r"mem_limit:\s*(\d+)([mg])", block)
    num, unit = int(value.group(1)), value.group(2)
    return num * 1024 if unit == "g" else num


class ComposeTest(unittest.TestCase):
    def test_services_set(self):
        self.assertEqual(set(PROD), EXPECTED)
        self.assertEqual(set(TEST), EXPECTED)

    def test_every_service_has_restart_and_memory_limit(self):
        for name, block in PROD.items():
            with self.subTest(service=name):
                self.assertRegex(block, r"mem_limit:\s*\d+[mg]")
                expected = 'restart: "no"' if name == "migrate" else "restart: unless-stopped"
                self.assertIn(expected, block)

    def test_test_overrides_lower_every_limit(self):
        for name in EXPECTED:
            with self.subTest(service=name):
                self.assertLessEqual(mem_mib(TEST[name]), mem_mib(PROD[name]))

    def test_prod_limits_match_architecture_7_3(self):
        expected = {"caddy": 256, "postgres": 2048, "platform": 1536, "nocodb": 2048, "n8n": 3072, "bot": 1024}
        self.assertEqual({n: mem_mib(PROD[n]) for n in expected}, expected)

    def test_test_limits_fit_6gb_server(self):
        total = sum(mem_mib(TEST[n]) for n in EXPECTED if n != "migrate")
        self.assertEqual(total, 128 + 768 + 512 + 1536 + 1024 + 512)
        self.assertLess(total, 5 * 1024)

    def test_only_caddy_publishes_ports(self):
        for name, block in {**PROD, **TEST}.items():
            if name != "caddy":
                with self.subTest(service=name):
                    self.assertNotIn("ports:", block)
                    self.assertNotIn("expose:", block)
        self.assertIn('"80:80"', PROD["caddy"])
        self.assertIn('"443:443"', PROD["caddy"])

    def test_long_running_services_have_healthcheck(self):
        for name in EXPECTED - {"migrate"}:
            with self.subTest(service=name):
                self.assertIn("healthcheck:", PROD[name])

    def test_platform_waits_for_migrations(self):
        self.assertIn("service_completed_successfully", PROD["platform"])
        self.assertIn("service_healthy", PROD["migrate"])

    def test_n8n_settings_from_architecture_7_3(self):
        n8n = PROD["n8n"]
        for line in ("DB_TYPE: postgresdb", "DB_POSTGRESDB_DATABASE: n8n", "N8N_DEFAULT_BINARY_DATA_MODE: filesystem",
                     'EXECUTIONS_DATA_MAX_AGE: "72"', 'EXECUTIONS_DATA_PRUNE_MAX_COUNT: "60000"',
                     'N8N_CONCURRENCY_PRODUCTION_LIMIT: "20"', "--max-old-space-size=2304"):
            with self.subTest(line=line):
                self.assertIn(line, n8n)

    def test_no_literal_secrets(self):
        for path in (INFRA / "compose.yaml", INFRA / "compose.test.yaml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^\s*([A-Z0-9_]*(PASSWORD|SECRET|KEY)[A-Z0-9_]*):\s*(.+)$", line)
                if m:
                    with self.subTest(line=line.strip()):
                        self.assertRegex(m.group(3), r"^\$\{[A-Z0-9_]+:\?")


if __name__ == "__main__":
    unittest.main()
