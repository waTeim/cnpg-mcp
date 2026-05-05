#!/usr/bin/env python3
"""
cnpg-mcp MCP Server - Startup Smoke Test

Exercises FastMCP prompt/resource/tool registration against a fresh in-process
FastMCP instance. Catches scaffold contract violations (e.g. invalid prompt
handler signatures) at build time, before the pod ever starts.

This is NOT a runtime MCP test — it does not open a network listener, does not
need auth credentials, and does not require Kubernetes. It only verifies that
register_resources / register_tools / register_prompts can all be invoked
without FastMCP raising.

Usage:
    python bin/smoke_test.py

Exit codes:
    0 - all registrations succeeded
    1 - a registration call raised
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR if SRC_DIR.exists() else REPO_ROOT))


# ----------------------------------------------------------------------------
# Sample prompt bundle
# ----------------------------------------------------------------------------
# Without a real prompts file, register_prompts() is a no-op (the registry
# loads zero prompts) and would not catch FastMCP signature-contract failures.
# We point PROMPTS_PATH at a temporary bundle that exercises the three shapes
# the registration code generates: no args, required-only, and optional args.

_SAMPLE_PROMPTS = """\
version: "0.0.0-smoke"
prompts:
  - id: smoke-no-args
    name: Smoke (no arguments)
    description: Smoke-test prompt with no arguments.
    template: "ok"
  - id: smoke-required
    name: Smoke (required argument)
    description: Smoke-test prompt with a required argument.
    template: "hello NAME"
    arguments:
      - name: subject
        description: Subject of the greeting.
        required: true
  - id: smoke-mixed
    name: Smoke (mixed arguments)
    description: Smoke-test prompt with required and optional arguments.
    template: "hello NAME SUFFIX"
    arguments:
      - name: subject
        description: Subject of the greeting.
        required: true
      - name: suffix
        description: Optional trailing text.
        required: false
"""


_EXPECTED_OIDC_DISCOVERY_PATHS = frozenset({
    # MCP clients (Codex, Claude Desktop) probe each spec at three URL variants:
    # the standard well-known root, the path-suffixed (RFC 9728-style) form, and
    # the path-prefixed (RFC 8615 §3) form. All nine must answer 200 — a 404 on
    # any one breaks client initialization.
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/mcp/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/openid-configuration/mcp",
    "/mcp/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/mcp/.well-known/oauth-protected-resource",
})


_REQUIRED_AUTH_FASTMCP_SYMBOLS = (
    # Pattern A — Auth0 (proxy)
    "create_auth0_oauth_proxy",
    "get_auth0_config_summary",
    "load_client_secret",
    "load_jwt_signing_key",
    "create_redis_client_storage",
    # Pattern B — Keycloak (remote DCR)
    "create_keycloak_auth_provider",
    "get_keycloak_config_summary",
    # Shared
    "load_oidc_config_from_file",
)


def _check_auth_fastmcp_surface() -> tuple[bool, str]:
    """
    auth_fastmcp.py must define every helper used by ANY entry-point branch,
    regardless of build-time auth_type. The runtime auth_type resolver
    (_resolve_auth_type in the entry point) can dispatch to keycloak / auth0
    at startup based on oidc.yaml or OIDC_AUTH_TYPE env, so missing helpers
    surface as ImportError far past unit test coverage.
    """
    try:
        import auth_fastmcp
    except ImportError as e:
        return False, f"could not import auth_fastmcp: {e}"

    missing = [s for s in _REQUIRED_AUTH_FASTMCP_SYMBOLS if not hasattr(auth_fastmcp, s)]
    if missing:
        return False, f"missing helpers: {sorted(missing)}"
    return True, f"all {len(_REQUIRED_AUTH_FASTMCP_SYMBOLS)} cross-mode helpers present"


def _check_oidc_discovery_surface() -> tuple[bool, str]:
    """
    Build an OIDCAuthProvider with mocked discovery HTTP and assert its
    metadata routes cover every URL variant MCP clients probe.

    Skipped silently if auth_oidc is not part of this scaffold or fails to
    import — this check is only meaningful for the generic OIDC branch.
    """
    try:
        from unittest.mock import patch, MagicMock
        import auth_oidc
    except ImportError:
        return True, "skipped (auth_oidc not present)"

    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {}
    fake_response.raise_for_status.return_value = None

    with patch.object(auth_oidc, "httpx", MagicMock(get=MagicMock(return_value=fake_response))), \
         patch.object(auth_oidc, "load_oidc_config_from_file", return_value=None):
        provider = auth_oidc.OIDCAuthProvider(
            issuer="https://example.invalid",
            audience="https://example.invalid/mcp",
            jwks_uri="https://example.invalid/jwks",
            resource_path="/mcp",
        )
        route_paths = {r.path for r in provider.get_metadata_routes()}

    missing = _EXPECTED_OIDC_DISCOVERY_PATHS - route_paths
    if missing:
        return False, f"missing discovery routes: {sorted(missing)}"
    return True, f"all {len(_EXPECTED_OIDC_DISCOVERY_PATHS)} discovery variants registered"


def main() -> int:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(_SAMPLE_PROMPTS)
        prompts_path = f.name
    os.environ["PROMPTS_PATH"] = prompts_path

    try:
        from fastmcp import FastMCP
        from cnpg_mcp_tools import (
            register_resources,
            register_tools,
            register_prompts,
        )

        mcp = FastMCP("cnpg-mcp (smoke)")

        for step, fn in (
            ("register_resources", register_resources),
            ("register_tools", register_tools),
            ("register_prompts", register_prompts),
        ):
            try:
                fn(mcp)
            except Exception:
                print(f"FAIL: {step} raised", file=sys.stderr)
                traceback.print_exc()
                return 1

        ok, detail = _check_auth_fastmcp_surface()
        if not ok:
            print(f"FAIL: auth_fastmcp helper surface — {detail}", file=sys.stderr)
            return 1
        print(f"OK: auth_fastmcp helper surface — {detail}.")

        ok, detail = _check_oidc_discovery_surface()
        if not ok:
            print(f"FAIL: OIDC discovery surface — {detail}", file=sys.stderr)
            return 1
        print(f"OK: OIDC discovery surface — {detail}.")

        print("OK: register_resources, register_tools, register_prompts all succeeded.")
        return 0
    finally:
        try:
            os.unlink(prompts_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())