from __future__ import annotations

import unittest

try:
    from tests.test_dashboard_cleanup_api import DashboardCleanupApiTests
    from tests.test_dashboard_cleanup_payload import DashboardCleanupPayloadTests
    from tests.test_dashboard_cleanup_retention_preview import DashboardCleanupRetentionPreviewTests
    from tests.test_dashboard_cleanup_retention_prune import DashboardCleanupRetentionPruneTests
    from tests.test_dashboard_cleanup_ui import DashboardCleanupUiTests
except ModuleNotFoundError:
    from test_dashboard_cleanup_api import DashboardCleanupApiTests
    from test_dashboard_cleanup_payload import DashboardCleanupPayloadTests
    from test_dashboard_cleanup_retention_preview import DashboardCleanupRetentionPreviewTests
    from test_dashboard_cleanup_retention_prune import DashboardCleanupRetentionPruneTests
    from test_dashboard_cleanup_ui import DashboardCleanupUiTests

__all__ = [
    "DashboardCleanupApiTests",
    "DashboardCleanupPayloadTests",
    "DashboardCleanupRetentionPreviewTests",
    "DashboardCleanupRetentionPruneTests",
    "DashboardCleanupUiTests",
]


def aggregate_suite(loader: unittest.TestLoader) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    for case in (
        DashboardCleanupApiTests,
        DashboardCleanupPayloadTests,
        DashboardCleanupRetentionPreviewTests,
        DashboardCleanupRetentionPruneTests,
        DashboardCleanupUiTests,
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
