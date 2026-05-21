"""Smoke test for a non-mutating CloudNativePG tool call."""

import time

import yaml

from plugins import TestPlugin, TestResult


def _content_text(result) -> str:
    if not getattr(result, "content", None):
        return str(result)
    return "".join(getattr(part, "text", None) or str(part) for part in result.content)


def _yaml_from_response(text: str) -> dict:
    marker = "```yaml"
    start = text.find(marker)
    if start == -1:
        raise ValueError(f"response does not contain a yaml code block: {text[:500]}")

    yaml_start = start + len(marker)
    yaml_end = text.find("```", yaml_start)
    if yaml_end == -1:
        raise ValueError(f"response yaml code block is not closed: {text[:500]}")

    return yaml.safe_load(text[yaml_start:yaml_end])


async def _tool_input_properties(session, tool_name: str) -> dict:
    result = await session.list_tools()
    for tool in result.tools:
        if tool.name != tool_name:
            continue

        schema = (
            getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None)
        )
        if schema is None and hasattr(tool, "model_dump"):
            tool_data = tool.model_dump(by_alias=True)
            schema = tool_data.get("inputSchema") or tool_data.get("input_schema")
        if hasattr(schema, "model_dump"):
            schema = schema.model_dump(by_alias=True)
        return (schema or {}).get("properties", {})

    return {}


class TestCreatePostgresClusterDryRun(TestPlugin):
    """Calls create_postgres_cluster in dry-run mode without touching Kubernetes."""

    tool_name = "create_postgres_cluster"
    description = "Verifies create_postgres_cluster renders a dry-run Cluster manifest"
    depends_on = ["TestCnpgToolSurface"]
    run_after = []

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            properties = await _tool_input_properties(session, self.tool_name)
            if "container_image" not in properties:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="create_postgres_cluster server schema is missing container_image",
                    error=(
                        "The MCP server under test is running an older "
                        "create_postgres_cluster_tool signature. Rebuild/restart "
                        "the server from src/cnpg_mcp_tools.py before running this test."
                    ),
                    duration_ms=(time.time() - start_time) * 1000,
                )

            result = await session.call_tool(
                self.tool_name,
                arguments={
                    "name": "dry-run-db",
                    "namespace": "default",
                    "instances": 1,
                    "storage_size": "1Gi",
                    "postgres_version": "16",
                    "container_image": "registry.example.com/postgresql:16.4",
                    "dry_run": True,
                },
            )
            text = _content_text(result)
            manifest = _yaml_from_response(text)

            expected_values = {
                "kind": manifest.get("kind"),
                "metadata.name": manifest.get("metadata", {}).get("name"),
                "metadata.namespace": manifest.get("metadata", {}).get("namespace"),
                "spec.imageName": manifest.get("spec", {}).get("imageName"),
            }
            if expected_values != {
                "kind": "Cluster",
                "metadata.name": "dry-run-db",
                "metadata.namespace": "default",
                "spec.imageName": "registry.example.com/postgresql:16.4",
            }:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Dry-run manifest did not include expected Cluster values",
                    error=f"values={expected_values}; response={text[:500]}",
                    duration_ms=(time.time() - start_time) * 1000,
                )

            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message="create_postgres_cluster dry-run returned a Cluster manifest",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="create_postgres_cluster dry-run call raised",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )


class TestCreatePostgresDatabaseLocaleDryRun(TestPlugin):
    """Calls create_postgres_database in dry-run mode with locale options."""

    tool_name = "create_postgres_database"
    description = "Verifies create_postgres_database renders Database locale fields"
    depends_on = ["TestCnpgToolSurface"]
    run_after = ["TestCreatePostgresClusterDryRun"]

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            result = await session.call_tool(
                self.tool_name,
                arguments={
                    "cluster_name": "dry-run-db",
                    "database_name": "localized-db",
                    "owner": "app",
                    "namespace": "default",
                    "reclaim_policy": "retain",
                    "encoding": "UTF8",
                    "locale_provider": "icu",
                    "locale_collate": "en_US.UTF-8",
                    "locale_ctype": "en_US.UTF-8",
                    "icu_locale": "en-US",
                    "collation_version": "153.120",
                    "dry_run": True,
                },
            )
            text = _content_text(result)

            expected = [
                "kind: Database",
                "name: localized-db",
                "encoding: UTF8",
                "localeProvider: icu",
                "localeCollate: en_US.UTF-8",
                "localeCType: en_US.UTF-8",
                "icuLocale: en-US",
                "collationVersion: '153.120'",
            ]
            missing = [needle for needle in expected if needle not in text]
            if missing:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Dry-run response did not include expected Database locale content",
                    error=", ".join(missing),
                    duration_ms=(time.time() - start_time) * 1000,
                )

            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=True,
                message="create_postgres_database dry-run returned Database locale fields",
                duration_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            return TestResult(
                plugin_name=self.get_name(),
                tool_name=self.tool_name,
                passed=False,
                message="create_postgres_database dry-run call raised",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )
