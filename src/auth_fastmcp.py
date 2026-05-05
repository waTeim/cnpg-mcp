"""
FastMCP auth provider configuration for cnpg-mcp MCP Server.

This module defines helpers for ALL supported auth modes — `auth0`, `keycloak`,
and `oidc` — regardless of the build-time `auth_type` the scaffold was
generated with. Runtime auth-type resolution lives in the entry point
(`_resolve_auth_type`); this module only needs to expose the right factory
function to whichever branch the entry point dispatches to.

Two architecturally distinct DCR patterns:

- Pattern A (Proxy) — `auth0`: FastMCP `Auth0Provider` runs its own DCR
  endpoint, mints MCP-side JWTs, persists upstream tokens in Redis, signs
  with a local JWT signing key.
- Pattern B (Remote) — `keycloak`: FastMCP `KeycloakAuthProvider` publishes
  RFC 9728 Protected Resource metadata pointing at the IdP. The IdP serves
  DCR directly; tokens are verified against its JWKS. No local client
  credentials, no Redis, no JWT signing key. Requires Keycloak >= 26.6.0
  and fastmcp >= 3.2.4.

The generic `oidc` branch is implemented in `auth_oidc.py`, not here.

See mcp-base patterns/authentication.md ("DCR model per provider") for the
full explanation.
"""

import os
import logging
import secrets
from typing import Optional, Dict, Any
from pathlib import Path

# Optional Redis storage backend for Pattern A (Auth0) OAuth session
# persistence. Absent in Pattern B (Keycloak) deployments — the import must
# not be required at module load time.
try:
    from key_value.aio.stores.redis import RedisStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from cryptography.fernet import Fernet
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    RedisStore = None
    FernetEncryptionWrapper = None
    Fernet = None

logger = logging.getLogger(__name__)

if os.getenv("DEBUG", "").lower() in ("true", "1", "yes"):
    logger.setLevel(logging.DEBUG)
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger.debug("DEBUG logging enabled for auth_fastmcp module")
else:
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


# ============================================================================
# Shared config loading
# ============================================================================

