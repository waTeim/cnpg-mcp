"""Unit coverage for DatabaseRole status/value formatting helpers."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

from plugins import TestPlugin, TestResult


ROLE_CRD = {
    "metadata": {
        "name": "dry-run-db-app-user",
        "namespace": "default",
        "generation": 2,
        "resourceVersion": "54321",
    },
    "spec": {
        "name": "app-user",
        "cluster": {"name": "dry-run-db"},
        "ensure": "present",
        "databaseRoleReclaimPolicy": "delete",
        "login": True,
        "superuser": False,
        "inherit": True,
        "createdb": True,
        "createrole": False,
        "replication": False,
        "bypassrls": False,
        "connectionLimit": 10,
        "validUntil": "2026-12-31T23:59:59Z",
        "comment": "application role",
        "inRoles": ["pg_read_all_data"],
        "passwordSecret": {"name": "cnpg-dry-run-db-user-app-user"},
    },
    "status": {
        "observedGeneration": 2,
        "applied": True,
        "message": "Role reconciled",
    },
}


class TestGetPostgresRoleStatusFormatting(TestPlugin):
    """Verifies get_postgres_role_status reports spec values and status."""

    tool_name = "get_postgres_role_status"
    description = "Verifies DatabaseRole status/value formatting without Kubernetes"
    depends_on = ["TestCnpgToolSurface"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            src_dir = Path(__file__).resolve().parents[2] / "src"
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            from cnpg_mcp_tools import get_postgres_role_status

            with patch("cnpg_mcp_tools.get_cnpg_database_role", return_value=ROLE_CRD):
                get_status_impl = get_postgres_role_status.__wrapped__
                text = await get_status_impl(
                    None,
                    cluster_name="dry-run-db",
                    role_name="app-user",
                    namespace="default",
                )
                json_text = await get_status_impl(
                    None,
                    cluster_name="dry-run-db",
                    role_name="app-user",
                    namespace="default",
                    format="json",
                )

            expected = [
                "**Role: default/dry-run-db-app-user**",
                "- Reclaim Policy: delete",
                "- Login: True",
                "- Create DB: True",
                "- Connection Limit: 10",
                "- Valid Until: 2026-12-31T23:59:59Z",
                "- Comment: application role",
                "- Member of: pg_read_all_data",
                "- Password Secret: cnpg-dry-run-db-user-app-user",
                "- Applied: True",
                "- Observed Generation: 2",
                "- Message: Role reconciled",
            ]
            missing = [needle for needle in expected if needle not in text]
            if missing:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Role status text missing expected content",
                    error=", ".join(missing),
                    duration_ms=(time.time() - start_time) * 1000,
                )

            structured = json.loads(json_text)
            attributes = structured["attributes"]
            if (
                structured["reclaim_policy"] != "delete"
                or attributes["connection_limit"] != 10
                or attributes["in_roles"] != ["pg_read_all_data"]
                or structured["status"]["applied"] is not True
            ):
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Role status JSON missing expected values",
                    error=json_text[:500],
                    duration_ms=(time.time() - start_time) * 1000,
                )

            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message="get_postgres_role_status reports DatabaseRole spec values and operator status",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="get_postgres_role_status formatting test raised",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )


class TestCreatePostgresRoleDryRun(TestPlugin):
    """Verifies create_postgres_role previews a DatabaseRole CRD."""

    tool_name = "create_postgres_role"
    description = "Verifies role creation dry run renders the DatabaseRole CRD"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestGetPostgresRoleStatusFormatting"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        cluster = {"metadata": {"name": "dry-run-db"}, "spec": {}}

        try:
            src_dir = Path(__file__).resolve().parents[2] / "src"
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            from cnpg_mcp_tools import create_postgres_role

            create_role_impl = create_postgres_role.__wrapped__
            with (
                patch("cnpg_mcp_tools.get_cnpg_cluster", return_value=cluster),
                patch("cnpg_mcp_tools.find_cnpg_database_role", return_value=None),
                patch("cnpg_mcp_tools.get_kubernetes_clients") as get_clients,
            ):
                text = await create_role_impl(
                    None,
                    cluster_name="dry-run-db",
                    role_name="app-user",
                    createdb=True,
                    in_roles=["pg_read_all_data"],
                    connection_limit=10,
                    reclaim_policy="delete",
                    namespace="default",
                    dry_run=True,
                )

            expected = [
                "kind: DatabaseRole",
                "name: dry-run-db-app-user",
                "databaseRoleReclaimPolicy: delete",
                "- pg_read_all_data",
                "connectionLimit: 10",
                "cnpg-dry-run-db-user-app-user",
            ]
            missing = [needle for needle in expected if needle not in text]
            if missing:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Role dry run missing expected DatabaseRole content",
                    error=", ".join(missing),
                    duration_ms=(time.time() - start_time) * 1000,
                )

            if get_clients.called:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Role dry run created Kubernetes resources",
                    error=str(get_clients.call_args),
                    duration_ms=(time.time() - start_time) * 1000,
                )

            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message="create_postgres_role dry run renders the DatabaseRole CRD without side effects",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="create_postgres_role dry run test raised",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )
