# CloudNativePG MCP Server

An MCP server for managing PostgreSQL clusters through the
[CloudNativePG](https://cloudnative-pg.io/) operator.

This version uses the MCP Base scaffold for its server layout, authentication,
container build, Helm chart, prompt registry, and test harness. The previous
manual implementation is retained under `deprecated-v1/` for reference.

## Tool Surface

The server exposes the CloudNativePG tools from the v1 implementation:

- `list_postgres_clusters`
- `get_cluster_status`
- `create_postgres_cluster`
- `scale_postgres_cluster`
- `delete_postgres_cluster`
- `list_postgres_roles`
- `create_postgres_role`
- `update_postgres_role`
- `delete_postgres_role`
- `list_postgres_databases`
- `create_postgres_database`
- `delete_postgres_database`

`create_postgres_database` supports CloudNativePG Database CRD create-time
locale options, including `encoding`, `locale`, `locale_provider`,
`locale_collate`, `locale_ctype`, `icu_locale`, `icu_rules`,
`builtin_locale`, and `collation_version`.

It also includes MCP Base scaffold admin tools for prompt management:

- `admin_reload_prompts`
- `admin_get_prompt_manifest`

## Layout

- `src/cnpg_mcp_server.py`: production FastMCP HTTP entrypoint
- `src/cnpg_mcp_test_server.py`: no-auth/OIDC test entrypoint
- `src/cnpg_mcp_tools.py`: CloudNativePG tool implementations and registration
- `src/mcp_context.py`: MCP context wrapper with user identity extraction
- `src/auth_*.py`: MCP Base scaffold authentication support
- `chart/`: Helm deployment assets
- `test/`: MCP plugin test harness
- `SCAFFOLD_INVENTORY.md`: MCP Base scaffold artifact hashes

## Development

Create an environment and install dependencies:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r test/requirements.txt
```

Run the scaffold registration smoke test:

```bash
python bin/smoke_test.py
```

Run the local no-auth MCP test suite:

```bash
python test/run-local-tests.py
```

Run the CloudNativePG Kubernetes integration tests adapted from
`deprecated-v1/test/plugins`:

```bash
python test/run-local-tests.py --include-integration
# or
make test-integration
```

These tests create, scale, update, and delete real CloudNativePG resources.
Useful optional settings:

- `CNPG_MCP_TEST_NAMESPACE`: namespace for test resources
- `CNPG_MCP_TEST_CLUSTER_PREFIX`: generated cluster name prefix
- `CNPG_MCP_TEST_STORAGE_SIZE`: per-instance storage size, default `1Gi`
- `CNPG_MCP_TEST_CREATE_WAIT_SECONDS`: cluster readiness timeout, default `300`
- `CNPG_MCP_TEST_SCALE_WAIT_SECONDS`: scale readiness timeout, default `300`

## Running Locally

The scaffold entrypoint uses HTTP transport:

```bash
python src/cnpg_mcp_server.py --host 0.0.0.0 --port 4200
```

The test server can be run without authentication:

```bash
python src/cnpg_mcp_test_server.py --host 127.0.0.1 --port 4201 --no-auth
```

## Kubernetes Access

The tools use the Kubernetes Python client. They load configuration in this
order:

1. In-cluster service account configuration
2. Local kubeconfig from `~/.kube/config` or `KUBECONFIG`

Most tools accept an optional `namespace`. When omitted, the current Kubernetes
context namespace is used, falling back to `default`.

For in-cluster Helm deployments, the server uses the deployment service account.
By default the chart grants that service account CNPG and secret permissions
only in the Helm release namespace. To manage CNPG resources in another
namespace, pass the tool's `namespace` argument and grant the service account
access there:

```yaml
rbac:
  targetNamespaces:
    - application-databases
```

For a shared MCP deployment that must operate in arbitrary namespaces, opt in to
cluster-wide RBAC:

```yaml
rbac:
  clusterWide: true
```

Cluster-wide mode grants secret access across namespaces, so prefer explicit
`targetNamespaces` when the target set is known.

## Deployment

The MCP Base scaffold includes Docker and Helm assets:

```bash
make build
make push
make helm-install
```

Use `python bin/configure-make.py` to generate `make.env` for image and
namespace settings before using the deployment targets.