def load_oidc_config_from_file(config_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load OIDC configuration from a YAML file.

    Searches in order:
    1. Provided config_path
    2. /etc/mcp/oidc/oidc.yaml (default Kubernetes ConfigMap mount)
    3. /config/oidc.yaml
    4. ./oidc.yaml
    """
    search_paths = []
    if config_path:
        search_paths.append(config_path)
    search_paths.extend([
        "/etc/mcp/oidc/oidc.yaml",
        "/config/oidc.yaml",
        "./oidc.yaml",
    ])

    for path_str in search_paths:
        path = Path(path_str)
        if path.exists() and path.is_file():
            try:
                import yaml
                with open(path, 'r') as f:
                    config = yaml.safe_load(f) or {}
                logger.info(f"Loaded OIDC config from: {path}")
                return config
            except ImportError:
                logger.error("PyYAML not installed. Install with: pip install pyyaml")
                raise
            except Exception as e:
                logger.warning(f"Failed to load config from {path}: {e}")
                continue

    logger.warning("No OIDC config file found; falling back to environment variables")
    return None


def _normalize_scopes(value: Any) -> Optional[list]:
    """Normalize scope config from YAML/env into a list of non-empty strings."""
    if value is None:
        return None
    if isinstance(value, str):
        raw_items = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            if item is None:
                continue
            raw_items.extend(str(item).replace(",", " ").split())
    else:
        raw_items = str(value).replace(",", " ").split()

    scopes = []
    for item in raw_items:
        scope = item.strip()
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes or None


def _resolve_required_scopes(
    config: Dict[str, Any],
    default: Optional[list] = None,
) -> Optional[list]:
    """
    Resolve required scopes from config/env.

    Accepted forms, in priority order:
    - required_scopes: ["scope-a", "scope-b"] or "scope-a scope-b"
    - scope: "scope-a"
    - OIDC_REQUIRED_SCOPES
    - OIDC_SCOPE
    """
    for source in (
        config.get("required_scopes"),
        config.get("scope"),
        os.getenv("OIDC_REQUIRED_SCOPES"),
        os.getenv("OIDC_SCOPE"),
    ):
        scopes = _normalize_scopes(source)
        if scopes:
            return scopes
    return list(default) if default else None


# ============================================================================
# Pattern A — Auth0 OAuth proxy
# ============================================================================

def load_client_secret(config: Dict[str, Any]) -> str:
    """Load Auth0 client secret from file, config, or AUTH0_CLIENT_SECRET env."""
    client_secret_file = config.get("client_secret_file")
    if client_secret_file:
        try:
            secret_path = Path(client_secret_file)
            if secret_path.exists():
                client_secret = secret_path.read_text().strip()
                logger.info(f"Loaded client secret from: {client_secret_file}")
                return client_secret
            else:
                logger.warning(f"Client secret file not found: {client_secret_file}")
        except Exception as e:
            logger.warning(f"Could not load client secret from file: {e}")

    client_secret = config.get("client_secret") or os.getenv("AUTH0_CLIENT_SECRET")
    if client_secret:
        logger.info("Loaded client secret from config/environment")
        return client_secret

    raise ValueError("No client secret found. Set client_secret_file, client_secret, or AUTH0_CLIENT_SECRET")


def load_jwt_signing_key(config: Dict[str, Any]) -> str:
    """Load (or generate) the MCP-side JWT signing key for Pattern A."""
    key_file = config.get("jwt_signing_key_file")
    if key_file:
        try:
            key_path = Path(key_file)
            if key_path.exists():
                key = key_path.read_text().strip()
                logger.info(f"Loaded JWT signing key from file: {key_file}")
                return key
            else:
                logger.warning(f"JWT signing key file not found: {key_file}")
        except Exception as e:
            logger.error(f"Could not load JWT signing key from file: {e}")

    key = config.get("jwt_signing_key") or os.getenv("JWT_SIGNING_KEY")
    if key:
        logger.info("Loaded JWT signing key from config/environment")
        return key

    logger.warning("WARNING: No JWT signing key provided!")
    logger.warning("Generating random key - NOT suitable for production!")
    logger.warning("Tokens will become INVALID after pod restarts!")
    logger.warning("Set jwt_signing_key_file, jwt_signing_key, or JWT_SIGNING_KEY")
    random_key = secrets.token_hex(32)
    logger.info(f"Generated random JWT signing key: {random_key[:8]}...")
    return random_key


def create_redis_client_storage(config: Dict[str, Any]):
    """Create encrypted Redis client storage for Pattern A OAuth sessions.

    Returns None if py-key-value-aio[redis] is not installed (which is the
    expected state for Pattern B / Keycloak deployments).
    """
    if not REDIS_AVAILABLE:
        logger.warning("Redis storage not available - install py-key-value-aio[redis]")
        logger.warning("OAuth sessions will not persist across restarts")
        return None

    redis_config = config.get("redis", {})
    host = redis_config.get("host") or os.getenv("REDIS_HOST", "localhost")
    port = redis_config.get("port") or int(os.getenv("REDIS_PORT", "6379"))
    db = redis_config.get("db") or int(os.getenv("REDIS_DB", "0"))
    password = redis_config.get("password") or os.getenv("REDIS_PASSWORD")

    try:
        logger.info(f"Connecting to Redis: {host}:{port}/{db}")

        redis_store = RedisStore(
            host=host,
            port=port,
            db=db,
            password=password if password else None,
        )

        encryption_key_file = config.get("storage_encryption_key_file")
        encryption_key = None
        if encryption_key_file:
            try:
                key_path = Path(encryption_key_file)
                if key_path.exists():
                    encryption_key = key_path.read_bytes()
                    logger.info(f"Loaded storage encryption key from: {encryption_key_file}")
                else:
                    logger.warning(f"Storage encryption key file not found: {encryption_key_file}")
            except Exception as e:
                logger.error(f"Could not load encryption key from file: {e}")

        if not encryption_key:
            encryption_key = config.get("storage_encryption_key")
            if encryption_key and isinstance(encryption_key, str):
                encryption_key = encryption_key.encode()

        if not encryption_key:
            logger.warning("WARNING: No storage encryption key provided!")
            logger.warning("Generating random key - NOT suitable for production!")
            encryption_key = Fernet.generate_key()

        client_storage = FernetEncryptionWrapper(
            key_value=redis_store,
            fernet=Fernet(encryption_key),
        )
        logger.info("Redis client storage configured successfully")
        return client_storage
    except Exception as e:
        logger.error(f"Failed to create Redis storage: {e}")
        logger.warning("OAuth sessions will not persist across restarts")
        return None


def create_auth0_oauth_proxy(config_path: Optional[str] = None):
    """Create and configure FastMCP Auth0Provider (Pattern A — proxy).

    Imports `Auth0Provider` lazily so Pattern B (Keycloak) deployments don't
    require the auth0 provider's optional dependencies at module-import time.
    """
    from fastmcp.server.auth.providers.auth0 import Auth0Provider

    logger.info("=" * 70)
    logger.info("Initializing FastMCP Auth0 Provider for cnpg-mcp")
    logger.info("=" * 70)

    config = load_oidc_config_from_file(config_path) or {}

    issuer = config.get("issuer") or os.getenv("OIDC_ISSUER")
    audience = config.get("audience") or os.getenv("OIDC_AUDIENCE")
    client_id = config.get("client_id") or os.getenv("AUTH0_CLIENT_ID")
    public_url = config.get("public_url") or os.getenv("PUBLIC_URL")

    if not issuer:
        raise ValueError("OIDC issuer is required. Set 'issuer' in config file or OIDC_ISSUER environment variable")
    if not audience:
        raise ValueError("OIDC audience is required. Set 'audience' in config file or OIDC_AUDIENCE environment variable")
    if not client_id:
        raise ValueError("Auth0 client ID is required. Set 'client_id' in config file or AUTH0_CLIENT_ID environment variable")
    if not public_url:
        raise ValueError("Public URL is required. Set 'public_url' in config file or PUBLIC_URL environment variable")

    client_secret = load_client_secret(config)
    jwt_signing_key = load_jwt_signing_key(config)
    client_storage = create_redis_client_storage(config)
    required_scopes = _resolve_required_scopes(
        config,
        default=["openid", "offline_access"],
    )

    issuer = issuer.rstrip('/')
    config_url = f"{issuer}/.well-known/openid-configuration"

    logger.info("Configuring FastMCP Auth0 Provider:")
    logger.info(f"  Issuer: {issuer}")
    logger.info(f"  Config URL: {config_url}")
    logger.info(f"  Audience: {audience}")
    logger.info(f"  Client ID: {client_id}")
    logger.info(f"  Public URL: {public_url}")
    logger.info(f"  Required scopes: {required_scopes}")
    logger.info(f"  Client Storage: {'Redis (persistent)' if client_storage else 'In-memory (not persistent)'}")

    provider_kwargs = {
        "config_url": config_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "audience": audience,
        "base_url": public_url,
        "redirect_path": "/auth/callback",
        "required_scopes": required_scopes,
        "require_authorization_consent": True,
        "jwt_signing_key": jwt_signing_key,
    }
    if client_storage:
        provider_kwargs["client_storage"] = client_storage

    auth_provider = Auth0Provider(**provider_kwargs)

    logger.info("=" * 70)
    logger.info("FastMCP Auth0 Provider configured successfully")
    logger.info("=" * 70)
    return auth_provider


def get_auth0_config_summary(issuer: str, audience: str, client_id: str, public_url: str) -> Dict[str, Any]:
    """Summary of Auth0 (Pattern A) provider configuration for logging."""
    return {
        "provider": "Auth0",
        "issuer": issuer,
        "audience": audience,
        "client_id": client_id,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "public_url": public_url,
        "redirect_path": "/auth/callback",
        "pkce_enabled": True,
        "consent_required": True,
    }


# ============================================================================
# Pattern B — Keycloak native DCR
# ============================================================================

def create_keycloak_auth_provider(config_path: Optional[str] = None):
    """Create and configure FastMCP KeycloakAuthProvider (Pattern B — remote).

    Imports `KeycloakAuthProvider` lazily so Pattern A (Auth0) deployments
    that may pin an older fastmcp version don't break at module-import time.
    """
    from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

    logger.info("=" * 70)
    logger.info("Initializing FastMCP Keycloak Auth Provider for cnpg-mcp")
    logger.info("=" * 70)

    config = load_oidc_config_from_file(config_path) or {}

    realm_url = (
        config.get("realm_url")
        or config.get("issuer")
        or os.getenv("KEYCLOAK_REALM_URL")
        or os.getenv("OIDC_ISSUER")
    )
    public_url = config.get("public_url") or os.getenv("PUBLIC_URL")
    audience = config.get("audience") or os.getenv("OIDC_AUDIENCE")
    required_scopes = _resolve_required_scopes(config)

    if not realm_url:
        raise ValueError(
            "Keycloak realm URL is required. Set 'realm_url' (or 'issuer') in config "
            "or KEYCLOAK_REALM_URL/OIDC_ISSUER environment variable"
        )
    if not public_url:
        raise ValueError(
            "Public URL is required. Set 'public_url' in config or PUBLIC_URL environment variable"
        )

    realm_url = str(realm_url).rstrip("/")

    logger.info("Configuring Keycloak Auth Provider:")
    logger.info(f"  Realm URL: {realm_url}")
    logger.info(f"  Public URL: {public_url}")
    logger.info(f"  Audience: {audience or '(not set)'}")
    logger.info(f"  Required scopes: {required_scopes or '(provider default)'}")

    provider_kwargs: Dict[str, Any] = {
        "realm_url": realm_url,
        "base_url": public_url,
    }
    if audience:
        provider_kwargs["audience"] = audience
    if required_scopes:
        provider_kwargs["required_scopes"] = required_scopes

    auth_provider = KeycloakAuthProvider(**provider_kwargs)

    logger.info("=" * 70)
    logger.info("FastMCP Keycloak Auth Provider configured successfully")
    logger.info("  - Tokens verified directly against Keycloak JWKS (no proxy)")
    logger.info("  - Dynamic Client Registration handled by Keycloak")
    logger.info("  - No client_id/secret, JWT signing key, or Redis required")
    logger.info("=" * 70)
    return auth_provider


def get_keycloak_config_summary(realm_url: str, audience: Optional[str], public_url: str) -> Dict[str, Any]:
    """Summary of Keycloak (Pattern B) provider configuration for logging."""
    return {
        "provider": "Keycloak",
        "realm_url": realm_url,
        "authorization_endpoint": f"{realm_url}/protocol/openid-connect/auth",
        "token_endpoint": f"{realm_url}/protocol/openid-connect/token",
        "jwks_uri": f"{realm_url}/protocol/openid-connect/certs",
        "audience": audience,
        "public_url": public_url,
    }