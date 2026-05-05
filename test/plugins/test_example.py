"""Smoke test for a non-mutating CloudNativePG tool call."""

import time

from plugins import TestPlugin, TestResult


class TestCreatePostgresClusterDryRun(TestPlugin):
    """Calls create_postgres_cluster in dry-run mode without touching Kubernetes."""

    tool_name = "create_postgres_cluster"
    description = "Verifies create_postgres_cluster renders a dry-run Cluster manifest"
    depends_on = ["TestCnpgToolSurface"]
    run_after = []

    async def test(self, session) -> TestResult:
        start_time = time.time()

        try:
            result = await session.call_tool(
                self.tool_name,
                arguments={
                    "name": "dry-run-db",
                    "namespace": "default",
                    "instances": 1,
                    "storage_size": "1Gi",
                    "postgres_version": "16",
                    "dry_run": True,
                },
            )
            text = result.content[0].text if result.content else str(result)

            expected = [
                "Dry run: PostgreSQL cluster definition",
                "kind: Cluster",
                "name: dry-run-db",
                "namespace: default",
            ]
            missing = [needle for needle in expected if needle not in text]
            if missing:
                return TestResult(
                    plugin_name=self.get_name(),
                    tool_name=self.tool_name,
                    passed=False,
                    message="Dry-run response did not include expected manifest content",
                    error=", ".join(missing),
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
            text = result.content[0].text if result.content else str(result)

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
