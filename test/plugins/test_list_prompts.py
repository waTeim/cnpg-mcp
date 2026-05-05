"""
Test plugin for listing prompts.
"""
from plugins import TestPlugin, TestResult
import time


class TestListPrompts(TestPlugin):
    """Tests the prompts/list endpoint."""

    tool_name = "list_prompts"
    description = "Verifies prompts/list works"
    depends_on = []
    run_after = []

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            result = await session.list_prompts()

            # Get list of prompts
            prompts = result.prompts if hasattr(result, 'prompts') else []

            # TODO: If your server defines prompts, validate them here
            # Example:
            # expected_prompts = ["help", "status"]
            # missing = [p for p in expected_prompts if p not in [pr.name for pr in prompts]]
            # if missing:
            #     return TestResult(passed=False, message=f"Missing prompts: {missing}", ...)

            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message=f"Prompts list returned successfully ({len(prompts)} prompts)",
                duration_ms=(time.time() - start_time) * 1000
            )

        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="Failed to list prompts",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000
            )
