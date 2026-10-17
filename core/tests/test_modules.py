import unittest

from . import _paths  # noqa: F401
from platform_core.modules import MODULES, load_modules, setup_modules


class ModulesTest(unittest.TestCase):
    def test_all_modules_have_setup(self):
        self.assertEqual([m.__name__ for m in load_modules()], list(MODULES))

    def test_setup_modules_returns_names(self):
        self.assertEqual(setup_modules(object()), ["leads", "dealer_cabinet", "billing"])

    def test_module_without_setup_rejected(self):
        with self.assertRaises(RuntimeError):
            load_modules(("json",))


if __name__ == "__main__":
    unittest.main()
