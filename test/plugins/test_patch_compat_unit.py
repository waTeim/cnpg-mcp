"""Unit coverage for Kubernetes patch helper compatibility."""

import sys
import time
from pathlib import Path
from unittest.mock import patch

from plugins import TestPlugin, TestResult


class _StrictPatchApi:
    """Fake CustomObjectsApi that rejects unexpected patch kwargs."""

    def __init__(self):
        self.call = None

    def patch_namespaced_custom_object(self, group, version, namespace, plural, name, body):
        self.call = {
            "group": group,
            "version": version,
            "namespace": namespace,
            "plural": plural,
            "name": name,
            "body": body,
        }
        return {"patched": True}


class TestPatchClusterSpecClientCompatibility(TestPlugin):
    """Ensures the cluster patch helper works with strict Kubernetes clients."""

    tool_name = "patch_cnpg_cluster_spec"
    description = "Verifies patch helper does not pass unsupported Kubernetes client kwargs"
    depends_on = []
    run_after = []

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            src_dir = Path(__file__).resolve().parents[2] / "src"
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            from cnpg_mcp_tools import patch_cnpg_cluster_spec

            fake_api = _StrictPatchApi()
            with patch("cnpg_mcp_tools.get_kubernetes_clients", return_value=(fake_api, None)):
                result = await patch_cnpg_cluster_spec("dusk", "matrix-postgres", {"managed": {"roles": []}})

            if result != {"patched": True}:
                return TestResult(
                    self.get_name(),
                    self.tool_name,
                    False,
                    "Patch helper returned unexpected result",
                    str(result),
                    (time.time() - start_time) * 1000,
                )

            expected_body = {"spec": {"managed": {"roles": []}}}
            if fake_api.call is None or fake_api.call["body"] != expected_body:
                return TestResult(
                    self.get_name(),
                    self.tool_name,
                    False,
                    "Patch helper did not send expected merge-patch body",
                    str(fake_api.call),
                    (time.time() - start_time) * 1000,
                )

            return TestResult(
                self.get_name(),
                self.tool_name,
                True,
                "patch_cnpg_cluster_spec works with clients that reject unsupported kwargs",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(),
                self.tool_name,
                False,
                "Patch helper compatibility test raised",
                str(e),
                (time.time() - start_time) * 1000,
            )
