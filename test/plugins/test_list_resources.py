"""
Test plugin for listing resources.

This template is a placeholder - customize it for your server's resources.
"""
from plugins import TestPlugin, TestResult
import time


class TestListResources(TestPlugin):
    """Tests the resources/list endpoint."""

    tool_name = "list_resources"
    description = "Verifies server exposes expected resources"
    depends_on = []
    run_after = []

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            result = await session.list_resources()

            # Get list of resource URIs (convert to strings for comparison)
            resource_uris = [str(r.uri) for r in result.resources] if hasattr(result, 'resources') else []

            # TODO: Customize this list for your server's resources
            # Example expected resources:
            # expected_resources = [
            #     "config://app/settings",
            #     "data://users/list",
            # ]

            # For now, just verify the endpoint works
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message=f"Resources list returned successfully ({len(resource_uris)} resources)",
                duration_ms=(time.time() - start_time) * 1000
            )

        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="Failed to list resources",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000
            )
