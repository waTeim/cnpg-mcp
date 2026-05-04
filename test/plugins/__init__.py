"""
MCP Test Plugin System

Plugins are Python modules that test individual MCP tools.
Each plugin should inherit from TestPlugin and implement the test() method.

Two plugin signatures are supported (the runner picks via inspect):

    async def test(self, session) -> TestResult: ...
    async def test(self, session, ctx: TestContext) -> TestResult: ...

The second form is preferred for new plugins — it gives access to the
runner's base_url (needed for non-MCP HTTP endpoints like /healthz) and a
shared dict for cross-plugin coordination (e.g. publishing a created
resource ID for downstream plugins to reuse instead of creating their own).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
import re


# DEPRECATED: prefer TestContext.shared. Kept for backward compatibility
# with plugins that import this module-level dict directly.
shared_test_state: Dict[str, Any] = {}


@dataclass
class TestResult:
    """Result of a test plugin execution."""
    plugin_name: str
    tool_name: str
    passed: bool
    message: str
    error: Optional[str] = None
    duration_ms: Optional[float] = None


@dataclass
class TestContext:
    """
    Per-run context passed to plugins that opt-in.

    Plugins receive this only if their `test()` signature declares a `ctx`
    parameter — older single-arg plugins keep working unchanged.

    Attributes:
        base_url: The MCP endpoint URL the runner connected to
            (e.g. "http://127.0.0.1:4201/test"). Strip the path suffix
            for non-MCP endpoints like /healthz / /readyz.
        include_integration: Whether mutating CloudNativePG Kubernetes tests
            should run. Defaults to False so local smoke tests stay safe.
        shared: Mutable dict for plugins to publish data for downstream
            plugins (e.g. a created resource ID, an obtained token, a
            scaffold project_id). Use `run_after` / `depends_on` to
            enforce ordering before reading from it.
    """
    base_url: str
    include_integration: bool = False
    shared: Dict[str, Any] = field(default_factory=dict)


def check_for_operational_error(response_text: str) -> Tuple[bool, Optional[str]]:
    """
    Check if a tool response contains an operational error.

    MCP tools may execute successfully (no exception) but return error messages
    indicating the underlying operation failed (e.g., RBAC permissions, network issues).

    Args:
        response_text: The text content returned by the MCP tool

    Returns:
        Tuple of (is_error, error_message)
        - is_error: True if response contains an error
        - error_message: Extracted error message if found, None otherwise
    """
    # Error patterns that indicate operational failures
    error_patterns = [
        r'Error (?:listing|getting|creating|updating|deleting|scaling)',
        r'Kubernetes API Error',
        r'\d{3} Forbidden',
        r'is forbidden:',
        r'cannot (?:list|get|create|update|delete|patch) resource',
        r'Permission denied',
        r'Unauthorized',
        r'Authentication failed',
        r'Connection refused',
        r'Connection timeout',
        r'No route to host',
    ]

    for pattern in error_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            # Extract error context (up to 500 chars from the match)
            start = max(0, match.start() - 50)
            end = min(len(response_text), match.end() + 450)
            error_context = response_text[start:end].strip()

            # Clean up the error message
            # Remove excessive whitespace and newlines
            error_context = re.sub(r'\s+', ' ', error_context)

            return True, error_context

    return False, None


class TestPlugin:
    """Base class for MCP test plugins."""

    # Override these in your plugin
    tool_name: str = "unknown"
    description: str = "No description"
    depends_on: list = []  # Hard dependencies - test skipped if these fail
    run_after: list = []   # Soft dependencies - test runs after these, but not skipped if they fail

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        """
        Run the test for this tool.

        Args:
            session: MCP ClientSession instance
            ctx: Optional per-run context. The runner inspects each plugin's
                signature and only passes ctx when declared, so legacy
                `(self, session)` plugins keep working.

        Returns:
            TestResult with pass/fail status and details
        """
        raise NotImplementedError("Plugin must implement test() method")

    def get_name(self) -> str:
        """Get the plugin name (defaults to class name)."""
        return self.__class__.__name__
