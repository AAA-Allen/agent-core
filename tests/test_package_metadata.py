import unittest

import agent_core


class PackageMetadataTests(unittest.TestCase):
    def test_version_is_exposed(self) -> None:
        self.assertEqual(agent_core.__version__, "0.1.1")


if __name__ == "__main__":
    unittest.main()
