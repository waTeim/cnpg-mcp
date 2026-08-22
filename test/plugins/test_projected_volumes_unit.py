"""Unit coverage for simplified projected volume shapes."""

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


# Keys the fake cluster resources expose, keyed by (kind, name).
FAKE_KEYS = {
    ("secret", "db-creds"): ["username", "password"],
    ("secret", "other"): ["token"],
    ("configMap", "app-config"): ["ca.crt", "settings.ini"],
    ("configMap", "clash"): ["password"],
}


async def _fake_read_keys(namespace, source_kind, name):
    keys = FAKE_KEYS.get((source_kind, name))
    if keys is None:
        label = "Secret" if source_kind == "secret" else "ConfigMap"
        raise ValueError(f"{label} '{namespace}/{name}' was not found.")
    return sorted(keys)


class TestProjectedVolumeShapes(TestPlugin):
    """Verifies name/directory input maps every key to a file."""

    tool_name = "configure_projected_volumes"
    description = "Verifies projected volume name and directory shapes"
    depends_on = ["TestCnpgToolSurface"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            tools = _import_tools()

            with patch("cnpg_mcp_tools.read_projection_keys", side_effect=_fake_read_keys):
                # A bare list of names uses the per-type default directory.
                template = await tools.build_projected_volume_template(
                    "claude", ["db-creds"], ["app-config"]
                )
                expected = {"sources": [
                    {"secret": {"name": "db-creds", "items": [
                        {"key": "password", "path": "secret/db-creds/password"},
                        {"key": "username", "path": "secret/db-creds/username"},
                    ]}},
                    {"configMap": {"name": "app-config", "items": [
                        {"key": "ca.crt", "path": "config/app-config/ca.crt"},
                        {"key": "settings.ini", "path": "config/app-config/settings.ini"},
                    ]}},
                ]}
                if template != expected:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Default directories did not follow <type>/<name>/<key>",
                        str(template), (time.time() - start_time) * 1000,
                    )

                # A mapping chooses the directory; None falls back to the default.
                template = await tools.build_projected_volume_template(
                    "claude", {"db-creds": "db", "other": None}, {"app-config": "conf/app"}, "0440"
                )
                paths = [
                    item["path"]
                    for source in template["sources"]
                    for projection in source.values()
                    for item in projection["items"]
                ]
                if paths != [
                    "db/password", "db/username",
                    "secret/other/token",
                    "conf/app/ca.crt", "conf/app/settings.ini",
                ]:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Custom and default directories were not applied as expected",
                        str(paths), (time.time() - start_time) * 1000,
                    )
                if template.get("defaultMode") != 0o440:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Octal defaultMode was not parsed as octal",
                        str(template.get("defaultMode")), (time.time() - start_time) * 1000,
                    )

                # Nothing projected means no template at all.
                if await tools.build_projected_volume_template("claude", None, None) is not None:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Empty input should produce no projected volume template",
                        duration_ms=(time.time() - start_time) * 1000,
                    )

            return TestResult(
                self.get_name(), self.tool_name, True,
                "Name and directory input maps every key under the expected directory",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(), self.tool_name, False,
                "Projected volume shape test raised", str(e),
                (time.time() - start_time) * 1000,
            )


class TestProjectedVolumeValidation(TestPlugin):
    """Verifies malformed projections are rejected before they reach the API."""

    tool_name = "configure_projected_volumes"
    description = "Verifies projected volume path and input validation"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestProjectedVolumeShapes"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            tools = _import_tools()
            build = tools.build_projected_volume_template

            # Neither the Cluster CRD nor the CNPG webhook rejects these paths; they
            # only fail later at Pod admission, so they must be caught here.
            cases = [
                ({"db-creds": "../escape"}, None, None, "'..' path element"),
                ({"db-creds": "/abs"}, None, None, "must be relative"),
                ({"db-creds": "a/../b"}, None, None, "'..' path element"),
                (["db-creds", "db-creds"], None, None, "listed more than once"),
                ({"db-creds": "shared"}, {"clash": "shared"}, None, "would project a file at"),
                ([{"name": "x"}], None, None, "must be resource names"),
                (["missing"], None, None, "was not found"),
                ("db-creds", None, None, "must be a list of names"),
                (None, None, "0400", "no projected_secrets"),
            ]

            with patch("cnpg_mcp_tools.read_projection_keys", side_effect=_fake_read_keys):
                for secrets, config_maps, default_mode, needle in cases:
                    try:
                        await build("claude", secrets, config_maps, default_mode)
                    except ValueError as e:
                        if needle not in str(e):
                            return TestResult(
                                self.get_name(), self.tool_name, False,
                                f"Error for {secrets!r} did not mention {needle!r}",
                                str(e), (time.time() - start_time) * 1000,
                            )
                        continue
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        f"Invalid input {secrets!r} was accepted",
                        duration_ms=(time.time() - start_time) * 1000,
                    )

                # Sharing a directory is legal when no file names collide.
                template = await build("claude", {"db-creds": "shared"}, {"app-config": "shared"})
                if len(template["sources"]) != 2:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Sharing a directory with distinct keys should be allowed",
                        str(template), (time.time() - start_time) * 1000,
                    )

            # Octal strings, decimal ints, and 0o-prefixed strings agree.
            parse = tools.parse_file_mode
            if not (parse("0400", "m") == parse(256, "m") == parse("0o400", "m") == 0o400):
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    "File mode parsing disagreed across octal and decimal forms",
                    duration_ms=(time.time() - start_time) * 1000,
                )
            for bad in [999, -1, "nope"]:
                try:
                    parse(bad, "m")
                except ValueError:
                    continue
                return TestResult(
                    self.get_name(), self.tool_name, False,
                    f"Invalid file mode {bad!r} was accepted",
                    duration_ms=(time.time() - start_time) * 1000,
                )

            return TestResult(
                self.get_name(), self.tool_name, True,
                "Bad paths, duplicates, and collisions are rejected before reaching the API",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(), self.tool_name, False,
                "Projected volume validation test raised", str(e),
                (time.time() - start_time) * 1000,
            )


class TestProjectedVolumeReplacement(TestPlugin):
    """Verifies configure_projected_volumes replaces rather than merges."""

    tool_name = "configure_projected_volumes"
    description = "Verifies projection replacement clears a stale defaultMode"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestProjectedVolumeValidation"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        cluster = {
            "metadata": {"name": "dev", "namespace": "claude"},
            "spec": {
                "instances": 1,
                "projectedVolumeTemplate": {
                    "defaultMode": 0o440,
                    "sources": [{"secret": {"name": "old-secret", "items": [
                        {"key": "k", "path": "secret/old-secret/k"}
                    ]}}],
                },
            },
        }

        try:
            tools = _import_tools()
            patches = []

            async def fake_patch(namespace, name, spec_patch):
                patches.append(spec_patch)
                return {}

            configure_impl = tools.configure_projected_volumes.__wrapped__
            with (
                patch("cnpg_mcp_tools.get_cnpg_cluster", return_value=cluster),
                patch("cnpg_mcp_tools.patch_cnpg_cluster_spec", side_effect=fake_patch),
                patch("cnpg_mcp_tools.read_projection_keys", side_effect=_fake_read_keys),
            ):
                # A replacement without a defaultMode must null out the stale one,
                # because a JSON merge patch would otherwise keep it.
                await configure_impl(None, name="dev", projected_config_maps=["app-config"], namespace="claude")
                replaced = patches[-1]["projectedVolumeTemplate"]
                if replaced.get("defaultMode") is not None:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Replacement did not clear the previous defaultMode",
                        str(replaced), (time.time() - start_time) * 1000,
                    )
                if [s for s in replaced["sources"] if "secret" in s]:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Replacement kept the previous secret source",
                        str(replaced), (time.time() - start_time) * 1000,
                    )

                # clear=True removes the whole field.
                await configure_impl(None, name="dev", clear=True, namespace="claude")
                if patches[-1] != {"projectedVolumeTemplate": None}:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "clear=True did not null out the projected volume template",
                        str(patches[-1]), (time.time() - start_time) * 1000,
                    )

                # Guards must not reach the API at all.
                before = len(patches)
                no_args = await configure_impl(None, name="dev", namespace="claude")
                conflict = await configure_impl(
                    None, name="dev", clear=True, projected_secrets=["db-creds"], namespace="claude",
                )
                bad_path = await configure_impl(
                    None, name="dev", projected_secrets={"db-creds": "../x"}, namespace="claude",
                )
                if len(patches) != before:
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "A rejected request still patched the cluster",
                        str(patches[before:]), (time.time() - start_time) * 1000,
                    )
                if not all("Error:" in r for r in (no_args, conflict, bad_path)):
                    return TestResult(
                        self.get_name(), self.tool_name, False,
                        "Guards did not return errors",
                        f"{no_args[:150]} | {conflict[:150]} | {bad_path[:150]}",
                        (time.time() - start_time) * 1000,
                    )

            return TestResult(
                self.get_name(), self.tool_name, True,
                "configure_projected_volumes replaces the template and guards bad input",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                self.get_name(), self.tool_name, False,
                "Projected volume replacement test raised", str(e),
                (time.time() - start_time) * 1000,
            )
