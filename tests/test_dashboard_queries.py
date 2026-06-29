from __future__ import annotations

import unittest

try:
    from tests.test_dashboard_api_queries import DashboardApiQueryTests
    from tests.test_dashboard_cleanup import (
        DashboardCleanupApiTests,
        DashboardCleanupPayloadTests,
        DashboardCleanupRetentionPreviewTests,
        DashboardCleanupRetentionPruneTests,
        DashboardCleanupUiTests,
    )
    from tests.test_dashboard_ui_contract import DashboardUiContractTests
except ModuleNotFoundError:
    from test_dashboard_api_queries import DashboardApiQueryTests
    from test_dashboard_cleanup import (
        DashboardCleanupApiTests,
        DashboardCleanupPayloadTests,
        DashboardCleanupRetentionPreviewTests,
        DashboardCleanupRetentionPruneTests,
        DashboardCleanupUiTests,
    )
    from test_dashboard_ui_contract import DashboardUiContractTests


class DashboardQueryTests(
    DashboardApiQueryTests,
    DashboardCleanupPayloadTests,
    DashboardCleanupApiTests,
    DashboardCleanupRetentionPreviewTests,
    DashboardCleanupRetentionPruneTests,
    DashboardCleanupUiTests,
    DashboardUiContractTests,
):
    pass


def aggregate_suite(loader: unittest.TestLoader) -> unittest.TestSuite:
    return loader.loadTestsFromTestCase(DashboardQueryTests)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    if pattern is None:
        return aggregate_suite(loader)
    return unittest.TestSuite()


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    raise SystemExit(0 if runner.run(aggregate_suite(unittest.defaultTestLoader)).wasSuccessful() else 1)
