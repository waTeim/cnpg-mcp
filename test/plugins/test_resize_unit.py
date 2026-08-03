"""Unit coverage for cluster storage resize helpers and guards."""

import sys
import time
from pathlib import Path
from unittest.mock import patch

from plugins import TestPlugin, TestResult


def _import_tools():
    src_dir = Path(__file__).resolve().parents[2] / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    import cnpg_mcp_tools

    return cnpg_mcp_tools


def _cluster(size="10Gi", instances=3, primary="dev-2", annotations=None, healthy=None):
    return {
        "metadata": {"name": "dev", "namespace": "claude", "annotations": annotations or {}},
        "spec": {"instances": instances, "storage": {"size": size}},
        "status": {
            "currentPrimary": primary,
            "targetPrimary": primary,
            "instanceNames": ["dev-1", "dev-2", "dev-3"],
            "instancesStatus": {"healthy": healthy if healthy is not None else ["dev-1", "dev-2", "dev-3"]},
        },
    }


def _pvc(instance, size, role="replica"):
    return {
        "pvc_name": instance,
        "instance": instance,
        "pvc_role": "PG_DATA",
        "instance_role": role,
        "requested_size": size,
        "actual_size": size,
        "phase": "Bound",
    }


class TestStorageQuantityParsing(TestPlugin):
    """Verifies Kubernetes storage quantities are compared correctly."""

    tool_name = "resize_postgres_cluster"
    description = "Verifies storage quantity parsing across binary and decimal suffixes"
    depends_on = ["TestCnpgToolSurface"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            tools = _import_tools()
            parse = tools.parse_storage_quantity

            cases = [
                ("1Gi", 2 ** 30),
                ("10Gi", 10 * 2 ** 30),
                ("500Mi", 500 * 2 ** 20),
                ("1G", 1e9),
                ("1024", 1024),
                ("1Ti", 2 ** 40),
            ]
            for value, expected in cases:
                if parse(value) != expected:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        f"parse_storage_quantity({value!r}) returned {parse(value)}, expected {expected}",
                        duration_ms=(time.time() - start_time) * 1000,
                    )

            # A decimal gigabyte is smaller than a binary one; the shrink/grow
            # decision depends on this comparison being exact.
            if not parse("1G") < parse("1Gi"):
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "1G should compare smaller than 1Gi",
                    duration_ms=(time.time() - start_time) * 1000,
                )

            for bad in ["10 gigs", "", "Gi", "abc"]:
                try:
                    parse(bad)
                except ValueError:
                    continue
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    f"parse_storage_quantity({bad!r}) should have raised ValueError",
                    duration_ms=(time.time() - start_time) * 1000,
                )

            return TestResult(
                self.get_name(), self.tool_name, True,
                "parse_storage_quantity handles binary, decimal, and invalid quantities",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(), self.tool_name, False,
                "Storage quantity parsing test raised", str(e),
                (time.time() - start_time) * 1000,
            )


class TestResizeShrinkRestoresValidation(TestPlugin):
    """Verifies a failed shrink patch still re-enables the validating webhook."""

    tool_name = "resize_postgres_cluster"
    description = "Verifies validation is restored even when the shrink patch fails"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestStorageQuantityParsing"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            tools = _import_tools()
            calls = []

            async def fake_set_validation(namespace, name, value):
                calls.append(("validation", value))

            async def failing_patch(namespace, name, spec_patch):
                calls.append(("spec", spec_patch))
                raise Exception("simulated API failure")

            resize_impl = tools.resize_postgres_cluster.__wrapped__
            with (
                patch("cnpg_mcp_tools.get_cnpg_cluster", return_value=_cluster()),
                patch("cnpg_mcp_tools.set_cluster_validation", side_effect=fake_set_validation),
                patch("cnpg_mcp_tools.patch_cnpg_cluster_spec", side_effect=failing_patch),
            ):
                text = await resize_impl(
                    None, name="dev", storage_size="5Gi",
                    confirm_shrink=True, namespace="claude",
                )

            expected = [
                ("validation", "disabled"),
                ("spec", {"storage": {"size": "5Gi"}, "instances": 4}),
                ("validation", None),
            ]
            if calls != expected:
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Shrink did not disable, patch, and restore validation in order",
                    str(calls), (time.time() - start_time) * 1000,
                )

            if "simulated API failure" not in text:
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Failed shrink did not surface the underlying error",
                    text[:500], (time.time() - start_time) * 1000,
                )

            return TestResult(
                self.get_name(), self.tool_name, True,
                "Shrink restores the validation annotation even when the spec patch fails",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(), self.tool_name, False,
                "Shrink validation-restore test raised", str(e),
                (time.time() - start_time) * 1000,
            )


class TestResizeGrowIsDirect(TestPlugin):
    """Verifies growing patches only the size and never touches validation."""

    tool_name = "resize_postgres_cluster"
    description = "Verifies growth is a plain spec patch with validation untouched"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestResizeShrinkRestoresValidation"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            tools = _import_tools()
            patches = []

            async def fake_patch(namespace, name, spec_patch):
                patches.append(spec_patch)
                return {}

            resize_impl = tools.resize_postgres_cluster.__wrapped__
            with (
                patch("cnpg_mcp_tools.get_cnpg_cluster", return_value=_cluster()),
                patch("cnpg_mcp_tools.patch_cnpg_cluster_spec", side_effect=fake_patch),
                patch("cnpg_mcp_tools.set_cluster_validation") as set_validation,
            ):
                text = await resize_impl(None, name="dev", storage_size="20Gi", namespace="claude")

            if patches != [{"storage": {"size": "20Gi"}}]:
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Growth patched something other than the storage size",
                    str(patches), (time.time() - start_time) * 1000,
                )

            if set_validation.called:
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Growth disabled validation, which is only needed for a shrink",
                    str(set_validation.call_args), (time.time() - start_time) * 1000,
                )

            if "instances" in text.lower().split("next steps")[0] and "10Gi -> 20Gi" not in text:
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Growth response did not report the size change",
                    text[:500], (time.time() - start_time) * 1000,
                )

            return TestResult(
                self.get_name(), self.tool_name, True,
                "Growth applies a plain storage size patch without disabling validation",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(), self.tool_name, False,
                "Growth path test raised", str(e),
                (time.time() - start_time) * 1000,
            )


class TestResizeNextStepGuidance(TestPlugin):
    """Verifies the resize state machine recommends the right next action."""

    tool_name = "get_cluster_resize_status"
    description = "Verifies next-step guidance across resize stages"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestResizeGrowIsDirect"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            tools = _import_tools()
            summarize = tools.summarize_resize_state
            describe = tools.describe_resize_next_step

            # Stage 1: old primary, new healthy instance -> promote it
            cluster = _cluster(size="5Gi", instances=4, primary="dev-2")
            cluster["status"]["instanceNames"].append("dev-4")
            cluster["status"]["instancesStatus"]["healthy"].append("dev-4")
            pvcs = [_pvc("dev-1", "10Gi"), _pvc("dev-2", "10Gi", "primary"), _pvc("dev-3", "10Gi"), _pvc("dev-4", "5Gi")]
            state = summarize(cluster, pvcs)
            step = describe(cluster, state)
            if state["stale_names"] != ["dev-1", "dev-2", "dev-3"] or "promote_cluster_instance" not in step or "dev-4" not in step:
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Stage 1 did not recommend promoting the resized instance",
                    step[:500], (time.time() - start_time) * 1000,
                )

            # Stage 2: new instance is primary -> recycle the stale replicas
            cluster["status"]["currentPrimary"] = "dev-4"
            cluster["status"]["targetPrimary"] = "dev-4"
            state = summarize(cluster, pvcs)
            step = describe(cluster, state)
            if "delete_cluster_instance" not in step or "dev-1" not in step or "dev-3" not in step:
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Stage 2 did not recommend recycling the stale instances",
                    step[:500], (time.time() - start_time) * 1000,
                )

            # Stage 3: everything resized -> complete
            state = summarize(cluster, [_pvc("dev-4", "5Gi", "primary"), _pvc("dev-1", "5Gi")])
            step = describe(cluster, state)
            if "complete" not in step.lower():
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Stage 3 did not report the resize as complete",
                    step[:500], (time.time() - start_time) * 1000,
                )

            # Stage 4: no resized instance is healthy yet -> wait, do not promote
            cluster["status"]["currentPrimary"] = "dev-2"
            cluster["status"]["targetPrimary"] = "dev-2"
            cluster["status"]["instancesStatus"]["healthy"] = ["dev-1", "dev-2", "dev-3"]
            state = summarize(cluster, pvcs)
            step = describe(cluster, state)
            if "promote_cluster_instance" in step or "finish replicating" not in step:
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Stage 4 recommended a promotion before the new instance was healthy",
                    step[:500], (time.time() - start_time) * 1000,
                )

            # Validation left disabled must be called out first
            cluster["metadata"]["annotations"] = {tools.CNPG_VALIDATION_ANNOTATION: "disabled"}
            state = summarize(cluster, pvcs)
            step = describe(cluster, state)
            if not step.startswith("WARNING: validation is still disabled"):
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "Disabled validation was not reported first",
                    step[:500], (time.time() - start_time) * 1000,
                )

            return TestResult(
                self.get_name(), self.tool_name, True,
                "Resize guidance tracks promote, recycle, wait, complete, and disabled validation",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(), self.tool_name, False,
                "Resize guidance test raised", str(e),
                (time.time() - start_time) * 1000,
            )


class TestInstanceGuards(TestPlugin):
    """Verifies promote and delete refuse unsafe instance operations."""

    tool_name = "promote_cluster_instance"
    description = "Verifies unhealthy promotion and primary deletion are refused"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestResizeNextStepGuidance"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            tools = _import_tools()
            promote_impl = tools.promote_cluster_instance.__wrapped__
            delete_impl = tools.delete_cluster_instance.__wrapped__

            lagging = _cluster(healthy=["dev-1", "dev-2"])
            lagging["status"]["instanceNames"].append("dev-4")

            with (
                patch("cnpg_mcp_tools.get_cnpg_cluster", return_value=lagging),
                patch("cnpg_mcp_tools.patch_cnpg_cluster_status") as status_patch,
            ):
                refused = await promote_impl(None, name="dev", instance="dev-4", namespace="claude")
                if status_patch.called or "not currently reported healthy" not in refused:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Promotion of an unhealthy instance was not refused",
                        refused[:500], (time.time() - start_time) * 1000,
                    )

                forced = await promote_impl(None, name="dev", instance="dev-4", force=True, namespace="claude")
                if not status_patch.called or "not reported healthy" not in forced:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "force=True did not perform the switchover with a warning",
                        forced[:500], (time.time() - start_time) * 1000,
                    )
                if status_patch.call_args.args[2:] and status_patch.call_args.args[2] != {"targetPrimary": "dev-4"}:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Switchover patched an unexpected status field",
                        str(status_patch.call_args), (time.time() - start_time) * 1000,
                    )

            with (
                patch("cnpg_mcp_tools.get_cnpg_cluster", return_value=_cluster()),
                patch("cnpg_mcp_tools.get_kubernetes_clients") as get_clients,
            ):
                refused = await delete_impl(
                    None, name="dev", instance="dev-2", confirm_deletion=True, namespace="claude",
                )
                if get_clients.called or "is the current primary" not in refused:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Deleting the primary instance was not refused",
                        refused[:500], (time.time() - start_time) * 1000,
                    )

            return TestResult(
                self.get_name(), self.tool_name, True,
                "Promote and delete refuse unsafe instance operations",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(), self.tool_name, False,
                "Instance guard test raised", str(e),
                (time.time() - start_time) * 1000,
            )
