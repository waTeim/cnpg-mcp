#!/usr/bin/env python3
"""
cnpg-mcp MCP Test Server (OIDC Auth or No-Auth Mode)

Test endpoint for the cnpg-mcp MCP server using standard OIDC authentication
or no-auth mode for development/testing.

This is deployed as a sidecar container alongside the main FastMCP OAuth server,
allowing both authentication methods to coexist:
- Main server (port 4200): FastMCP OAuth proxy issuing MCP tokens
- Test server (port 4201): Standard OIDC accepting Auth0 JWT tokens

Both servers share the same tool implementations from cnpg_mcp_tools.py.

No-Auth Mode:
  For development and testing without authentication:
    python cnpg_mcp_test_server.py --no-auth --port 4201

  This injects a mock user identity for tools that require user context.
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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Import shared tools and resources registration
from cnpg_mcp_tools import register_tools, register_resources

# Import OIDC auth
from auth_oidc import OIDCAuthProvider, OIDCAuthMiddleware

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:     %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

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
    "cnpg-mcp-test",
    instructions="""
cnpg-mcp MCP Test Server (OIDC Auth).

This is the test server endpoint that accepts Auth0 JWT tokens directly.
Use this for testing with standard OIDC authentication.
"""
)

# ============================================================================
# Register Resources and Tools
# ============================================================================

register_resources(mcp)
register_tools(mcp)

logger.info("Resources and tools registered with test MCP server")

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
# No-Auth Middleware (for development/testing)
# ============================================================================

class NoAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware that injects mock user claims for no-auth testing mode.

    This allows testing MCP server functionality without requiring
    actual OIDC authentication. The identity is provided via command-line.
    """

    def __init__(self, app, identity: str, exclude_paths: list = None):
        """
        Initialize NoAuth middleware.

        Args:
            app: Starlette application
            identity: Mock user identity to inject
            exclude_paths: Paths to skip (health checks)
        """
        super().__init__(app)
        self.identity = identity
        self.exclude_paths = exclude_paths or ["/healthz", "/readyz"]

        # Create mock claims that mimic OIDC token structure
        self.mock_claims = {
            'sub': identity,
            'preferred_username': identity,
            'name': identity,
            'email': f'{identity}@test.local',
            'iss': 'http://localhost/no-auth',
            'aud': 'mcp-test',
            'scope': 'openid profile email',
        }

    async def dispatch(self, request: Request, call_next):
        """Inject mock claims into all requests."""
        # Skip for health checks
        for excluded in self.exclude_paths:
            if request.url.path.startswith(excluded):
                return await call_next(request)

        # Inject mock claims into request state
        request.state.auth_claims = self.mock_claims
        request.state.user_id = self.identity
        request.state.claims = self.mock_claims  # For user_hash.py

        return await call_next(request)


# ============================================================================
# Main Entry Point
# ============================================================================

def _install_clean_shutdown_handlers() -> None:
    """
    Install SIGTERM/SIGINT handlers that perform a normal sys.exit so atexit
    hooks (notably coverage.py's data-save hook) get to run.

    uvicorn captures these as the "original" handlers in its
    `capture_signals()` context, overrides them while serving, and on
    shutdown restores + re-raises the captured signal — at which point our
    handler runs sys.exit(0), the process exits cleanly, and atexit fires.

    Without this, uvicorn restores Python's default handler and the re-raised
    signal terminates the process with exit 128+sig and skips atexit, which
    causes `coverage run` to write no data file (and any other atexit-based
    cleanup to be skipped).
    """
    import signal

    def _exit(signum: int, _frame) -> None:
        sys.exit(0)

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)


