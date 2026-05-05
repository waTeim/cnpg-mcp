#!/usr/bin/env python3
"""
cnpg-mcp MCP Server - Entry Point

MCP server using FastMCP with oidc authentication.
HTTP transport only (Streamable HTTP).

Tool implementations are in cnpg_mcp_tools.py
"""

import argparse
import logging
import sys
import os
import warnings

# Suppress deprecation warnings from dependencies
warnings.filterwarnings("ignore", category=DeprecationWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*HTTPResponse.getheaders.*")

from fastmcp import FastMCP, Context
import uvicorn
from starlette.routing import Route
from starlette.responses import JSONResponse

# Import tool and resource registration from tools module
from cnpg_mcp_tools import register_tools, register_resources

# Build-time default — used only when neither the mounted oidc.yaml nor the
# OIDC_AUTH_TYPE env var supplies a value. The actual auth_type used at
# runtime is resolved by _resolve_auth_type() below so the helm chart's
# `oidc.authType` (which renders into the mounted ConfigMap) controls which
# branch runs, and a build-time/runtime mismatch surfaces as a warning
# instead of a silent wrong-branch dispatch.
_AUTH_TYPE_BUILD_DEFAULT = "oidc"
_VALID_AUTH_TYPES = ("auth0", "keycloak", "oidc")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:     %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)


def _resolve_auth_type() -> str:
    """Pick the auth_type that drives transport dispatch.

    Resolution order (highest priority first):
      1. `auth_type` field in the mounted oidc.yaml ConfigMap
      2. `OIDC_AUTH_TYPE` environment variable
      3. The build-time default baked in by the scaffold generator

    Logs the chosen value and its source. Logs a WARNING when the runtime
    value differs from the build-time default, since that historically
    caused silent wrong-branch dispatch (e.g. helm chart deploys
    `authType: keycloak` but the entry point was generated with
    `auth_type: oidc` and runs the wrong transport).
    """
    # Late import — auth_oidc / auth_fastmcp both expose this helper, and
    # they're only safe to import once their dependencies are installed.
    try:
        from auth_oidc import load_oidc_config_from_file
    except ImportError:
        try:
            from auth_fastmcp import load_oidc_config_from_file
        except ImportError:
            load_oidc_config_from_file = None  # type: ignore[assignment]

    config_value = None
    if load_oidc_config_from_file is not None:
        try:
            config = load_oidc_config_from_file() or {}
            config_value = config.get("auth_type")
        except Exception as e:
            logger.warning(f"Could not load oidc.yaml to read auth_type: {e}")

    env_value = os.getenv("OIDC_AUTH_TYPE")

    if config_value:
        chosen, source = config_value, "oidc.yaml"
    elif env_value:
        chosen, source = env_value, "OIDC_AUTH_TYPE env"
    else:
        chosen, source = _AUTH_TYPE_BUILD_DEFAULT, "build-time default"

    if chosen not in _VALID_AUTH_TYPES:
        raise ValueError(
            f"Invalid auth_type {chosen!r} from {source}. "
            f"Expected one of {_VALID_AUTH_TYPES}."
        )

    logger.info(f"🔐 auth_type = {chosen!r} (source: {source})")
    if chosen != _AUTH_TYPE_BUILD_DEFAULT:
        logger.warning(
            f"⚠️  Runtime auth_type {chosen!r} differs from build-time "
            f"default {_AUTH_TYPE_BUILD_DEFAULT!r}. The runtime value wins, "
            f"but if you also customized cnpg_mcp_tools.py or "
            f"chart templates for {_AUTH_TYPE_BUILD_DEFAULT!r}, regenerate "
            f"the scaffold with --auth-type {chosen} to keep them aligned."
        )
    return chosen

# Suppress noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)

# Custom filter to exclude health check endpoints from access logs
class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Exclude health check paths from access logs
        return not any(path in record.getMessage() for path in ["/healthz", "/readyz", "/health"])

# ============================================================================
# FastMCP Server Initialization
# ============================================================================

mcp = FastMCP(
    "cnpg-mcp",
    instructions="""
cnpg-mcp MCP Server.

This server provides tools for managing CloudNativePG clusters, roles,
and databases in Kubernetes environments.
"""
)

# ============================================================================
# Register Resources and Tools from tools module
# ============================================================================

register_resources(mcp)
register_tools(mcp)

