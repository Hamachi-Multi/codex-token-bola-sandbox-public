from __future__ import annotations

try:
    from tests.support import ROOT, unittest
except ModuleNotFoundError:
    from support import ROOT, unittest


class PackagingMetadataTests(unittest.TestCase):
    def test_pyproject_disables_implicit_flat_layout_package_discovery(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('license = "MIT"', text)
        self.assertIn("[tool.setuptools]", text)
        self.assertIn("py-modules = []", text)


if __name__ == "__main__":
    unittest.main()
