"""Unit coverage for deleting role partial-state leftovers."""

import sys
import time
from pathlib import Path
from unittest.mock import patch

from plugins import TestPlugin, TestResult


class TestDeletePostgresRoleOrphanSecret(TestPlugin):
    """Verifies delete_postgres_role cleans up an orphaned role Secret."""

    tool_name = "delete_postgres_role"
    description = "Verifies role delete cleans up Secret when managed role entry is absent"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestPatchClusterSpecClientCompatibility"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        cluster = {
            "metadata": {"name": "matrix-postgres", "namespace": "dusk"},
            "spec": {"managed": {"roles": []}},
        }
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
                patch("cnpg_mcp_tools.get_cnpg_cluster", return_value=cluster),
                patch("cnpg_mcp_tools.delete_role_secret", side_effect=fake_delete_role_secret),
                patch("cnpg_mcp_tools.patch_cnpg_cluster_spec") as patch_cluster,
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

            if patch_cluster.called:
                return TestResult(
                    self.get_name(),
                    self.tool_name,
                    False,
                    "delete_postgres_role patched the cluster even though no role entry existed",
                    str(patch_cluster.call_args),
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