logger.info("Resources and tools registered with MCP server")

# ============================================================================
# Health Check Endpoints
# ============================================================================

async def liveness_check(request):
    """Kubernetes liveness probe endpoint."""
    return JSONResponse({"status": "alive"})


async def readiness_check(request):
    """Kubernetes readiness probe endpoint."""
    return JSONResponse({"status": "ready"})


# ============================================================================
# HTTP Transport
# ============================================================================

def run_http_transport_auth0(host: str, port: int):
    """Run server in HTTP mode with FastMCP Auth0 OAuth Proxy."""
    from auth_fastmcp import create_auth0_oauth_proxy, get_auth0_config_summary, load_oidc_config_from_file

    logger.info("Initializing FastMCP Auth0 OAuth Proxy...")

    # Load configuration
    config = load_oidc_config_from_file() or {}
    issuer = config.get("issuer") or os.getenv("OIDC_ISSUER") or ""
    audience = config.get("audience") or os.getenv("OIDC_AUDIENCE") or ""
    client_id = config.get("client_id") or os.getenv("AUTH0_CLIENT_ID") or ""
    public_url = config.get("public_url") or os.getenv("PUBLIC_URL") or ""

    # Create OAuth Proxy (handles token issuance)
    auth_proxy = create_auth0_oauth_proxy()

    # Log configuration summary
    config_summary = get_auth0_config_summary(issuer, audience, client_id, public_url)
    logger.info("=" * 80)
    logger.info("FastMCP Auth0 OAuth Configuration:")
    logger.info("=" * 80)
    for key, value in config_summary.items():
        logger.info(f"  {key}: {value}")
    logger.info("=" * 80)

    # Set OAuth on mcp instance
    mcp.auth = auth_proxy

    # Create app with OAuth at /mcp endpoint
    app = mcp.http_app(transport="http", path="/mcp")

    # Add CORS middleware to handle OPTIONS preflight requests
    from starlette.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allow all origins (customize as needed)
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],  # Explicitly allow OPTIONS
        allow_headers=["*"],
    )

    # Add health check routes
    app.add_route("/healthz", liveness_check)
    app.add_route("/readyz", readiness_check)

    logger.info("")
    logger.info("=" * 80)
    logger.info("Server Configuration:")
    logger.info("=" * 80)
    logger.info(f"  Listening on: {host}:{port}")
    logger.info(f"  MCP Endpoint: /mcp")
    logger.info(f"  Auth Type: Auth0 (FastMCP OAuth Proxy)")
    logger.info("  Server: cnpg-mcp")
    logger.info(f"  OAuth Discovery: /.well-known/oauth-authorization-server")
    logger.info(f"  Client Registration: /register")
    logger.info("=" * 80)
    logger.info("")
    logger.info("To get an MCP token:")
    logger.info(f"  ./test/get-mcp-token.py --url http://{host}:{port}")
    logger.info("")
    logger.info("To test with MCP token:")
    logger.info("  ./test/test-mcp.py --transport http \\")
    logger.info(f"    --url http://{host}:{port}/mcp \\")
    logger.info("    --token-file /tmp/mcp-token.txt")
    logger.info("")

    # Add health check filter to uvicorn access logger
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    # Run server
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        ws="none"
    )


def run_http_transport_keycloak(host: str, port: int):
    """Run server in HTTP mode with FastMCP's native Keycloak auth provider.

    Uses KeycloakAuthProvider from fastmcp>=3.2.4. Keycloak handles Dynamic
    Client Registration natively (requires Keycloak >= 26.6.0), so no
    client_id/client_secret, JWT signing key, or Redis storage is needed.
    """
    from auth_fastmcp import create_keycloak_auth_provider, get_keycloak_config_summary, load_oidc_config_from_file

    logger.info("Initializing FastMCP Keycloak Auth Provider...")

    # Load configuration (used for the logged summary)
    config = load_oidc_config_from_file() or {}
    realm_url = (
        config.get("realm_url")
        or config.get("issuer")
        or os.getenv("KEYCLOAK_REALM_URL")
        or os.getenv("OIDC_ISSUER")
        or ""
    )
    audience = config.get("audience") or os.getenv("OIDC_AUDIENCE")
    public_url = config.get("public_url") or os.getenv("PUBLIC_URL") or ""

    auth_provider = create_keycloak_auth_provider()

    config_summary = get_keycloak_config_summary(realm_url.rstrip("/"), audience, public_url)
    logger.info("=" * 80)
    logger.info("FastMCP Keycloak Configuration:")
    logger.info("=" * 80)
    for key, value in config_summary.items():
        logger.info(f"  {key}: {value}")
    logger.info("=" * 80)

    mcp.auth = auth_provider

    app = mcp.http_app(transport="http", path="/mcp")

    from starlette.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    app.add_route("/healthz", liveness_check)
    app.add_route("/readyz", readiness_check)

    logger.info("")
    logger.info("=" * 80)
    logger.info("Server Configuration:")
    logger.info("=" * 80)
    logger.info(f"  Listening on: {host}:{port}")
    logger.info(f"  MCP Endpoint: /mcp")
    logger.info(f"  Auth Type: Keycloak (FastMCP KeycloakAuthProvider, DCR)")
    logger.info("  Server: cnpg-mcp")
    logger.info(f"  OAuth Discovery: /.well-known/oauth-authorization-server")
    logger.info(f"  Client Registration: via Keycloak DCR")
    logger.info("=" * 80)
    logger.info("")
    logger.info("To test with bearer token:")
    logger.info("  ./test/test-mcp.py --transport http \\")
    logger.info(f"    --url http://{host}:{port}/mcp \\")
    logger.info("    --token <your-keycloak-jwt>")
    logger.info("")

    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        ws="none"
    )


def run_http_transport_oidc(host: str, port: int):
    """Run server in HTTP mode with generic OIDC authentication (Dex, Okta, etc.)."""
    from auth_oidc import OIDCAuthProvider, OIDCAuthMiddleware

    logger.info("Initializing Generic OIDC Authentication...")

    # Create OIDC auth provider (auto-discovers from issuer)
    auth_provider = OIDCAuthProvider()

    # Create app WITHOUT built-in auth (we'll add middleware)
    app = mcp.http_app(transport="http", path="/mcp")

    # Add CORS middleware to handle OPTIONS preflight requests
    from starlette.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allow all origins (customize as needed)
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],  # Explicitly allow OPTIONS
        allow_headers=["*"],
    )

    # Add OIDC authentication middleware
    app.add_middleware(
        OIDCAuthMiddleware,
        auth_provider=auth_provider,
        exclude_paths=["/healthz", "/readyz", "/.well-known/", "/register"]
    )

    # Add OAuth metadata routes
    for route in auth_provider.get_metadata_routes():
        app.add_route(route.path, route.endpoint, methods=route.methods)

    # Add health check routes
    app.add_route("/healthz", liveness_check)
    app.add_route("/readyz", readiness_check)

    logger.info("")
    logger.info("=" * 80)
    logger.info("Server Configuration:")
    logger.info("=" * 80)
    logger.info(f"  Listening on: {host}:{port}")
    logger.info(f"  MCP Endpoint: /mcp")
    logger.info(f"  Auth Type: Generic OIDC (Dex, Keycloak, etc.)")
    logger.info(f"  Issuer: {auth_provider.issuer}")
    logger.info(f"  Audience: {auth_provider.audience}")
    logger.info("  Server: cnpg-mcp")
    logger.info(f"  OAuth Discovery: /.well-known/oauth-authorization-server")
    if auth_provider.upstream_dcr_endpoint:
        logger.info(f"  Client Registration: /register (proxied)")
    logger.info("=" * 80)
    logger.info("")
    logger.info("To test with bearer token:")
    logger.info("  ./test/test-mcp.py --transport http \\")
    logger.info(f"    --url http://{host}:{port}/mcp \\")
    logger.info("    --token <your-jwt-token>")
    logger.info("")

    # Add health check filter to uvicorn access logger
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    # Run server
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        ws="none"
    )


def run_http_transport(host: str, port: int):
    """Run server in HTTP mode with configured authentication."""
    auth_type = _resolve_auth_type()
    if auth_type == "keycloak":
        run_http_transport_keycloak(host, port)
    elif auth_type == "oidc":
        run_http_transport_oidc(host, port)
    else:
        run_http_transport_auth0(host, port)


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="cnpg-mcp MCP Server"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "4200")),
        help="Port for HTTP transport (default: 4200)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host for HTTP transport (default: 0.0.0.0)"
    )

    args = parser.parse_args()
    run_http_transport(args.host, args.port)


if __name__ == "__main__":
    main()