def main():
    """Main entry point for the test server."""
    _install_clean_shutdown_handlers()

    parser = argparse.ArgumentParser(
        description="cnpg-mcp MCP Test Server (OIDC Auth or No-Auth mode)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("TEST_PORT", "4201")),
        help="Port to listen on (default: 4201)"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable authentication (for testing without credentials)"
    )
    parser.add_argument(
        "--identity",
        default="test-user",
        help="Mock user identity when using --no-auth (default: test-user)"
    )

    args = parser.parse_args()

    # Determine auth mode
    auth_mode = "No-Auth" if args.no_auth else "OIDC"

    logger.info("=" * 70)
    logger.info(f"cnpg-mcp MCP Test Server ({auth_mode})")
    logger.info("=" * 70)
    logger.info(f"Listening on: {args.host}:{args.port}")
    logger.info(f"Endpoint: /test")
    if args.no_auth:
        logger.info(f"Auth: DISABLED (no-auth mode)")
        logger.info(f"Identity: {args.identity}")
    else:
        logger.info(f"Auth: Standard OIDC (Auth0 JWT tokens)")
    logger.info("=" * 70)

    # Create FastMCP HTTP app
    app = mcp.http_app(path="/test")

    # Add CORS middleware to handle OPTIONS preflight requests
    from starlette.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allow all origins (customize as needed)
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],  # Explicitly allow OPTIONS
        allow_headers=["*"],
    )

    # Add request logging middleware with MCP message inspection
    class RequestLoggingMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            # Log all non-health-check requests with MCP details
            if request.url.path not in ["/healthz", "/readyz"]:
                mcp_details = await self._extract_mcp_details(request)
                logger.info(f"🌐 HTTP {request.method} {request.url.path}{mcp_details}")

            response = await call_next(request)

            # Log response status for non-health-checks
            if request.url.path not in ["/healthz", "/readyz"]:
                logger.info(f"   ← HTTP {response.status_code}")

            return response

        async def _extract_mcp_details(self, request: Request) -> str:
            """Extract MCP method and tool/resource details from request."""
            try:
                # Read body without consuming it for downstream
                body = await request.body()

                # Parse JSON-RPC message
                import json
                message = json.loads(body)

                method = message.get("method", "unknown")

                # Extract details based on method type
                if method == "tools/call":
                    params = message.get("params", {})
                    tool_name = params.get("name", "unknown")
                    return f" → tools/call({tool_name})"
                elif method == "resources/read":
                    params = message.get("params", {})
                    uri = params.get("uri", "unknown")
                    return f" → resources/read({uri})"
                elif method in ["tools/list", "resources/list", "prompts/list"]:
                    return f" → {method}"
                elif method == "initialize":
                    return f" → initialize"
                else:
                    return f" → {method}"

            except Exception:
                # If we can't parse, just return empty string
                return ""

    logger.info("Adding request logging middleware...")
    app.add_middleware(RequestLoggingMiddleware)

    # Add authentication middleware based on mode
    if args.no_auth:
        logger.info(f"Adding NoAuth middleware (identity: {args.identity})...")
        app.add_middleware(
            NoAuthMiddleware,
            identity=args.identity,
            exclude_paths=["/healthz", "/readyz"]
        )
    else:
        # Create OIDC auth provider
        logger.info("Initializing OIDC authentication...")
        oidc_provider = OIDCAuthProvider()
        logger.info(f"   Issuer: {oidc_provider.issuer}")
        logger.info(f"   Audience: {oidc_provider.audience}")

        logger.info("Adding OIDC authentication middleware...")
        app.add_middleware(
            OIDCAuthMiddleware,
            auth_provider=oidc_provider,
            exclude_paths=["/healthz", "/readyz"]
        )

    # Add health check routes
    app.add_route("/healthz", liveness_check)
    app.add_route("/readyz", readiness_check)

    logger.info("✅ Test server ready")
    logger.info("")
    if args.no_auth:
        logger.info("To test (no authentication required):")
        logger.info(f"  ./test/test-mcp.py --url http://localhost:{args.port}/test --no-auth")
    else:
        logger.info("To test with Auth0 JWT token:")
        logger.info("  1. Get token: ./test/get-user-token.py")
        logger.info("  2. Test: ./test/test-mcp.py --transport http \\")
        logger.info(f"           --url http://localhost:{args.port}/test \\")
        logger.info("           --token-file /tmp/user-token.txt")
    logger.info("")

    # Add health check filter to uvicorn access logger
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    # Run server
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        ws="none"
    )


if __name__ == "__main__":
    main()