"""Unit coverage for database status/value formatting helpers."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

from plugins import TestPlugin, TestResult


class TestGetPostgresDatabaseStatusFormatting(TestPlugin):
    """Verifies get_postgres_database_status reports spec values and status."""

    tool_name = "get_postgres_database_status"
    description = "Verifies database status/value formatting without Kubernetes"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestCreatePostgresDatabaseLocaleDryRun"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        database_crd = {
            "metadata": {
                "name": "dry-run-db-localized-db",
                "namespace": "default",
                "generation": 3,
                "resourceVersion": "12345",
            },
            "spec": {
                "name": "localized-db",
                "owner": "app",
                "cluster": {"name": "dry-run-db"},
                "ensure": "present",
                "databaseReclaimPolicy": "retain",
                "encoding": "UTF8",
                "localeProvider": "icu",
                "localeCollate": "en_US.UTF-8",
                "localeCType": "en_US.UTF-8",
                "icuLocale": "en-US",
                "collationVersion": "153.120",
            },
            "status": {
                "observedGeneration": 3,
                "applied": True,
                "message": "Database reconciled",
            },
        }

        try:
            src_dir = Path(__file__).resolve().parents[2] / "src"
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            from cnpg_mcp_tools import get_postgres_database_status

            with patch("cnpg_mcp_tools.get_cnpg_database", return_value=database_crd):
                get_status_impl = get_postgres_database_status.__wrapped__
                text = await get_status_impl(
                    None,
                    cluster_name="dry-run-db",
                    database_name="localized-db",
                    namespace="default",
                )
                json_text = await get_status_impl(
                    None,
                    cluster_name="dry-run-db",
                    database_name="localized-db",
                    namespace="default",
                    format="json",
                )

            expected = [
                "Current Locale/Encoding Values",
                "- Encoding: UTF8",
                "- Locale Provider: icu",
                "- LC_COLLATE: en_US.UTF-8",
                "- LC_CTYPE: en_US.UTF-8",
                "- ICU Locale: en-US",
                "- Collation Version: 153.120",
                "- Applied: True",
                "- Observed Generation: 3",
                "- Message: Database reconciled",
            ]
            missing = [needle for needle in expected if needle not in text]
            if missing:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Database status text missing expected content",
                    error=", ".join(missing),
                    duration_ms=(time.time() - start_time) * 1000,
                )

            structured = json.loads(json_text)
            if structured["create_options"]["localeProvider"] != "icu" or structured["status"]["applied"] is not True:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Database status JSON missing expected values",
                    error=json_text[:500],
                    duration_ms=(time.time() - start_time) * 1000,
                )

            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message="get_postgres_database_status reports Database spec values and operator status",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="get_postgres_database_status formatting test raised",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )
