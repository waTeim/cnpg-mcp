"""
Test plugin for the test server's Kubernetes liveness/readiness endpoints.

These endpoints are plain HTTP (not MCP-protocol) and exist outside the
/test path. They're what the Helm chart's livenessProbe/readinessProbe
hit, so regressions here would silently break pod health checks.

Reads `ctx.base_url` to derive the host:port — see TestContext in
test/plugins/__init__.py.
"""
from plugins import TestPlugin, TestResult, TestContext
from typing import Optional
from urllib.parse import urlparse, urlunparse
import time

import httpx


def _strip_path(base_url: str, new_path: str) -> str:
    """Replace the path component of base_url with new_path."""
    p = urlparse(base_url)
    return urlunparse((p.scheme, p.netloc, new_path, "", "", ""))


class TestHealthEndpoints(TestPlugin):
    """Hits /healthz and /readyz, asserts 200 with the documented JSON shape."""

    tool_name = "http_health"
    description = "Verifies /healthz and /readyz return 200 with the K8s probe JSON"
    depends_on = []
    run_after = []

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()

        if ctx is None or not ctx.base_url:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="No base_url available in TestContext — runner did not pass ctx",
                duration_ms=(time.time() - start_time) * 1000,
            )

        healthz = _strip_path(ctx.base_url, "/healthz")
        readyz = _strip_path(ctx.base_url, "/readyz")

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                liveness = await client.get(healthz)
                readiness = await client.get(readyz)
        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message=f"HTTP request to health endpoints failed",
                error=f"{type(e).__name__}: {e}",
                duration_ms=(time.time() - start_time) * 1000,
            )

        checks = [
            (healthz, liveness, {"status": "alive"}),
            (readyz, readiness, {"status": "ready"}),
        ]
        for url, resp, expected in checks:
            if resp.status_code != 200:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message=f"GET {url} returned {resp.status_code}, expected 200",
                    error=resp.text[:200],
                    duration_ms=(time.time() - start_time) * 1000,
                )
            try:
                body = resp.json()
            except ValueError as e:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message=f"GET {url} returned non-JSON body",
                    error=str(e),
                    duration_ms=(time.time() - start_time) * 1000,
                )
            if body != expected:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message=f"GET {url} body {body!r} != expected {expected!r}",
                    duration_ms=(time.time() - start_time) * 1000,
                )

        return TestResult(
            plugin_name=self.get_name(),
            tool_name=self.tool_name,
            passed=True,
            message="Both /healthz and /readyz returned 200 with expected JSON",
            duration_ms=(time.time() - start_time) * 1000,
        )
