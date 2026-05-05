"""
Test plugin for reading resources.

This template is a placeholder - customize it for your server's resources.
"""
from plugins import TestPlugin, TestResult
import time


class TestReadResource(TestPlugin):
    """Tests reading a resource."""

    tool_name = "read_resource"
    description = "Verifies reading a resource (customize for your resources)"
    depends_on = ["TestListResources"]
    run_after = []

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            # TODO: Customize this for your server's resources
            # Example:
            # result = await session.read_resource(uri="config://app/settings")
            #
            # # Extract and validate content
            # if hasattr(result, 'contents') and result.contents:
            #     text_content = result.contents[0].text if result.contents else ""
            # else:
            #     text_content = str(result)
            #
            # # Validate content contains expected data
            # if "expected_key" not in text_content:
            #     return TestResult(...)

            # For now, skip this test
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message="Skipped - customize this test for your resources",
                duration_ms=(time.time() - start_time) * 1000
            )

        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="Failed to read resource",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000
            )
