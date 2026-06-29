from __future__ import annotations

import unittest

try:
    from tests.test_tool_timing import ToolTimingTests
    from tests.test_dashboard_queries import DashboardQueryTests
    from tests.test_normalize import NormalizeTests
    from tests.test_cli_contract import CliContractTests
    from tests.test_privacy_defaults import PrivacyDefaultTests
except ModuleNotFoundError:
    from test_tool_timing import ToolTimingTests
    from test_dashboard_queries import DashboardQueryTests
    from test_normalize import NormalizeTests
    from test_cli_contract import CliContractTests
    from test_privacy_defaults import PrivacyDefaultTests

__all__ = [
    "DashboardQueryTests",
    "NormalizeTests",
    "PrivacyDefaultTests",
    "CliContractTests",
    "ToolTimingTests",
]


def aggregate_suite(loader: unittest.TestLoader) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    for case in (
        ToolTimingTests,
        DashboardQueryTests,
        NormalizeTests,
        CliContractTests,
        PrivacyDefaultTests,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    if pattern is None:
        return aggregate_suite(loader)
    return unittest.TestSuite()


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    raise SystemExit(0 if runner.run(aggregate_suite(unittest.defaultTestLoader)).wasSuccessful() else 1)
