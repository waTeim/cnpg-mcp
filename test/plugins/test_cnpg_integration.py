"""Opt-in integration tests adapted from deprecated-v1/test/plugins.

These tests exercise the real CloudNativePG tools against Kubernetes. They
create and delete resources, so they are skipped unless explicitly enabled:

    python test/run-local-tests.py --include-integration
"""

import asyncio
import os
import time
from typing import Any, Dict, Optional

from plugins import TestContext, TestPlugin, TestResult, check_for_operational_error


TRUTHY = {"1", "true", "yes", "on"}
TEST_NAMESPACE = os.getenv("CNPG_MCP_TEST_NAMESPACE") or None
CLUSTER_PREFIX = os.getenv("CNPG_MCP_TEST_CLUSTER_PREFIX", "test-cluster")
STORAGE_SIZE = os.getenv("CNPG_MCP_TEST_STORAGE_SIZE", "1Gi")
POSTGRES_VERSION = os.getenv("CNPG_MCP_TEST_POSTGRES_VERSION", "16")
CREATE_WAIT_SECONDS = int(os.getenv("CNPG_MCP_TEST_CREATE_WAIT_SECONDS", "300"))
SCALE_WAIT_SECONDS = int(os.getenv("CNPG_MCP_TEST_SCALE_WAIT_SECONDS", "300"))
POLL_INTERVAL_SECONDS = int(os.getenv("CNPG_MCP_TEST_POLL_INTERVAL_SECONDS", "5"))
SCALE_INSTANCES = int(os.getenv("CNPG_MCP_TEST_SCALE_INSTANCES", "2"))


def _state(ctx: Optional[TestContext]) -> Dict[str, Any]:
    if ctx is not None:
        return ctx.shared
    from plugins import shared_test_state

    return shared_test_state


def _include_integration(ctx: Optional[TestContext]) -> bool:
    return bool(ctx and ctx.include_integration)


def _skip(plugin: TestPlugin, start_time: float, message: str) -> TestResult:
    return TestResult(
        plugin_name=plugin.get_name(),
        tool_name=plugin.tool_name,
        passed=True,
        message=f"Skipped: {message}",
        duration_ms=(time.time() - start_time) * 1000,
    )


def _integration_skip(plugin: TestPlugin, start_time: float, ctx: Optional[TestContext]) -> Optional[TestResult]:
    if _include_integration(ctx):
        return None
    return _skip(plugin, start_time, "pass --include-integration to run Kubernetes integration tests")


def _text(result) -> str:
    if not getattr(result, "content", None):
        return ""
    return "".join(getattr(part, "text", "") for part in result.content)


def _with_namespace(arguments: Dict[str, Any]) -> Dict[str, Any]:
    if TEST_NAMESPACE:
        return {**arguments, "namespace": TEST_NAMESPACE}
    return arguments


def _operational_failure(plugin: TestPlugin, start_time: float, message: str, response_text: str) -> Optional[TestResult]:
    is_error, error_msg = check_for_operational_error(response_text)
    if not is_error:
        return None
    return TestResult(
        plugin_name=plugin.get_name(),
        tool_name=plugin.tool_name,
        passed=False,
        message=message,
        error=error_msg or response_text[:500],
        duration_ms=(time.time() - start_time) * 1000,
    )


async def _call_text(session, tool_name: str, arguments: Dict[str, Any]) -> str:
    result = await session.call_tool(tool_name, arguments=arguments)
    return _text(result)


async def _cleanup_cluster(session, cluster_name: str) -> None:
    try:
        await session.call_tool(
            "delete_postgres_cluster",
            arguments=_with_namespace({"name": cluster_name, "confirm_deletion": True}),
        )
    except Exception:
        pass


class ServerInfoTest(TestPlugin):
    """Test server initialization and CloudNativePG tool availability."""

    tool_name = "server_init"
    description = "Test server initialization and capabilities"
    depends_on = ["TestCnpgToolSurface"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        expected_tools = {
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
            "create_postgres_database",
            "delete_postgres_database",
        }

        try:
            tools_result = await session.list_tools()
            actual_tools = {tool.name for tool in tools_result.tools}
            missing_tools = expected_tools - actual_tools
            if missing_tools:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message=f"Missing tools: {sorted(missing_tools)}",
                    duration_ms=(time.time() - start_time) * 1000,
                )
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message=f"Found all {len(expected_tools)} CloudNativePG tools",
                duration_ms=(time.time() - start_time) * 1000,
            )
        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="Test failed with exception",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )


