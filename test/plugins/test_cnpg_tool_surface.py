"""Test plugin for the CloudNativePG tool registration surface."""

import time

from plugins import TestPlugin, TestResult


EXPECTED_CNPG_TOOLS = {
    "list_postgres_clusters",
    "get_cluster_status",
    "create_postgres_cluster",
    "scale_postgres_cluster",
    "delete_postgres_cluster",
    "list_postgres_roles",
    "create_postgres_role",
    "update_postgres_role",
    "delete_postgres_role",
    "list_postgres_databases",
    "get_postgres_database_status",
    "create_postgres_database",
    "delete_postgres_database",
}


class TestCnpgToolSurface(TestPlugin):
    """Verifies that the legacy CloudNativePG MCP tool names are registered."""

    tool_name = "tools/list"
    description = "Verifies expected CloudNativePG tool names are exposed"
    depends_on = []
    run_after = []

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            result = await session.list_tools()
            tools = {tool.name for tool in result.tools}
            missing = sorted(EXPECTED_CNPG_TOOLS - tools)

            if missing:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Missing expected CloudNativePG tools",
                    error=", ".join(missing),
                    duration_ms=(time.time() - start_time) * 1000,
                )

            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message=f"All {len(EXPECTED_CNPG_TOOLS)} CloudNativePG tools are registered",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="Failed to list tools",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )
