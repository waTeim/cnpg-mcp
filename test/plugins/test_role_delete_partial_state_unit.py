"""Unit coverage for deleting role partial-state leftovers."""

import sys
import time
from pathlib import Path
from unittest.mock import patch

from plugins import TestPlugin, TestResult


class TestDeletePostgresRoleOrphanSecret(TestPlugin):
    """Verifies delete_postgres_role cleans up an orphaned role Secret."""

    tool_name = "delete_postgres_role"
    description = "Verifies role delete cleans up Secret when the DatabaseRole CRD is absent"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestPatchClusterSpecClientCompatibility"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        deleted = []

        async def fake_delete_role_secret(namespace: str, secret_name: str) -> bool:
            deleted.append((namespace, secret_name))
            return True

        try:
            src_dir = Path(__file__).resolve().parents[2] / "src"
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            from cnpg_mcp_tools import delete_postgres_role

            delete_role_impl = delete_postgres_role.__wrapped__
            with (
                patch("cnpg_mcp_tools.find_cnpg_database_role", return_value=None),
                patch("cnpg_mcp_tools.delete_role_secret", side_effect=fake_delete_role_secret),
                patch("cnpg_mcp_tools.get_kubernetes_clients") as get_clients,
            ):
                text = await delete_role_impl(
                    None,
                    cluster_name="matrix-postgres",
                    role_name="synapse",
                    namespace="dusk",
                )

            expected_secret = ("dusk", "cnpg-matrix-postgres-user-synapse")
            if deleted != [expected_secret]:
                return TestResult(
                    self.get_name(),
                    self.tool_name,
                    False,
                    "delete_postgres_role did not delete the orphaned Secret",
                    str(deleted),
                    (time.time() - start_time) * 1000,
                )

            if get_clients.called:
                return TestResult(
                    self.get_name(),
                    self.tool_name,
                    False,
                    "delete_postgres_role touched the Kubernetes API even though no DatabaseRole CRD existed",
                    str(get_clients.call_args),
                    (time.time() - start_time) * 1000,
                )

            if "Cleaned up orphaned PostgreSQL role secret" not in text or "cnpg-matrix-postgres-user-synapse" not in text:
                return TestResult(
                    self.get_name(),
                    self.tool_name,
                    False,
                    "delete_postgres_role response did not explain orphan cleanup",
                    text[:500],
                    (time.time() - start_time) * 1000,
                )

            return TestResult(
                self.get_name(),
                self.tool_name,
                True,
                "delete_postgres_role cleans up orphaned role Secret partial state",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(),
                self.tool_name,
                False,
                "Orphan role Secret deletion test raised",
                str(e),
                (time.time() - start_time) * 1000,
            )


class TestDeletePostgresRoleDropsRole(TestPlugin):
    """Verifies delete_postgres_role forces the reclaim policy when drop_role is set."""

    tool_name = "delete_postgres_role"
    description = "Verifies drop_role=True patches the DatabaseRole reclaim policy before deletion"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestDeletePostgresRoleOrphanSecret"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        role_crd = {
            "metadata": {"name": "matrix-postgres-synapse", "namespace": "dusk"},
            "spec": {
                "name": "synapse",
                "cluster": {"name": "matrix-postgres"},
                "databaseRoleReclaimPolicy": "retain",
                "passwordSecret": {"name": "cnpg-matrix-postgres-user-synapse"},
            },
        }
        patched = []

        async def fake_patch(namespace: str, crd_name: str, spec_patch: dict) -> dict:
            patched.append((namespace, crd_name, spec_patch))
            return role_crd

        async def fake_delete_role_secret(namespace: str, secret_name: str) -> bool:
            return True

        try:
            src_dir = Path(__file__).resolve().parents[2] / "src"
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            from cnpg_mcp_tools import delete_postgres_role

            delete_role_impl = delete_postgres_role.__wrapped__
            with (
                patch("cnpg_mcp_tools.find_cnpg_database_role", return_value=role_crd),
                patch("cnpg_mcp_tools.patch_cnpg_database_role_spec", side_effect=fake_patch),
                patch("cnpg_mcp_tools.delete_role_secret", side_effect=fake_delete_role_secret),
                patch("cnpg_mcp_tools.get_kubernetes_clients", return_value=(_RecordingCustomApi(), None)) as get_clients,
            ):
                text = await delete_role_impl(
                    None,
                    cluster_name="matrix-postgres",
                    role_name="synapse",
                    drop_role=True,
                    namespace="dusk",
                )
                custom_api = get_clients.return_value[0]

            expected_patch = [("dusk", "matrix-postgres-synapse", {"databaseRoleReclaimPolicy": "delete"})]
            if patched != expected_patch:
                return TestResult(
                    self.get_name(),
                    self.tool_name,
                    False,
                    "drop_role=True did not force the reclaim policy to 'delete'",
                    str(patched),
                    (time.time() - start_time) * 1000,
                )

            if custom_api.deleted != [("databaseroles", "matrix-postgres-synapse", "dusk")]:
                return TestResult(
                    self.get_name(),
                    self.tool_name,
                    False,
                    "delete_postgres_role did not delete the DatabaseRole CRD",
                    str(custom_api.deleted),
                    (time.time() - start_time) * 1000,
                )

            if "will be dropped from PostgreSQL" not in text:
                return TestResult(
                    self.get_name(),
                    self.tool_name,
                    False,
                    "delete_postgres_role response did not report the role being dropped",
                    text[:500],
                    (time.time() - start_time) * 1000,
                )

            return TestResult(
                self.get_name(),
                self.tool_name,
                True,
                "delete_postgres_role honors drop_role by forcing the reclaim policy",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(),
                self.tool_name,
                False,
                "drop_role deletion test raised",
                str(e),
                (time.time() - start_time) * 1000,
            )


class _RecordingCustomApi:
    """Minimal CustomObjectsApi stand-in that records delete calls."""

    def __init__(self):
        self.deleted = []

    def delete_namespaced_custom_object(self, group, version, namespace, plural, name):
        self.deleted.append((plural, name, namespace))
        return {}