class ListClustersTest(TestPlugin):
    """Test the list_postgres_clusters tool."""

    tool_name = "list_postgres_clusters"
    description = "Test listing PostgreSQL clusters"
    depends_on = ["ServerInfoTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        if skipped := _integration_skip(self, start_time, ctx):
            return skipped

        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({}))
            if len(response_text) < 10:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message=f"Response too short ({len(response_text)} chars)",
                    duration_ms=(time.time() - start_time) * 1000,
                )
            if failure := _operational_failure(self, start_time, "Tool executed but operation failed", response_text):
                return failure
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message=f"Successfully listed clusters ({len(response_text)} chars)",
                duration_ms=(time.time() - start_time) * 1000,
            )
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class CreatePostgresClusterTest(TestPlugin):
    """Create a shared PostgreSQL cluster for the integration workflow."""

    tool_name = "create_postgres_cluster"
    description = "Test creating a PostgreSQL cluster"
    depends_on = ["ServerInfoTest"]
    run_after = ["ListClustersTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        if skipped := _integration_skip(self, start_time, ctx):
            return skipped

        state = _state(ctx)
        cluster_name = f"{CLUSTER_PREFIX}-{int(time.time())}"
        state["test_cluster_name"] = cluster_name

        try:
            response_text = await _call_text(
                session,
                self.tool_name,
                _with_namespace({
                    "name": cluster_name,
                    "instances": 1,
                    "storage_size": STORAGE_SIZE,
                    "postgres_version": POSTGRES_VERSION,
                }),
            )
            if failure := _operational_failure(self, start_time, "Cluster creation failed", response_text):
                return failure
            if "cluster" not in response_text.lower():
                return TestResult(self.get_name(), self.tool_name, False, "Response missing cluster creation confirmation", response_text[:500], (time.time() - start_time) * 1000)

            deadline = time.time() + CREATE_WAIT_SECONDS
            last_status_text = ""
            while time.time() < deadline:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                last_status_text = await _call_text(
                    session,
                    "get_cluster_status",
                    _with_namespace({"name": cluster_name}),
                )
                status_lower = last_status_text.lower()
                if "1/1 ready" in status_lower or "healthy" in status_lower:
                    return TestResult(
                        plugin_name=self.get_name(),
                        tool_name=self.tool_name,
                        passed=True,
                        message=f"Successfully created cluster '{cluster_name}'",
                        duration_ms=(time.time() - start_time) * 1000,
                    )

            await _cleanup_cluster(session, cluster_name)
            state["test_cluster_name"] = None
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message=f"Cluster '{cluster_name}' was not ready after {CREATE_WAIT_SECONDS} seconds",
                error=last_status_text[:500],
                duration_ms=(time.time() - start_time) * 1000,
            )
        except Exception as e:
            await _cleanup_cluster(session, cluster_name)
            state["test_cluster_name"] = None
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class VerifyClusterCreatedTest(TestPlugin):
    """Verify that the created cluster appears in list output."""

    tool_name = "list_postgres_clusters"
    description = "Verify created cluster appears in clusters list"
    depends_on = ["CreatePostgresClusterTest"]
    run_after = ["CreatePostgresClusterTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        cluster_name = _state(ctx).get("test_cluster_name")
        if not _include_integration(ctx) or not cluster_name:
            return _skip(self, start_time, "no integration cluster available")
        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({}))
            if failure := _operational_failure(self, start_time, "Tool executed but operation failed", response_text):
                return failure
            if cluster_name not in response_text:
                return TestResult(self.get_name(), self.tool_name, False, f"Created cluster '{cluster_name}' not found", response_text[:500], (time.time() - start_time) * 1000)
            return TestResult(self.get_name(), self.tool_name, True, f"Verified cluster '{cluster_name}' appears in clusters list", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class GetClusterStatusTest(TestPlugin):
    """Test the get_cluster_status tool."""

    tool_name = "get_cluster_status"
    description = "Test getting cluster status details"
    depends_on = ["CreatePostgresClusterTest"]
    run_after = ["VerifyClusterCreatedTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        cluster_name = _state(ctx).get("test_cluster_name")
        if not _include_integration(ctx) or not cluster_name:
            return _skip(self, start_time, "no integration cluster available")
        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({"name": cluster_name}))
            if failure := _operational_failure(self, start_time, "Tool executed but operation failed", response_text):
                return failure
            if not any(keyword in response_text.lower() for keyword in ["status", "instances", "ready"]):
                return TestResult(self.get_name(), self.tool_name, False, "Response missing expected status keywords", response_text[:500], (time.time() - start_time) * 1000)
            return TestResult(self.get_name(), self.tool_name, True, f"Successfully got status for cluster '{cluster_name}'", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class ScalePostgresClusterTest(TestPlugin):
    """Test scaling the shared PostgreSQL cluster."""

    tool_name = "scale_postgres_cluster"
    description = "Test scaling a PostgreSQL cluster"
    depends_on = ["CreatePostgresClusterTest"]
    run_after = ["VerifyClusterCreatedTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        cluster_name = _state(ctx).get("test_cluster_name")
        if not _include_integration(ctx) or not cluster_name:
            return _skip(self, start_time, "no integration cluster available")
        try:
            response_text = await _call_text(
                session,
                self.tool_name,
                _with_namespace({"name": cluster_name, "instances": SCALE_INSTANCES}),
            )
            if failure := _operational_failure(self, start_time, "Cluster scaling failed", response_text):
                return failure
            if "scal" not in response_text.lower():
                return TestResult(self.get_name(), self.tool_name, False, "Response missing scaling confirmation", response_text[:500], (time.time() - start_time) * 1000)

            deadline = time.time() + SCALE_WAIT_SECONDS
            last_status_text = ""
            expected_ready = f"{SCALE_INSTANCES}/{SCALE_INSTANCES} ready"
            while time.time() < deadline:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                last_status_text = await _call_text(session, "get_cluster_status", _with_namespace({"name": cluster_name}))
                if expected_ready in last_status_text.lower():
                    return TestResult(self.get_name(), self.tool_name, True, f"Successfully scaled cluster '{cluster_name}' to {SCALE_INSTANCES} instances", duration_ms=(time.time() - start_time) * 1000)

            return TestResult(self.get_name(), self.tool_name, False, f"Scaling was not complete after {SCALE_WAIT_SECONDS} seconds", last_status_text[:500], (time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class ListRolesTest(TestPlugin):
    """Test listing PostgreSQL roles in the shared cluster."""

    tool_name = "list_postgres_roles"
    description = "Test listing PostgreSQL roles/users"
    depends_on = ["CreatePostgresClusterTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        cluster_name = _state(ctx).get("test_cluster_name")
        if not _include_integration(ctx) or not cluster_name:
            return _skip(self, start_time, "no integration cluster available")
        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({"cluster_name": cluster_name}))
            if failure := _operational_failure(self, start_time, "Tool executed but operation failed", response_text):
                return failure
            return TestResult(self.get_name(), self.tool_name, True, f"Successfully listed roles for '{cluster_name}'", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class CreatePostgresRoleTest(TestPlugin):
    """Test creating a PostgreSQL role in the shared cluster."""

    tool_name = "create_postgres_role"
    description = "Test creating a PostgreSQL role"
    depends_on = ["CreatePostgresClusterTest"]
    run_after = ["ListRolesTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        state = _state(ctx)
        cluster_name = state.get("test_cluster_name")
        if not _include_integration(ctx) or not cluster_name:
            return _skip(self, start_time, "no integration cluster available")
        role_name = f"test-role-{int(time.time())}"
        try:
            response_text = await _call_text(
                session,
                self.tool_name,
                _with_namespace({
                    "cluster_name": cluster_name,
                    "role_name": role_name,
                    "login": True,
                    "superuser": False,
                    "createdb": True,
                    "createrole": False,
                }),
            )
            if failure := _operational_failure(self, start_time, "Role creation failed", response_text):
                return failure
            if "role" not in response_text.lower():
                return TestResult(self.get_name(), self.tool_name, False, "Response missing role creation confirmation", response_text[:500], (time.time() - start_time) * 1000)
            state["test_role_name"] = role_name
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            return TestResult(self.get_name(), self.tool_name, True, f"Successfully created role '{role_name}'", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class VerifyRoleCreatedTest(TestPlugin):
    """Verify created role appears in list output."""

    tool_name = "list_postgres_roles"
    description = "Verify created role appears in roles list"
    depends_on = ["CreatePostgresRoleTest"]
    run_after = ["CreatePostgresRoleTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        state = _state(ctx)
        cluster_name = state.get("test_cluster_name")
        role_name = state.get("test_role_name")
        if not _include_integration(ctx) or not cluster_name or not role_name:
            return _skip(self, start_time, "no integration role available")
        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({"cluster_name": cluster_name}))
            if failure := _operational_failure(self, start_time, "Tool executed but operation failed", response_text):
                return failure
            if role_name not in response_text:
                return TestResult(self.get_name(), self.tool_name, False, f"Created role '{role_name}' not found", response_text[:500], (time.time() - start_time) * 1000)
            return TestResult(self.get_name(), self.tool_name, True, f"Verified role '{role_name}' appears in roles list", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class UpdatePostgresRoleTest(TestPlugin):
    """Test updating the created PostgreSQL role."""

    tool_name = "update_postgres_role"
    description = "Test updating a PostgreSQL role"
    depends_on = ["CreatePostgresRoleTest"]
    run_after = ["VerifyRoleCreatedTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        state = _state(ctx)
        cluster_name = state.get("test_cluster_name")
        role_name = state.get("test_role_name")
        if not _include_integration(ctx) or not cluster_name or not role_name:
            return _skip(self, start_time, "no integration role available")
        try:
            response_text = await _call_text(
                session,
                self.tool_name,
                _with_namespace({"cluster_name": cluster_name, "role_name": role_name, "createdb": False}),
            )
            if failure := _operational_failure(self, start_time, "Role update failed", response_text):
                return failure
            if "updated" not in response_text.lower() and "role" not in response_text.lower():
                return TestResult(self.get_name(), self.tool_name, False, "Response missing update confirmation", response_text[:500], (time.time() - start_time) * 1000)
            return TestResult(self.get_name(), self.tool_name, True, f"Successfully updated role '{role_name}'", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class ListDatabasesTest(TestPlugin):
    """Test listing CloudNativePG Database CRDs for the shared cluster."""

    tool_name = "list_postgres_databases"
    description = "Test listing PostgreSQL databases"
    depends_on = ["CreatePostgresClusterTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        cluster_name = _state(ctx).get("test_cluster_name")
        if not _include_integration(ctx) or not cluster_name:
            return _skip(self, start_time, "no integration cluster available")
        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({"cluster_name": cluster_name}))
            if failure := _operational_failure(self, start_time, "Tool executed but operation failed", response_text):
                return failure
            return TestResult(self.get_name(), self.tool_name, True, f"Successfully listed databases for '{cluster_name}'", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class CreatePostgresDatabaseTest(TestPlugin):
    """Test creating a CloudNativePG Database CRD."""

    tool_name = "create_postgres_database"
    description = "Test creating a PostgreSQL database"
    depends_on = ["CreatePostgresRoleTest"]
    run_after = ["ListDatabasesTest", "VerifyRoleCreatedTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        state = _state(ctx)
        cluster_name = state.get("test_cluster_name")
        role_name = state.get("test_role_name")
        if not _include_integration(ctx) or not cluster_name or not role_name:
            return _skip(self, start_time, "no integration cluster/role available")
        database_name = f"testdb{int(time.time())}"
        try:
            response_text = await _call_text(
                session,
                self.tool_name,
                _with_namespace({
                    "cluster_name": cluster_name,
                    "database_name": database_name,
                    "owner": role_name,
                    "reclaim_policy": "delete",
                }),
            )
            if failure := _operational_failure(self, start_time, "Database creation failed", response_text):
                return failure
            if "database" not in response_text.lower():
                return TestResult(self.get_name(), self.tool_name, False, "Response missing database creation confirmation", response_text[:500], (time.time() - start_time) * 1000)
            state["test_database_name"] = database_name
            return TestResult(self.get_name(), self.tool_name, True, f"Successfully created database '{database_name}'", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class VerifyDatabaseCreatedTest(TestPlugin):
    """Verify created database appears in list output."""

    tool_name = "list_postgres_databases"
    description = "Verify created database appears in database list"
    depends_on = ["CreatePostgresDatabaseTest"]
    run_after = ["CreatePostgresDatabaseTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        state = _state(ctx)
        cluster_name = state.get("test_cluster_name")
        database_name = state.get("test_database_name")
        if not _include_integration(ctx) or not cluster_name or not database_name:
            return _skip(self, start_time, "no integration database available")
        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({"cluster_name": cluster_name}))
            if failure := _operational_failure(self, start_time, "Tool executed but operation failed", response_text):
                return failure
            if database_name not in response_text:
                return TestResult(self.get_name(), self.tool_name, False, f"Created database '{database_name}' not found", response_text[:500], (time.time() - start_time) * 1000)
            return TestResult(self.get_name(), self.tool_name, True, f"Verified database '{database_name}' appears in database list", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class DeletePostgresDatabaseTest(TestPlugin):
    """Delete the test Database CRD."""

    tool_name = "delete_postgres_database"
    description = "Test deleting the created PostgreSQL database"
    depends_on = ["CreatePostgresDatabaseTest"]
    run_after = ["VerifyDatabaseCreatedTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        state = _state(ctx)
        cluster_name = state.get("test_cluster_name")
        database_name = state.get("test_database_name")
        if not _include_integration(ctx) or not cluster_name or not database_name:
            return _skip(self, start_time, "no integration database available")
        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({"cluster_name": cluster_name, "database_name": database_name}))
            if failure := _operational_failure(self, start_time, "Database deletion failed", response_text):
                return failure
            state["test_database_name"] = None
            return TestResult(self.get_name(), self.tool_name, True, f"Successfully deleted database '{database_name}'", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class DeletePostgresRoleTest(TestPlugin):
    """Delete the test PostgreSQL role."""

    tool_name = "delete_postgres_role"
    description = "Test deleting the created PostgreSQL role"
    depends_on = ["CreatePostgresRoleTest"]
    run_after = ["UpdatePostgresRoleTest", "DeletePostgresDatabaseTest"]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        state = _state(ctx)
        cluster_name = state.get("test_cluster_name")
        role_name = state.get("test_role_name")
        if not _include_integration(ctx) or not cluster_name or not role_name:
            return _skip(self, start_time, "no integration role available")
        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({"cluster_name": cluster_name, "role_name": role_name}))
            if failure := _operational_failure(self, start_time, "Role deletion failed", response_text):
                return failure
            state["test_role_name"] = None
            return TestResult(self.get_name(), self.tool_name, True, f"Successfully deleted role '{role_name}'", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Test failed with exception", str(e), (time.time() - start_time) * 1000)


class DeletePostgresClusterTest(TestPlugin):
    """Delete the shared test cluster."""

    tool_name = "delete_postgres_cluster"
    description = "Test deleting the shared test cluster"
    depends_on = ["CreatePostgresClusterTest"]
    run_after = [
        "VerifyClusterCreatedTest",
        "ScalePostgresClusterTest",
        "GetClusterStatusTest",
        "ListRolesTest",
        "CreatePostgresRoleTest",
        "VerifyRoleCreatedTest",
        "UpdatePostgresRoleTest",
        "DeletePostgresRoleTest",
        "ListDatabasesTest",
        "CreatePostgresDatabaseTest",
        "VerifyDatabaseCreatedTest",
        "DeletePostgresDatabaseTest",
    ]

    async def test(self, session, ctx: Optional[TestContext] = None) -> TestResult:
        start_time = time.time()
        state = _state(ctx)
        cluster_name = state.get("test_cluster_name")
        if not _include_integration(ctx) or not cluster_name:
            return _skip(self, start_time, "no integration cluster available")
        try:
            response_text = await _call_text(session, self.tool_name, _with_namespace({"name": cluster_name, "confirm_deletion": True}))
            if failure := _operational_failure(self, start_time, "Cluster deletion failed", response_text):
                return failure
            state["test_cluster_name"] = None
            return TestResult(self.get_name(), self.tool_name, True, f"Successfully deleted cluster '{cluster_name}'", duration_ms=(time.time() - start_time) * 1000)
        except Exception as e:
            return TestResult(self.get_name(), self.tool_name, False, "Cluster deletion test failed with exception", str(e), (time.time() - start_time) * 1000)
