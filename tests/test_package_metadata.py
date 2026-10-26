import re
import unittest

import agent_core


class PackageMetadataTests(unittest.TestCase):
    def test_version_is_exposed_and_matches_project(self) -> None:
        self.assertRegex(agent_core.__version__, r"^\d+\.\d+\.\d+$")
        with open("pyproject.toml", encoding="utf-8") as handle:
            pyproject = handle.read()
        declared = re.search(r'^version = "(\d+\.\d+\.\d+)"', pyproject, re.MULTILINE)
        self.assertIsNotNone(declared)
        self.assertEqual(agent_core.__version__, declared.group(1))


if __name__ == "__main__":
    unittest.main()
