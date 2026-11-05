import unittest

import registry


class SmokeTests(unittest.TestCase):
    def test_registry_is_a_list(self):
        self.assertIsInstance(registry.FEATURES, list)


if __name__ == "__main__":
    unittest.main()
