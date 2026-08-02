"""
cnpg-mcp MCP Server - CloudNativePG Tool Implementations

This module contains the MCP tool implementations for managing CloudNativePG
clusters, DatabaseRole CRDs, and Database CRDs. The Kubernetes
business logic is adapted from deprecated-v1/src/cnpg_tools.py and registered
through the MCP Base scaffold registration hooks.
"""

import asyncio
import base64
import json
import logging
import os
import secrets
import string
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from fastmcp import Context
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field

from mcp_context import MCPContext, with_mcp_context
from prompt_registry import get_prompt_registry, reload_prompt_registry

# ============================================================================
# Logging Configuration
# ============================================================================

# Suppress deprecation warnings from dependencies
# These are not from our code and will be fixed when dependencies update
warnings.filterwarnings("ignore", category=DeprecationWarning, module="websockets")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="uvicorn.protocols.websockets")

# Suppress urllib3 deprecation warning (used by kubernetes client)
# Warning: HTTPResponse.getheaders() is deprecated in urllib3 v2.1.0
warnings.filterwarnings("ignore", category=DeprecationWarning, module="urllib3")
warnings.filterwarnings("ignore", message=".*HTTPResponse.getheaders.*")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:     %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

# Set log levels for external libraries to reduce noise
logging.getLogger("httpx").setLevel(logging.WARNING)  # Suppress HTTP request logs
# Note: mcp logger kept at INFO to show "Processing request of type X" logs


# Filter to suppress verbose logs
class VerboseLogsFilter(logging.Filter):
    """Filter out repetitive/verbose logs to reduce noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()

        # Suppress health check and MCP endpoint access logs (redundant with request type logs)
        if any(x in message for x in ["/healthz", "/readyz", "/mcp"]):
            return False

        # Suppress scope validation logs (every request)
        if "Scope validation:" in message:
            return False

        # Suppress session creation/termination (very frequent)
        if any(x in message for x in [
            "Created new transport with session ID:",
            "Terminating session:"
        ]):
            return False

        return True


# Apply filters to reduce log noise
# Note: Only filter uvicorn.access and mcp, NOT auth_oidc (we need auth details for debugging)
logging.getLogger("uvicorn.access").addFilter(VerboseLogsFilter())
logging.getLogger("mcp").addFilter(VerboseLogsFilter())

# ============================================================================
# Configuration and Constants
# ============================================================================

CHARACTER_LIMIT = 25000
CNPG_GROUP = "postgresql.cnpg.io"
CNPG_VERSION = "v1"
CNPG_PLURAL = "clusters"
CNPG_DATABASE_PLURAL = "databases"
CNPG_DATABASE_ROLE_PLURAL = "databaseroles"

# Boolean DatabaseRole attributes, mapped to their display label and the value
# PostgreSQL assumes when the field is left unset in the CRD.
DATABASE_ROLE_FLAGS = {
    "login": ("Login", False),
    "superuser": ("Superuser", False),
    "inherit": ("Inherit", True),
    "createdb": ("Create DB", False),
    "createrole": ("Create Role", False),
    "replication": ("Replication", False),
    "bypassrls": ("Bypass RLS", False),
}

DATABASE_CREATE_OPTION_LABELS = {
    "encoding": "Encoding",
    "locale": "Locale",
    "localeProvider": "Locale Provider",
    "localeCollate": "LC_COLLATE",
    "localeCType": "LC_CTYPE",
    "icuLocale": "ICU Locale",
    "icuRules": "ICU Rules",
    "builtinLocale": "Builtin Locale",
    "collationVersion": "Collation Version",
}

# Transport mode (set via CLI args)
TRANSPORT_MODE = "stdio"  # or "http"

# ============================================================================
# Kubernetes Client Initialization
# ============================================================================

# Kubernetes clients (initialized lazily)
custom_api: Optional[client.CustomObjectsApi] = None
core_api: Optional[client.CoreV1Api] = None
_k8s_init_attempted = False
_k8s_init_error: Optional[str] = None

def get_kubernetes_clients() -> tuple[client.CustomObjectsApi, client.CoreV1Api]:
    """
    Get or initialize Kubernetes API clients (lazy initialization).

    This allows the MCP server to start even if Kubernetes is not available,
    and provides clear error messages when tools are called without K8s access.
    """
    global custom_api, core_api, _k8s_init_attempted, _k8s_init_error

    # Return cached clients if already initialized
    if custom_api is not None and core_api is not None:
        return custom_api, core_api

    # If we already tried and failed, return the cached error
    if _k8s_init_attempted and _k8s_init_error:
        raise Exception(_k8s_init_error)

    # Try to initialize
    _k8s_init_attempted = True

    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        try:
            config.load_kube_config()
            logger.info("Loaded kubeconfig from file")
        except Exception as e:
            _k8s_init_error = (
                f"Failed to load Kubernetes configuration: {e}\n\n"
                "Make sure you have:\n"
                "1. A valid ~/.kube/config file, OR\n"
                "2. KUBECONFIG environment variable set, OR\n"
                "3. Running inside a Kubernetes cluster with proper service account\n\n"
                "You can test your kubectl access with: kubectl cluster-info"
            )
            logger.error(f"Kubernetes initialization failed: {_k8s_init_error}")
            raise Exception(_k8s_init_error)

    custom_api = client.CustomObjectsApi()
    core_api = client.CoreV1Api()

    return custom_api, core_api


def get_current_namespace() -> str:
    """
    Get the current namespace from the Kubernetes context.

    Returns the namespace from the current context in kubeconfig, or reads from
    the pod's service account namespace file when running in-cluster.
    """
    # First, try to read from pod's service account namespace (in-cluster)
    namespace_file = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if namespace_file.exists():
        try:
            namespace = namespace_file.read_text().strip()
            logger.info(f"Using namespace from service account: {namespace}")
            return namespace
        except Exception as e:
            logger.warning(f"Could not read namespace file: {e}")

    # Fall back to kubeconfig context
    try:
        contexts, active_context = config.list_kube_config_contexts()
        if active_context and 'namespace' in active_context.get('context', {}):
            namespace = active_context['context']['namespace']
            logger.info(f"Using namespace from kubeconfig context: {namespace}")
            return namespace
    except Exception as e:
        logger.debug(f"Could not get namespace from kubeconfig context: {e}")

    # Last resort: default namespace
    logger.info("Using default namespace")
    return "default"


# ============================================================================
# Utility Functions
# ============================================================================

def validate_rfc1123_name(name: str, resource_type: str = "resource") -> None:
    """
    Validate that a name conforms to RFC 1123 DNS label standard.

    RFC 1123 requirements for Kubernetes resource names:
    - Must be 63 characters or less
    - Must contain only lowercase alphanumeric characters or '-'
    - Must start with an alphanumeric character
    - Must end with an alphanumeric character

    Args:
        name: The name to validate
        resource_type: Type of resource (for error messages)

    Raises:
        ValueError: If the name doesn't conform to RFC 1123
    """
    if not name:
        raise ValueError(f"{resource_type} name cannot be empty")

    if len(name) > 63:
        raise ValueError(
            f"{resource_type} name '{name}' is too long ({len(name)} characters). "
            f"RFC 1123 DNS labels must be 63 characters or less."
        )

    # Check pattern: lowercase alphanumeric or '-', must start and end with alphanumeric
    import re
    if not re.match(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', name):
        issues = []

        if name[0] not in 'abcdefghijklmnopqrstuvwxyz0123456789':
            issues.append("must start with a lowercase letter or number")

        if len(name) > 1 and name[-1] not in 'abcdefghijklmnopqrstuvwxyz0123456789':
            issues.append("must end with a lowercase letter or number")

        invalid_chars = set(c for c in name if c not in 'abcdefghijklmnopqrstuvwxyz0123456789-')
        if invalid_chars:
            issues.append(f"contains invalid characters: {', '.join(sorted(invalid_chars))}")

        if not any(c.islower() and c.isalpha() for c in name) and not any(c.isupper() for c in name):
            # Check if there are uppercase letters
            pass
        elif any(c.isupper() for c in name):
            issues.append("must be lowercase (uppercase letters are not allowed)")

        raise ValueError(
            f"{resource_type} name '{name}' is invalid. RFC 1123 DNS label requirements:\n"
            f"  - Must contain only lowercase letters (a-z), numbers (0-9), and hyphens (-)\n"
            f"  - Must start and end with a letter or number\n"
            f"  - Must be 63 characters or less\n\n"
            f"Issues found: {'; '.join(issues)}"
        )


def truncate_response(content: str, max_length: int = CHARACTER_LIMIT) -> str:
    """Truncate response content to stay within character limits."""
    if len(content) <= max_length:
        return content
    
    truncated = content[:max_length - 100]
    return f"{truncated}\n\n... (truncated, {len(content) - max_length} characters omitted)"


def format_database_create_options(spec: Dict[str, Any], include_unset: bool = False) -> str:
    """Format optional Database CRD CREATE DATABASE parameters."""
    lines = []
    for field, label in DATABASE_CREATE_OPTION_LABELS.items():
        if field in spec:
            lines.append(f"- {label}: {spec[field]}")
        elif include_unset:
            lines.append(f"- {label}: not set")
    if not lines:
        return "- Defaults: inherited from PostgreSQL/template settings"
    return "\n".join(lines)


def database_create_options_dict(spec: Dict[str, Any], include_unset: bool = False) -> Dict[str, Any]:
    """Return Database CRD CREATE DATABASE parameters as a structured dict."""
    return {
        field: spec.get(field)
        for field in DATABASE_CREATE_OPTION_LABELS
        if include_unset or field in spec
    }


def format_database_object_status(status: Dict[str, Any]) -> str:
    """Format Database CRD reconciliation status."""
    if not status:
        return "- Status: not reported by the operator yet"

    lines = [
        f"- Applied: {status.get('applied', 'unknown')}",
        f"- Observed Generation: {status.get('observedGeneration', 'unknown')}",
        f"- Message: {status.get('message', 'none')}",
    ]

    for object_type in ("schemas", "extensions", "fdws", "servers"):
        objects = status.get(object_type) or []
        if not objects:
            continue
        lines.append(f"- {object_type.title()}:")
        for item in objects:
            name = item.get("name", "unknown")
            applied = item.get("applied", "unknown")
            message = item.get("message", "none")
            lines.append(f"  - {name}: applied={applied}, message={message}")

    return "\n".join(lines)


def format_error_message(error: Exception, context: str = "") -> str:
    """Format error messages in an LLM-friendly, actionable way."""
    if isinstance(error, ApiException):
        status = error.status
        reason = error.reason
        try:
            body = json.loads(error.body) if error.body else {}
            message = body.get('message', str(error))
        except (json.JSONDecodeError, ValueError) as json_error:
            # If the error body isn't valid JSON, use the raw body or string representation
            message = error.body if error.body else str(error)
        
        suggestion = ""
        if status == 404:
            suggestion = "The resource does not exist. Try listing available resources first or check the namespace."
        elif status == 403:
            suggestion = "Permission denied. Verify that the service account has proper RBAC permissions for CloudNativePG resources."
        elif status == 409:
            suggestion = "Resource conflict. The resource may already exist or there's a version conflict."
        elif status == 422:
            suggestion = "Invalid resource specification. Check the cluster specification against CloudNativePG API documentation."
        
        result = f"Kubernetes API Error ({status} {reason})"
        if context:
            result += f" while {context}"
        result += f": {message}"
        if suggestion:
            result += f"\n\nSuggestion: {suggestion}"
        
        return result
    
    return f"Error{' ' + context if context else ''}: {str(error)}"


def generate_password(length: int = 16) -> str:
    """Generate a random alphanumeric password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


async def get_cnpg_cluster(namespace: str, name: str) -> Dict[str, Any]:
    """Get a CloudNativePG cluster resource."""
    try:
        custom_api, _ = get_kubernetes_clients()
        cluster = await asyncio.to_thread(
            custom_api.get_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_PLURAL,
            name=name
        )
        return cluster
    except ApiException as e:
        raise Exception(format_error_message(e, f"getting cluster {namespace}/{name}"))


async def list_cnpg_clusters(namespace: Optional[str] = None) -> List[Dict[str, Any]]:
    """List CloudNativePG cluster resources."""
    try:
        custom_api, _ = get_kubernetes_clients()

        # Default to current namespace if not specified (consistent with other tools)
        if namespace is None:
            namespace = get_current_namespace()

        result = await asyncio.to_thread(
            custom_api.list_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_PLURAL
        )
        return result.get('items', [])
    except ApiException as e:
        raise Exception(format_error_message(e, "listing clusters"))


async def patch_cnpg_cluster_spec(namespace: str, name: str, spec_patch: Dict[str, Any]) -> Dict[str, Any]:
    """Patch only selected Cluster spec fields.

    Patching a full object fetched moments earlier can race with CloudNativePG
    status/spec reconciliation and produce 409 conflicts. A focused merge patch
    avoids carrying a stale metadata.resourceVersion back to the API server.
    """
    try:
        custom_api, _ = get_kubernetes_clients()
        return await asyncio.to_thread(
            custom_api.patch_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_PLURAL,
            name=name,
            body={"spec": spec_patch},
        )
    except ApiException as e:
        raise Exception(format_error_message(e, f"patching cluster {namespace}/{name}"))


async def get_cnpg_database(namespace: str, cluster_name: str, database_name: str) -> Dict[str, Any]:
    """
    Get a Database CRD by logical database name.

    The create/delete tools use <cluster>-<database> as the CRD name, but users
    can create Database CRDs with custom metadata names. Try the conventional
    name first, then fall back to matching spec.cluster.name and spec.name.
    """
    custom_api, _ = get_kubernetes_clients()
    expected_crd_name = f"{cluster_name}-{database_name}"

    try:
        database = await asyncio.to_thread(
            custom_api.get_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_DATABASE_PLURAL,
            name=expected_crd_name,
        )
        spec = database.get("spec", {})
        if spec.get("cluster", {}).get("name") == cluster_name and spec.get("name") == database_name:
            return database
    except ApiException as e:
        if e.status != 404:
            raise Exception(format_error_message(e, f"getting database {namespace}/{expected_crd_name}"))

    try:
        databases = await asyncio.to_thread(
            custom_api.list_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_DATABASE_PLURAL,
        )
    except ApiException as e:
        raise Exception(format_error_message(e, f"listing databases for cluster {namespace}/{cluster_name}"))

    matches = [
        db for db in databases.get("items", [])
        if db.get("spec", {}).get("cluster", {}).get("name") == cluster_name
        and db.get("spec", {}).get("name") == database_name
    ]
    if not matches:
        raise Exception(f"Database CRD for database '{database_name}' in cluster '{namespace}/{cluster_name}' was not found.")
    if len(matches) > 1:
        names = ", ".join(db.get("metadata", {}).get("name", "unknown") for db in matches)
        raise Exception(f"Multiple Database CRDs match database '{database_name}' in cluster '{namespace}/{cluster_name}': {names}")
    return matches[0]


def database_role_crd_name(cluster_name: str, role_name: str) -> str:
    """Return the conventional DatabaseRole CRD name for a cluster/role pair."""
    return f"{cluster_name}-{role_name}"


def role_password_secret_name(cluster_name: str, role_name: str) -> str:
    """Return the conventional password Secret name for a cluster/role pair."""
    return f"cnpg-{cluster_name}-user-{role_name}"


async def list_cnpg_database_roles(namespace: str, cluster_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """List DatabaseRole CRDs in a namespace, optionally filtered by cluster."""
    try:
        custom_api, _ = get_kubernetes_clients()
        result = await asyncio.to_thread(
            custom_api.list_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_DATABASE_ROLE_PLURAL,
        )
    except ApiException as e:
        raise Exception(format_error_message(e, f"listing database roles in namespace {namespace}"))

    roles = result.get("items", [])
    if cluster_name is None:
        return roles
    return [
        role for role in roles
        if role.get("spec", {}).get("cluster", {}).get("name") == cluster_name
    ]


async def get_cnpg_database_role(namespace: str, cluster_name: str, role_name: str) -> Dict[str, Any]:
    """
    Get a DatabaseRole CRD by PostgreSQL role name.

    The create/delete tools use <cluster>-<role> as the CRD name, but users can
    create DatabaseRole CRDs with custom metadata names. Try the conventional
    name first, then fall back to matching spec.cluster.name and spec.name.
    """
    custom_api, _ = get_kubernetes_clients()
    expected_crd_name = database_role_crd_name(cluster_name, role_name)

    try:
        role = await asyncio.to_thread(
            custom_api.get_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_DATABASE_ROLE_PLURAL,
            name=expected_crd_name,
        )
        spec = role.get("spec", {})
        if spec.get("cluster", {}).get("name") == cluster_name and spec.get("name") == role_name:
            return role
    except ApiException as e:
        if e.status != 404:
            raise Exception(format_error_message(e, f"getting database role {namespace}/{expected_crd_name}"))

    matches = [
        role for role in await list_cnpg_database_roles(namespace, cluster_name)
        if role.get("spec", {}).get("name") == role_name
    ]
    if not matches:
        raise Exception(f"DatabaseRole CRD for role '{role_name}' in cluster '{namespace}/{cluster_name}' was not found.")
    if len(matches) > 1:
        names = ", ".join(role.get("metadata", {}).get("name", "unknown") for role in matches)
        raise Exception(f"Multiple DatabaseRole CRDs match role '{role_name}' in cluster '{namespace}/{cluster_name}': {names}")
    return matches[0]


async def find_cnpg_database_role(namespace: str, cluster_name: str, role_name: str) -> Optional[Dict[str, Any]]:
    """Return a DatabaseRole CRD, or None when no CRD matches the role name."""
    try:
        return await get_cnpg_database_role(namespace, cluster_name, role_name)
    except Exception as e:
        if "was not found" in str(e):
            return None
        raise


async def patch_cnpg_database_role_spec(namespace: str, crd_name: str, spec_patch: Dict[str, Any]) -> Dict[str, Any]:
    """Patch selected DatabaseRole spec fields with a focused merge patch."""
    try:
        custom_api, _ = get_kubernetes_clients()
        return await asyncio.to_thread(
            custom_api.patch_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_DATABASE_ROLE_PLURAL,
            name=crd_name,
            body={"spec": spec_patch},
        )
    except ApiException as e:
        raise Exception(format_error_message(e, f"patching database role {namespace}/{crd_name}"))


def database_role_attributes_dict(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return DatabaseRole attributes as a structured dict, with defaults applied."""
    attributes: Dict[str, Any] = {
        field: spec.get(field, default) for field, (_, default) in DATABASE_ROLE_FLAGS.items()
    }
    attributes.update({
        "connection_limit": spec.get("connectionLimit", -1),
        "valid_until": spec.get("validUntil"),
        "comment": spec.get("comment"),
        "in_roles": spec.get("inRoles", []),
        "disable_password": spec.get("disablePassword", False),
        "client_certificate": spec.get("clientCertificate", {}).get("enabled", False),
        "password_secret": spec.get("passwordSecret", {}).get("name"),
    })
    return attributes


def format_database_role_attributes(spec: Dict[str, Any]) -> str:
    """Format DatabaseRole attributes in a human-readable way."""
    lines = [f"- {label}: {spec.get(field, default)}" for field, (label, default) in DATABASE_ROLE_FLAGS.items()]
    lines.append(f"- Connection Limit: {spec.get('connectionLimit', -1)}")

    if spec.get("validUntil"):
        lines.append(f"- Valid Until: {spec['validUntil']}")
    if spec.get("comment"):
        lines.append(f"- Comment: {spec['comment']}")
    if spec.get("inRoles"):
        lines.append(f"- Member of: {', '.join(spec['inRoles'])}")
    if spec.get("disablePassword"):
        lines.append("- Password: disabled (set to NULL in PostgreSQL)")
    else:
        lines.append(f"- Password Secret: {spec.get('passwordSecret', {}).get('name', 'none')}")
    if spec.get("clientCertificate", {}).get("enabled"):
        lines.append("- Client Certificate: enabled")

    return "\n".join(lines)


def format_database_role_object_status(status: Dict[str, Any]) -> str:
    """Format DatabaseRole CRD reconciliation status."""
    if not status:
        return "- Status: not reported by the operator yet"

    lines = [
        f"- Applied: {status.get('applied', 'unknown')}",
        f"- Observed Generation: {status.get('observedGeneration', 'unknown')}",
        f"- Message: {status.get('message', 'none')}",
    ]

    client_certificate = status.get("clientCertificate") or {}
    if client_certificate:
        lines.append(
            f"- Client Certificate: expiration={client_certificate.get('expiration', 'unknown')}, "
            f"message={client_certificate.get('message', 'none')}"
        )

    for condition in status.get("conditions") or []:
        lines.append(
            f"- Condition {condition.get('type', 'unknown')}: {condition.get('status', 'unknown')} "
            f"({condition.get('reason', 'unknown')}: {condition.get('message', 'none')})"
        )

    return "\n".join(lines)


async def read_role_secret(namespace: str, secret_name: str) -> Optional[Any]:
    """Read a role password Secret, returning None when it does not exist."""
    _, core_api = get_kubernetes_clients()
    try:
        return await asyncio.to_thread(
            core_api.read_namespaced_secret,
            name=secret_name,
            namespace=namespace,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


async def delete_role_secret(namespace: str, secret_name: str) -> bool:
    """Delete a role password Secret, returning whether a Secret was deleted."""
    _, core_api = get_kubernetes_clients()
    try:
        await asyncio.to_thread(
            core_api.delete_namespaced_secret,
            name=secret_name,
            namespace=namespace,
        )
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


async def write_role_secret(
    namespace: str,
    secret_name: str,
    cluster_name: str,
    role_name: str,
    password: str,
) -> bool:
    """
    Store a role password in a basic-auth Secret, creating it when absent.

    Returns True when a new Secret was created, False when an existing one was
    updated in place.
    """
    _, core_api = get_kubernetes_clients()

    existing = await read_role_secret(namespace, secret_name)
    if existing is not None:
        existing.data = dict(existing.data or {})
        existing.data["username"] = base64.b64encode(role_name.encode()).decode()
        existing.data["password"] = base64.b64encode(password.encode()).decode()
        await asyncio.to_thread(
            core_api.replace_namespaced_secret,
            name=secret_name,
            namespace=namespace,
            body=existing,
        )
        return False

    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=secret_name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/name": "cnpg",
                "cnpg.io/cluster": cluster_name,
                "cnpg.io/role": role_name,
            },
        ),
        data={
            "username": base64.b64encode(role_name.encode()).decode(),
            "password": base64.b64encode(password.encode()).decode(),
        },
        type="kubernetes.io/basic-auth",
    )
    await asyncio.to_thread(
        core_api.create_namespaced_secret,
        namespace=namespace,
        body=secret,
    )
    return True


def format_cluster_status(cluster: Dict[str, Any], detail_level: str = "concise") -> str:
    """Format cluster status in a human-readable way."""
    metadata = cluster.get('metadata', {})
    spec = cluster.get('spec', {})
    status = cluster.get('status', {})
    
    name = metadata.get('name', 'unknown')
    namespace = metadata.get('namespace', 'unknown')
    instances = spec.get('instances', 0)
    
    phase = status.get('phase', 'Unknown')
    ready_instances = status.get('readyInstances', 0)
    current_primary = status.get('currentPrimary', 'unknown')
    
    result = f"**Cluster: {namespace}/{name}**\n"
    result += f"- Status: {phase}\n"
    result += f"- Instances: {ready_instances}/{instances} ready\n"
    result += f"- Current Primary: {current_primary}\n"
    
    if detail_level == "detailed":
        # Add more detailed information
        pg_version = spec.get('imageName', 'unknown')
        storage_size = spec.get('storage', {}).get('size', 'unknown')
        
        result += f"- PostgreSQL Version: {pg_version}\n"
        result += f"- Storage Size: {storage_size}\n"
        
        # Add conditions
        conditions = status.get('conditions', [])
        if conditions:
            result += "\n**Conditions:**\n"
            for condition in conditions:
                ctype = condition.get('type', 'Unknown')
                cstatus = condition.get('status', 'Unknown')
                reason = condition.get('reason', '')
                message = condition.get('message', '')
                result += f"- {ctype}: {cstatus}"
                if reason:
                    result += f" ({reason})"
                if message and detail_level == "detailed":
                    result += f"\n  {message}"
                result += "\n"
    
    return result


# ============================================================================
# Pydantic Models for Tool Inputs
# ============================================================================

class ListClustersInput(BaseModel):
    """Input for listing PostgreSQL clusters."""
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace to list clusters from. If not provided, uses the current namespace from your Kubernetes context."
    )
    detail_level: Literal["concise", "detailed"] = Field(
        "concise",
        description="Level of detail in the response. 'concise' for overview, 'detailed' for comprehensive information."
    )


class GetClusterStatusInput(BaseModel):
    """Input for getting cluster status."""
    name: str = Field(
        ...,
        description="Name of the CloudNativePG cluster.",
        examples=["my-postgres-cluster", "production-db"]
    )
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context.",
        examples=["default", "production", "postgres-system"]
    )
    detail_level: Literal["concise", "detailed"] = Field(
        "concise",
        description="Level of detail in the response."
    )


class CreateClusterInput(BaseModel):
    """Input for creating a new PostgreSQL cluster."""
    name: str = Field(
        ...,
        description="Name for the new cluster. Must conform to RFC 1123 DNS label standard: lowercase letters (a-z), numbers (0-9), and hyphens (-) only; must start and end with a letter or number; max 63 characters.",
        examples=["my-postgres-cluster", "production-db", "app-db-01"],
        pattern=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$',
        max_length=63
    )
    instances: int = Field(
        3,
        description="Number of PostgreSQL instances in the cluster (for high availability).",
        ge=1,
        le=10
    )
    storage_size: str = Field(
        "10Gi",
        description="Storage size for each instance (e.g., '10Gi', '100Gi').",
        examples=["10Gi", "50Gi", "100Gi"]
    )
    postgres_version: str = Field(
        "16",
        description="PostgreSQL major version to use.",
        examples=["14", "15", "16"]
    )
    container_image: Optional[str] = Field(
        None,
        description="Full PostgreSQL container image reference to use for spec.imageName. If specified, overrides postgres_version.",
        examples=["ghcr.io/cloudnative-pg/postgresql:16", "registry.example.com/postgres:16.4"]
    )
    storage_class: Optional[str] = Field(
        None,
        description="Kubernetes storage class to use. If not specified, uses the cluster default."
    )
    image_pull_policy: Optional[str] = Field(
        None,
        description="Image pull policy for the PostgreSQL container (Always, Never, IfNotPresent). If not specified, Kubernetes default applies.",
        examples=["Always", "Never", "IfNotPresent"]
    )
    node_selector: Optional[Dict[str, str]] = Field(
        None,
        description="Node labels used to constrain scheduling (spec.affinity.nodeSelector). Required for node-local storage: pin instances to the node(s) holding the local volumes and pair with a node-local storage_class.",
        examples=[{"kubernetes.io/hostname": "worker-1"}, {"disktype": "nvme"}]
    )
    tolerations: Optional[List[Dict[str, Any]]] = Field(
        None,
        description="Pod tolerations allowing scheduling onto tainted nodes (spec.affinity.tolerations). Dedicated local-storage nodes are often tainted; supply matching tolerations so pods are admitted. Each entry uses standard fields: key, operator, value, effect, tolerationSeconds.",
        examples=[[{"key": "storage", "operator": "Equal", "value": "local", "effect": "NoSchedule"}]]
    )
    wait: bool = Field(
        False,
        description="Wait for the cluster to become operational before returning. If False, returns immediately after creation. Automatically set to False if instances > 5."
    )
    timeout: Optional[int] = Field(
        None,
        description="Maximum time in seconds to wait for cluster to become operational (only used if wait=True). If not specified, defaults to 60 seconds per instance. Must be between 30 and 600 seconds.",
        ge=30,
        le=600
    )
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster will be created. If not specified, uses the current namespace from your Kubernetes context.",
        examples=["default", "production"]
    )
    dry_run: bool = Field(
        False,
        description="If True, returns the cluster definition without creating it. Useful for previewing the configuration before applying it."
    )


class ScaleClusterInput(BaseModel):
    """Input for scaling a cluster."""
    name: str = Field(..., description="Name of the cluster to scale.")
    instances: int = Field(
        ...,
        description="New number of instances.",
        ge=1,
        le=10
    )
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace of the cluster. If not specified, uses the current namespace from your Kubernetes context."
    )
    dry_run: bool = Field(
        False,
        description="If True, shows what would be changed without applying it. Useful for previewing the scaling operation."
    )


class DeleteClusterInput(BaseModel):
    """Input for deleting a cluster."""
    name: str = Field(
        ...,
        description="Name of the cluster to delete.",
        examples=["my-postgres-cluster", "old-test-cluster"]
    )
    confirm_deletion: bool = Field(
        False,
        description="Must be explicitly set to true to confirm deletion. This is a safety mechanism to prevent accidental deletion of clusters."
    )
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )
    dry_run: bool = Field(
        False,
        description="If True, shows what would be deleted without performing the deletion. Useful for previewing the deletion impact."
    )


class ListRolesInput(BaseModel):
    """Input for listing PostgreSQL roles."""
    cluster_name: str = Field(..., description="Name of the PostgreSQL cluster.")
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )


class GetRoleStatusInput(BaseModel):
    """Input for getting PostgreSQL role status."""
    cluster_name: str = Field(..., description="Name of the PostgreSQL cluster.")
    role_name: str = Field(..., description="Name of the role inside PostgreSQL.")
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )


class CreateRoleInput(BaseModel):
    """Input for creating a PostgreSQL role."""
    cluster_name: str = Field(..., description="Name of the PostgreSQL cluster.")
    role_name: str = Field(
        ...,
        description="Name of the role to create. Must conform to RFC 1123 DNS label standard (required for DatabaseRole CRD and Kubernetes secret naming): lowercase letters (a-z), numbers (0-9), and hyphens (-) only; must start and end with a letter or number; max 63 characters.",
        examples=["app-user", "readonly-user", "admin-01"],
        pattern=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$',
        max_length=63
    )
    login: bool = Field(True, description="Allow role to log in. Default: true.")
    superuser: bool = Field(False, description="Grant superuser privileges. Default: false.")
    inherit: bool = Field(True, description="Inherit privileges from roles it is a member of. Default: true.")
    createdb: bool = Field(False, description="Allow role to create databases. Default: false.")
    createrole: bool = Field(False, description="Allow role to create other roles. Default: false.")
    replication: bool = Field(False, description="Allow role to initiate streaming replication. Default: false.")
    bypassrls: bool = Field(False, description="Allow role to bypass row-level security policies. Default: false.")
    in_roles: Optional[List[str]] = Field(
        None,
        description="Existing roles this role is granted membership in, for example ['pg_read_all_data']."
    )
    connection_limit: Optional[int] = Field(
        None,
        description="Maximum concurrent connections for this role. -1 (the default) means no limit."
    )
    valid_until: Optional[str] = Field(
        None,
        description="RFC 3339 timestamp after which the role's password is no longer valid, for example '2026-12-31T23:59:59Z'. Omit for a password that never expires."
    )
    comment: Optional[str] = Field(None, description="Description attached to the role in PostgreSQL.")
    disable_password: bool = Field(
        False,
        description="If True, the role's password is set to NULL and no password Secret is generated. Default: false."
    )
    client_certificate: bool = Field(
        False,
        description="If True, the operator issues and renews a TLS client certificate for this role in Secret '<cluster>-<role>-client-cert'. Requires login=True. Default: false."
    )
    reclaim_policy: Literal["retain", "delete"] = Field(
        "retain",
        description="Policy for role deletion. 'retain' keeps the role in PostgreSQL when the DatabaseRole CRD is deleted, 'delete' drops it."
    )
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )
    dry_run: bool = Field(
        False,
        description="If True, shows the DatabaseRole CRD that would be created without creating it. Useful for previewing the configuration."
    )


class UpdateRoleInput(BaseModel):
    """Input for updating a PostgreSQL role."""
    cluster_name: str = Field(..., description="Name of the PostgreSQL cluster.")
    role_name: str = Field(..., description="Name of the role to update.")
    login: Optional[bool] = Field(None, description="Allow role to log in.")
    superuser: Optional[bool] = Field(None, description="Grant superuser privileges.")
    inherit: Optional[bool] = Field(None, description="Inherit privileges from roles it is a member of.")
    createdb: Optional[bool] = Field(None, description="Allow role to create databases.")
    createrole: Optional[bool] = Field(None, description="Allow role to create other roles.")
    replication: Optional[bool] = Field(None, description="Allow role to initiate streaming replication.")
    bypassrls: Optional[bool] = Field(None, description="Allow role to bypass row-level security policies.")
    in_roles: Optional[List[str]] = Field(
        None,
        description="Replacement list of roles this role is a member of. Membership changes are applied with GRANT/REVOKE."
    )
    connection_limit: Optional[int] = Field(None, description="Maximum concurrent connections for this role. -1 means no limit.")
    valid_until: Optional[str] = Field(None, description="RFC 3339 timestamp after which the role's password is no longer valid.")
    comment: Optional[str] = Field(None, description="Description attached to the role in PostgreSQL.")
    disable_password: Optional[bool] = Field(None, description="Set the role's password to NULL in PostgreSQL.")
    client_certificate: Optional[bool] = Field(None, description="Issue and renew a TLS client certificate for this role. Requires login.")
    reclaim_policy: Optional[Literal["retain", "delete"]] = Field(
        None,
        description="Policy for role deletion. 'retain' keeps the role in PostgreSQL when the DatabaseRole CRD is deleted, 'delete' drops it."
    )
    password: Optional[str] = Field(None, description="New password for the role. If not specified, password remains unchanged.")
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )
    dry_run: bool = Field(
        False,
        description="If True, shows what changes would be made without applying them. Useful for previewing the update."
    )


class DeleteRoleInput(BaseModel):
    """Input for deleting a PostgreSQL role."""
    cluster_name: str = Field(..., description="Name of the PostgreSQL cluster.")
    role_name: str = Field(..., description="Name of the role to delete.")
    drop_role: bool = Field(
        False,
        description="If True, forces the DatabaseRole reclaim policy to 'delete' before removing the CRD, so the role is dropped from PostgreSQL. If False, the reclaim policy configured on the role decides."
    )
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )
    dry_run: bool = Field(
        False,
        description="If True, shows what would be deleted without performing the deletion. Useful for previewing the deletion impact."
    )


class ListDatabasesInput(BaseModel):
    """Input for listing PostgreSQL databases."""
    cluster_name: str = Field(..., description="Name of the PostgreSQL cluster.")
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )


class GetDatabaseStatusInput(BaseModel):
    """Input for getting PostgreSQL database status."""
    cluster_name: str = Field(..., description="Name of the PostgreSQL cluster.")
    database_name: str = Field(..., description="Name of the database inside PostgreSQL.")
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )


class CreateDatabaseInput(BaseModel):
    """Input for creating a PostgreSQL database."""
    cluster_name: str = Field(..., description="Name of the PostgreSQL cluster.")
    database_name: str = Field(
        ...,
        description="Name of the database to create. Must conform to RFC 1123 DNS label standard (required for Database CRD naming): lowercase letters (a-z), numbers (0-9), and hyphens (-) only; must start and end with a letter or number; max 63 characters.",
        examples=["app-db", "analytics-db", "user-data"],
        pattern=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$',
        max_length=63
    )
    owner: str = Field(..., description="Name of the role that will own the database.")
    reclaim_policy: Literal["retain", "delete"] = Field(
        "retain",
        description="Policy for database deletion. 'retain' keeps the database when the CRD is deleted, 'delete' removes it."
    )
    encoding: Optional[str] = Field(
        None,
        description="Optional PostgreSQL CREATE DATABASE ENCODING value, for example UTF8. Immutable after creation."
    )
    locale: Optional[str] = Field(
        None,
        description="Optional PostgreSQL CREATE DATABASE LOCALE value. Sets default collation order and character classification. Immutable after creation."
    )
    locale_provider: Optional[Literal["builtin", "icu", "libc"]] = Field(
        None,
        description="Optional PostgreSQL CREATE DATABASE LOCALE_PROVIDER value. PostgreSQL 16+ supports this for databases."
    )
    locale_collate: Optional[str] = Field(
        None,
        description="Optional PostgreSQL CREATE DATABASE LC_COLLATE value. Immutable after creation."
    )
    locale_ctype: Optional[str] = Field(
        None,
        description="Optional PostgreSQL CREATE DATABASE LC_CTYPE value. Immutable after creation."
    )
    icu_locale: Optional[str] = Field(
        None,
        description="Optional PostgreSQL CREATE DATABASE ICU_LOCALE value. Requires locale_provider='icu'."
    )
    icu_rules: Optional[str] = Field(
        None,
        description="Optional PostgreSQL CREATE DATABASE ICU_RULES value. Requires locale_provider='icu'."
    )
    builtin_locale: Optional[str] = Field(
        None,
        description="Optional PostgreSQL CREATE DATABASE BUILTIN_LOCALE value. Requires locale_provider='builtin'."
    )
    collation_version: Optional[str] = Field(
        None,
        description="Optional PostgreSQL CREATE DATABASE COLLATION_VERSION value. Immutable after creation."
    )
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )
    dry_run: bool = Field(
        False,
        description="If True, shows the Database CRD definition that would be created without creating it. Useful for previewing the configuration."
    )


class DeleteDatabaseInput(BaseModel):
    """Input for deleting a PostgreSQL database."""
    cluster_name: str = Field(..., description="Name of the PostgreSQL cluster.")
    database_name: str = Field(..., description="Name of the database to delete.")
    namespace: Optional[str] = Field(
        None,
        description="Kubernetes namespace where the cluster exists. If not specified, uses the current namespace from your Kubernetes context."
    )
    dry_run: bool = Field(
        False,
        description="If True, shows what would be deleted without performing the deletion. Useful for previewing the deletion impact."
    )


# ============================================================================
# MCP Tools - Implementation Functions
# ============================================================================


# ============================================================================
# MCP Tool Implementations
# These functions are imported by both server files and decorated there
# ============================================================================

@with_mcp_context
async def list_postgres_clusters(
    context: MCPContext,
    namespace: Optional[str] = None,
    detail_level: Literal["concise", "detailed"] = "concise",
    format: Literal["text", "json"] = "text"
) -> str:
    """
    List all PostgreSQL clusters managed by CloudNativePG.

    This tool retrieves information about PostgreSQL clusters in the Kubernetes cluster.
    Use this to discover available clusters, check their health status, and understand
    the current state of your PostgreSQL infrastructure.

    Args:
        namespace: Kubernetes namespace to list clusters from. If not provided, uses
                  the current namespace from your Kubernetes context. To list clusters
                  in a different namespace, specify it explicitly.
        detail_level: Level of detail in the response. Use 'concise' for a quick
                     overview or 'detailed' for comprehensive information including
                     conditions, resources, and configurations.
        format: Output format. 'text' for human-readable (default), 'json' for structured
               data that can be programmatically consumed.

    Returns:
        A formatted string containing cluster information. Returns human-readable
        status information for each cluster including name, namespace, health status,
        number of ready instances, and current primary pod. If format='json', returns
        a JSON string with structured data.

    Examples:
        - List clusters in current namespace: list_postgres_clusters()
        - List clusters in a specific namespace: list_postgres_clusters(namespace="production")
        - Get detailed information: list_postgres_clusters(detail_level="detailed")
        - Get JSON output: list_postgres_clusters(format="json")

    Error Handling:
        - If RBAC permissions are insufficient, ensure you have 'get' and 'list'
          permissions for postgresql.cnpg.io/clusters resources in the namespace.
        - If no clusters are found, returns a message indicating empty results.
    """
    try:
        clusters = await list_cnpg_clusters(namespace)

        if not clusters:
            scope = f"in namespace '{namespace}'" if namespace else "cluster-wide"
            if format == "json":
                return json.dumps({"clusters": [], "count": 0, "scope": scope})
            return f"No PostgreSQL clusters found {scope}."

        if format == "json":
            # Return structured JSON
            cluster_list = []
            for cluster in clusters:
                metadata = cluster.get('metadata', {})
                spec = cluster.get('spec', {})
                status = cluster.get('status', {})

                cluster_data = {
                    "name": metadata.get('name', 'unknown'),
                    "namespace": metadata.get('namespace', 'unknown'),
                    "instances": spec.get('instances', 0),
                    "ready_instances": status.get('readyInstances', 0),
                    "phase": status.get('phase', 'Unknown'),
                    "current_primary": status.get('currentPrimary', 'unknown')
                }

                if detail_level == "detailed":
                    cluster_data.update({
                        "postgres_version": spec.get('imageName', 'unknown'),
                        "storage_size": spec.get('storage', {}).get('size', 'unknown'),
                        "conditions": status.get('conditions', [])
                    })

                cluster_list.append(cluster_data)

            return json.dumps({
                "clusters": cluster_list,
                "count": len(cluster_list),
                "scope": f"namespace '{namespace}'" if namespace else "all namespaces"
            }, indent=2)

        # Default: human-readable text
        result = f"Found {len(clusters)} PostgreSQL cluster(s):\n\n"

        for cluster in clusters:
            result += format_cluster_status(cluster, detail_level) + "\n"

        return truncate_response(result)

    except Exception as e:
        return format_error_message(e, "listing PostgreSQL clusters")



@with_mcp_context
async def get_cluster_status(
    context: MCPContext,
    name: str,
    namespace: Optional[str] = None,
    detail_level: Literal["concise", "detailed"] = "concise",
    format: Literal["text", "json"] = "text"
) -> str:
    """
    Get detailed status information for a specific PostgreSQL cluster.

    This tool retrieves comprehensive information about a CloudNativePG cluster,
    including its current state, health conditions, replica status, and configuration.
    Use this to troubleshoot issues, verify cluster health, or get detailed insights
    into a specific cluster's operation.

    Args:
        name: Name of the CloudNativePG cluster resource.
        namespace: Kubernetes namespace where the cluster exists. If not specified,
                  uses the current namespace from your Kubernetes context. Cluster
                  names are only unique within a namespace.
        detail_level: Level of detail. 'concise' provides essential status information,
                     'detailed' includes conditions, events, resource usage, and full
                     configuration.
        format: Output format. 'text' for human-readable (default), 'json' for structured
               data that can be programmatically consumed.

    Returns:
        Formatted string with cluster status information including phase, ready instances,
        primary pod, PostgreSQL version, storage configuration, and detailed conditions
        if requested. If format='json', returns a JSON string with structured data.

    Examples:
        - get_cluster_status(name="main-db")  # Uses current context namespace
        - get_cluster_status(name="main-db", namespace="production")
        - get_cluster_status(name="test-db", detail_level="detailed")
        - get_cluster_status(name="main-db", format="json")

    Error Handling:
        - Returns 404 if cluster doesn't exist: Double-check the namespace and name.
        - Returns 403 if permissions are insufficient: Verify RBAC permissions for the
          postgresql.cnpg.io/clusters resource.
    """
    try:
        # Infer namespace from context if not provided
        if namespace is None:
            namespace = get_current_namespace()

        cluster = await get_cnpg_cluster(namespace, name)

        if format == "json":
            # Return structured JSON
            metadata = cluster.get('metadata', {})
            spec = cluster.get('spec', {})
            status = cluster.get('status', {})

            cluster_data = {
                "name": metadata.get('name', 'unknown'),
                "namespace": metadata.get('namespace', 'unknown'),
                "instances": spec.get('instances', 0),
                "ready_instances": status.get('readyInstances', 0),
                "phase": status.get('phase', 'Unknown'),
                "current_primary": status.get('currentPrimary', 'unknown'),
                "postgres_version": spec.get('imageName', 'unknown'),
                "storage_size": spec.get('storage', {}).get('size', 'unknown')
            }

            if detail_level == "detailed":
                cluster_data.update({
                    "storage_class": spec.get('storage', {}).get('storageClass'),
                    "conditions": status.get('conditions', []),
                    "postgresql_parameters": spec.get('postgresql', {}).get('parameters', {}),
                    # Roles are managed through DatabaseRole CRDs; this reports only
                    # leftovers in the deprecated inline Cluster field.
                    "legacy_managed_roles": spec.get('managed', {}).get('roles', [])
                })

            return json.dumps(cluster_data, indent=2)

        # Default: human-readable text
        result = format_cluster_status(cluster, detail_level)
        return truncate_response(result)

    except Exception as e:
        return format_error_message(e, f"getting cluster status for {namespace}/{name}")



@with_mcp_context
async def create_postgres_cluster(
    context: MCPContext,
    name: str,
    instances: int = 3,
    storage_size: str = "10Gi",
    postgres_version: str = "16",
    container_image: Optional[str] = None,
    storage_class: Optional[str] = None,
    image_pull_policy: Optional[str] = None,
    node_selector: Optional[Dict[str, str]] = None,
    tolerations: Optional[List[Dict[str, Any]]] = None,
    wait: bool = False,
    timeout: Optional[int] = None,
    namespace: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Create a new PostgreSQL cluster with CloudNativePG.

    This tool creates a new high-availability PostgreSQL cluster with the specified
    configuration. The cluster will automatically set up replication, backups, and
    monitoring. This is a comprehensive workflow tool that handles the entire cluster
    creation process.

    Args:
        name: Name for the new cluster. Must be a valid Kubernetes resource name
              (lowercase alphanumeric characters or '-', starting and ending with
              alphanumeric character).
        instances: Number of PostgreSQL instances. Use 1 for development, 3+ for
                  production high availability. Default is 3.
        storage_size: Storage size per instance using Kubernetes quantity format
                     (e.g., '10Gi', '100Gi', '1Ti'). Consider your data size and
                     growth projections.
        postgres_version: PostgreSQL major version (e.g., '14', '15', '16').
                         CloudNativePG will use the latest minor version available.
        container_image: Full PostgreSQL container image reference to use for
                        spec.imageName (e.g., 'ghcr.io/cloudnative-pg/postgresql:16').
                        If specified, overrides postgres_version.
        storage_class: Kubernetes storage class for persistent volumes. If not specified,
                      uses the cluster's default storage class. Use fast storage (SSD)
                      for production databases.
        image_pull_policy: Image pull policy for the PostgreSQL container image. Accepts
                          standard Kubernetes values: 'Always', 'Never', or 'IfNotPresent'.
                          If not specified, Kubernetes default behavior applies (IfNotPresent
                          for tagged images, Always for :latest).
        node_selector: Node labels (as a key/value map) used to constrain which nodes the
                      cluster pods can be scheduled onto. Maps to spec.affinity.nodeSelector.
                      Essential when using node-local storage: pin instances to the node(s)
                      that physically hold the local volumes, e.g.
                      {"kubernetes.io/hostname": "worker-1"} to pin to a specific node, or
                      {"disktype": "nvme"} to target a pool of nodes with local SSDs. Pair
                      this with a node-local storage_class (e.g. a topology-aware local
                      volume provisioner) so the PersistentVolumes are provisioned on the
                      same nodes the pods land on.
        tolerations: Pod tolerations (as a list of Kubernetes Toleration objects) allowing
                    the cluster pods to schedule onto tainted nodes. Maps to
                    spec.affinity.tolerations. Dedicated local-storage nodes are commonly
                    tainted; supply matching tolerations so the pods are actually admitted,
                    e.g. [{"key": "storage", "operator": "Equal", "value": "local",
                    "effect": "NoSchedule"}]. Each entry accepts the standard toleration
                    fields: key, operator ('Equal' or 'Exists'), value, effect
                    ('NoSchedule', 'PreferNoSchedule', 'NoExecute'), and tolerationSeconds.
        wait: If True, wait for the cluster to become operational before returning.
              If False (default), return immediately after creation. Automatically
              set to False if instances > 5 (to avoid waiting more than 5 minutes).
        timeout: Maximum time in seconds to wait for cluster to become operational
                (only used if wait=True). If not specified, defaults to 60 seconds
                per instance. Range: 30-600 seconds (0.5-10 minutes).
        namespace: Kubernetes namespace where the cluster will be created. If not specified,
                  uses the current namespace from your Kubernetes context. The namespace
                  must exist before creating the cluster.
        dry_run: If True, returns the cluster definition that would be created without
                actually creating it. Useful for previewing the configuration before
                applying it. Default is False.

    Returns:
        Success message with cluster details if creation succeeds, or detailed error
        message with suggestions if it fails. If wait=True, includes final cluster status.
        If dry_run=True, returns the YAML cluster definition that would be created.

    Examples:
        - Simple cluster: create_postgres_cluster(name="my-db")
        - Wait for ready (auto-timeout 3min for 3 instances): create_postgres_cluster(name="my-db", wait=True)
        - With custom timeout: create_postgres_cluster(name="my-db", wait=True, timeout=300)
        - Large cluster (wait auto-disabled): create_postgres_cluster(name="big-db", instances=8, wait=True)
        - Custom image: create_postgres_cluster(
            name="custom-db",
            container_image="registry.example.com/postgresql:16.4"
          )
        - Production cluster: create_postgres_cluster(
            name="main-db",
            instances=5,
            storage_size="100Gi",
            postgres_version="16",
            storage_class="fast-ssd",
            wait=True,
            namespace="production"
          )
        - Node-local storage pinned to a node: create_postgres_cluster(
            name="local-db",
            instances=1,
            storage_size="500Gi",
            storage_class="local-storage",
            node_selector={"kubernetes.io/hostname": "worker-1"}
          )
        - Node-local storage on a dedicated (tainted) storage pool: create_postgres_cluster(
            name="local-ha-db",
            instances=3,
            storage_size="500Gi",
            storage_class="local-storage",
            node_selector={"disktype": "nvme"},
            tolerations=[{"key": "storage", "operator": "Equal",
                          "value": "local", "effect": "NoSchedule"}]
          )

    Error Handling:
        - 409 Conflict: Cluster with this name already exists. Choose a different name
          or delete the existing cluster first.
        - 422 Invalid: Check that all parameters meet CloudNativePG requirements.
        - 403 Forbidden: Ensure service account has 'create' permission for
          postgresql.cnpg.io/clusters.
        - Timeout: If wait=True and cluster doesn't become ready within timeout period.

    Note:
        Cluster creation is asynchronous. If wait=False, use get_cluster_status() to
        monitor the cluster until it reaches 'Cluster in healthy state' phase.
    """
    try:
        # Validate cluster name conforms to RFC 1123
        validate_rfc1123_name(name, "Cluster")

        # Infer namespace from context if not provided
        if namespace is None:
            namespace = get_current_namespace()

        cluster_image = container_image or f"ghcr.io/cloudnative-pg/postgresql:{postgres_version}"

        # Auto-disable wait for large clusters (> 5 instances)
        # Waiting more than 5 minutes is too long
        original_wait = wait
        if instances > 5:
            wait = False

        # Calculate dynamic timeout based on instances if not provided
        # Default: 60 seconds per instance
        if timeout is None:
            timeout = instances * 60
        # Clamp timeout to valid range (30-600 seconds)
        timeout = max(30, min(600, timeout))

        # Build the cluster specification
        cluster_spec = {
            "apiVersion": f"{CNPG_GROUP}/{CNPG_VERSION}",
            "kind": "Cluster",
            "metadata": {
                "name": name,
                "namespace": namespace
            },
            "spec": {
                "instances": instances,
                "imageName": cluster_image,
                "storage": {
                    "size": storage_size
                },
                "postgresql": {
                    "parameters": {
                        "max_connections": "100",
                        "shared_buffers": "256MB"
                    }
                }
            }
        }
        
        # Add storage class if specified
        if storage_class:
            cluster_spec["spec"]["storage"]["storageClass"] = storage_class

        # Add image pull policy if specified
        if image_pull_policy:
            cluster_spec["spec"]["imagePullPolicy"] = image_pull_policy

        # Add pod scheduling constraints if specified. CNPG exposes these under
        # spec.affinity: nodeSelector pins pods to nodes matching the given labels
        # (required to co-locate pods with node-local storage), and tolerations allow
        # those pods onto tainted (e.g. dedicated storage) nodes.
        affinity: Dict[str, Any] = {}
        if node_selector:
            affinity["nodeSelector"] = node_selector
        if tolerations:
            affinity["tolerations"] = tolerations
        if affinity:
            cluster_spec["spec"]["affinity"] = affinity

        # Pre-rendered display lines for optional settings (used in status messages).
        storage_class_line = f"- Storage Class: {storage_class}\n" if storage_class else ""
        pull_policy_line = f"- Image Pull Policy: {image_pull_policy}\n" if image_pull_policy else ""
        node_selector_line = (
            f"- Node Selector: {', '.join(f'{k}={v}' for k, v in node_selector.items())}\n"
            if node_selector else ""
        )
        tolerations_line = (
            f"- Tolerations: {len(tolerations)} configured\n" if tolerations else ""
        )

        # If dry_run, return the cluster definition without creating
        if dry_run:
            cluster_yaml = yaml.dump(cluster_spec, default_flow_style=False, sort_keys=False)
            return f"""Dry run: PostgreSQL cluster definition for '{name}' in namespace '{namespace}'

This is the cluster definition that would be created:

```yaml
{cluster_yaml}```

To create this cluster, call create_postgres_cluster again with dry_run=False (or omit the dry_run parameter).
"""

        # Create the cluster
        custom_api, _ = get_kubernetes_clients()
        result = await asyncio.to_thread(
            custom_api.create_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_PLURAL,
            body=cluster_spec
        )
        
        cluster_name = result['metadata']['name']

        # If wait is False, return immediately
        if not wait:
            auto_disabled_msg = ""
            if original_wait and instances > 5:
                auto_disabled_msg = f"\nNote: Wait was automatically disabled because {instances} instances would require waiting up to {instances * 60} seconds (more than 5 minutes).\n"

            return f"""Successfully created PostgreSQL cluster '{cluster_name}' in namespace '{namespace}'.

Configuration:
- Instances: {instances}
- Container Image: {cluster_image}
- Storage Size: {storage_size}
{storage_class_line}{pull_policy_line}{node_selector_line}{tolerations_line}{auto_disabled_msg}
The cluster is now being provisioned. You can monitor its status using:
get_cluster_status(namespace="{namespace}", name="{cluster_name}")

Wait until the cluster reaches 'Cluster in healthy state' phase before connecting.
"""

        # Wait for cluster to become operational
        import time
        start_time = time.time()
        poll_interval = 5  # Check every 5 seconds

        while True:
            elapsed = time.time() - start_time

            # Check timeout
            if elapsed >= timeout:
                return f"""Cluster '{cluster_name}' created but TIMED OUT waiting for it to become operational.

Configuration:
- Instances: {instances}
- Container Image: {cluster_image}
- Storage Size: {storage_size}
{storage_class_line}{pull_policy_line}{node_selector_line}{tolerations_line}
Timeout: {timeout} seconds elapsed

The cluster is still provisioning. Check status with:
get_cluster_status(namespace="{namespace}", name="{cluster_name}")

Note: Cluster creation can take several minutes depending on storage provisioning
and PostgreSQL initialization time.
"""

            # Get current cluster status
            try:
                cluster = await get_cnpg_cluster(namespace, cluster_name)
                status = cluster.get('status', {})
                phase = status.get('phase', '')
                ready_instances = status.get('readyInstances', 0)

                # Check if cluster is healthy
                if 'healthy' in phase.lower() and ready_instances == instances:
                    current_primary = status.get('currentPrimary', 'unknown')
                    return f"""Successfully created PostgreSQL cluster '{cluster_name}' in namespace '{namespace}'.

Configuration:
- Instances: {instances} ({ready_instances} ready)
- Container Image: {cluster_image}
- Storage Size: {storage_size}
{storage_class_line}{pull_policy_line}{node_selector_line}{tolerations_line}- Current Primary: {current_primary}

Status: {phase}

Cluster is operational and ready for connections!

Time elapsed: {int(elapsed)} seconds

Get connection details with:
kubectl get secret {cluster_name}-app -n {namespace} -o jsonpath='{{.data.password}}' | base64 -d
"""

            except Exception:
                # Cluster might not be fully created yet, continue waiting
                pass

            # Wait before next check
            await asyncio.sleep(poll_interval)

    except Exception as e:
        return format_error_message(e, f"creating cluster {namespace}/{name}")



@with_mcp_context
async def scale_postgres_cluster(
    context: MCPContext,
    name: str,
    instances: int,
    namespace: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Scale a PostgreSQL cluster by changing the number of instances.

    This tool modifies the number of PostgreSQL instances in a cluster, allowing you
    to scale up for increased capacity or scale down to reduce resource usage.
    CloudNativePG handles the scaling process safely, ensuring data consistency.

    Args:
        name: Name of the cluster to scale.
        instances: New number of instances (1-10). For high availability, use 3 or more.
        namespace: Kubernetes namespace where the cluster exists. If not specified,
                  uses the current namespace from your Kubernetes context.
        dry_run: If True, shows what would be changed without applying it. Useful for
                previewing the scaling operation. Default is False.

    Returns:
        Success message if the scaling operation is initiated, or error details if it fails.
        If dry_run=True, returns a preview of the changes that would be made.

    Examples:
        - Scale up: scale_postgres_cluster(name="main-db", instances=5)
        - Scale with namespace: scale_postgres_cluster(name="main-db", instances=5, namespace="production")
        - Scale down: scale_postgres_cluster(name="test-db", instances=1)
        - Preview scaling: scale_postgres_cluster(name="main-db", instances=5, dry_run=True)

    Error Handling:
        - 404: Cluster not found. Verify namespace and name.
        - 422: Invalid instance count. Must be between 1 and 10.
        - Scaling is performed as a rolling update. Monitor with get_cluster_status().

    Note:
        Scaling is asynchronous. The cluster will gradually adjust to the new size.
        Use get_cluster_status() to monitor progress.
    """
    try:
        # Infer namespace from context if not provided
        if namespace is None:
            namespace = get_current_namespace()

        # Get current cluster
        cluster = await get_cnpg_cluster(namespace, name)
        current_instances = cluster['spec']['instances']

        # If dry_run, return preview of changes
        if dry_run:
            return f"""Dry run: Scaling operation for cluster '{namespace}/{name}'

Current configuration:
- Instances: {current_instances}

Proposed changes:
- Instances: {current_instances} -> {instances}

Impact:
- {abs(instances - current_instances)} instance(s) will be {'added' if instances > current_instances else 'removed'}
- Scaling {'up' if instances > current_instances else 'down'} from {current_instances} to {instances}

To apply this change, call scale_postgres_cluster again with dry_run=False (or omit the dry_run parameter).
"""

        # Apply a focused merge patch so operator reconciliation updates do
        # not make this request conflict with a stale resourceVersion.
        await patch_cnpg_cluster_spec(namespace, name, {"instances": instances})

        return f"""Successfully initiated scaling of cluster '{namespace}/{name}' to {instances} instance(s).

The cluster will perform a rolling update to reach the desired instance count.
Monitor the scaling progress with:
get_cluster_status(namespace="{namespace}", name="{name}")
"""

    except Exception as e:
        return format_error_message(e, f"scaling cluster {namespace}/{name}")



@with_mcp_context
async def delete_postgres_cluster(
    context: MCPContext,
    name: str,
    confirm_deletion: bool = False,
    namespace: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Delete a PostgreSQL cluster and its associated resources.

    This tool permanently deletes a CloudNativePG cluster. This is a destructive
    operation that cannot be undone. All data will be lost unless you have backups.
    Use with caution, especially in production environments.

    Automatically cleans up:
    - The cluster resource itself
    - All associated role password secrets (labeled with cnpg.io/cluster={name})

    Args:
        name: Name of the cluster to delete.
        confirm_deletion: Must be explicitly set to True to confirm deletion.
                         This is a required safety mechanism to prevent accidental deletions.
        namespace: Kubernetes namespace where the cluster exists. If not specified,
                  uses the current namespace from your Kubernetes context.
        dry_run: If True, shows what would be deleted without performing the deletion.
                Useful for previewing the deletion impact. Default is False.

    Returns:
        Success message if deletion is initiated (including count of secrets cleaned up),
        warning message if not confirmed, or error details if it fails.
        If dry_run=True, returns a preview of what would be deleted.

    Examples:
        - Request deletion (shows warning): delete_postgres_cluster(name="old-test-cluster")
        - Confirm deletion: delete_postgres_cluster(name="old-test-cluster", confirm_deletion=True)
        - Preview deletion: delete_postgres_cluster(name="old-test-cluster", dry_run=True)

    Error Handling:
        - 404: Cluster not found. Verify namespace and name.
        - 403: Permission denied. Ensure service account has 'delete' permission.

    Warning:
        This operation is DESTRUCTIVE and IRREVERSIBLE. All data in the cluster
        will be permanently lost. Make sure you have backups before deleting
        production clusters. The persistent volumes may be retained or deleted
        depending on the storage class reclaim policy.
    """
    try:
        # Infer namespace from context if not provided
        if namespace is None:
            namespace = get_current_namespace()

        # Verify cluster exists
        cluster = await get_cnpg_cluster(namespace, name)

        # If dry_run, show what would be deleted
        if dry_run:
            # Count associated secrets
            _, core_api = get_kubernetes_clients()
            label_selector = f"cnpg.io/cluster={name}"
            secrets = await asyncio.to_thread(
                core_api.list_namespaced_secret,
                namespace=namespace,
                label_selector=label_selector
            )
            secret_count = len(secrets.items)
            secret_names = [s.metadata.name for s in secrets.items]

            spec = cluster.get('spec', {})
            instances = spec.get('instances', 0)
            storage_size = spec.get('storage', {}).get('size', 'unknown')

            return f"""Dry run: Deletion preview for cluster '{namespace}/{name}'

Cluster details:
- Instances: {instances}
- Storage size per instance: {storage_size}
- Total storage: {instances}x {storage_size}

Resources that would be deleted:
- Cluster CRD: {name}
- Associated secrets: {secret_count} secret(s)
  {chr(10).join(['  - ' + s for s in secret_names]) if secret_names else '  (none)'}

WARNING: This operation would be DESTRUCTIVE and IRREVERSIBLE:
- All data in this cluster would be PERMANENTLY LOST
- All databases, tables, and data would be deleted
- Depending on storage class policy, persistent volumes may be deleted

To proceed with deletion, call delete_postgres_cluster with confirm_deletion=True and dry_run=False (or omit dry_run).
"""

        # Check if deletion is confirmed
        if not confirm_deletion:
            return f"""WARNING: DELETION NOT CONFIRMED

You are about to delete the PostgreSQL cluster '{namespace}/{name}'.

WARNING: This is a DESTRUCTIVE and IRREVERSIBLE operation:
- All data in this cluster will be PERMANENTLY LOST
- All databases, tables, and data will be deleted
- Depending on storage class policy, persistent volumes may be deleted
- This action CANNOT be undone

Before proceeding, ensure you have:
- Backed up all important data
- Verified this is the correct cluster to delete
- Confirmed with your team (if applicable)

To proceed with deletion, call this tool again with confirm_deletion=True:

delete_postgres_cluster(
    name="{name}",
    namespace="{namespace}",
    confirm_deletion=True
)

To cancel, simply do not call the tool again.
"""

        # Delete the cluster
        custom_api, core_api = get_kubernetes_clients()
        await asyncio.to_thread(
            custom_api.delete_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_PLURAL,
            name=name
        )

        # Clean up associated role secrets
        secrets_deleted = 0
        try:
            # Find all secrets for this cluster using label selector
            label_selector = f"cnpg.io/cluster={name}"
            secrets = await asyncio.to_thread(
                core_api.list_namespaced_secret,
                namespace=namespace,
                label_selector=label_selector
            )

            # Delete each secret
            for secret in secrets.items:
                try:
                    await asyncio.to_thread(
                        core_api.delete_namespaced_secret,
                        name=secret.metadata.name,
                        namespace=namespace
                    )
                    secrets_deleted += 1
                except Exception:
                    # Continue even if a secret fails to delete
                    pass
        except Exception:
            # If secret cleanup fails, don't fail the whole operation
            pass

        secrets_msg = ""
        if secrets_deleted > 0:
            secrets_msg = f"\n\nCleaned up {secrets_deleted} associated role secret(s)."

        return f"""Successfully initiated deletion of cluster '{namespace}/{name}'.{secrets_msg}

WARNING: This is a destructive operation. All data in this cluster will be permanently lost.

The cluster and its pods are being terminated. Depending on your storage class
reclaim policy, the persistent volumes may be:
- Retained: PVCs remain and can be manually deleted later
- Deleted: PVCs are automatically deleted (data loss is permanent)

Check deletion progress with:
kubectl get cluster {name} -n {namespace}

The cluster will no longer appear in list_postgres_clusters() once deletion is complete.
"""

    except Exception as e:
        return format_error_message(e, f"deleting cluster {namespace}/{name}")



@with_mcp_context
async def list_postgres_roles(
    context: MCPContext,
    cluster_name: str,
    namespace: Optional[str] = None,
    format: Literal["text", "json"] = "text"
) -> str:
    """
    List all PostgreSQL roles/users managed for a cluster by DatabaseRole CRDs.

    Roles defined through the deprecated Cluster .spec.managed.roles field are
    reported separately so pre-existing roles stay visible.

    Args:
        cluster_name: Name of the PostgreSQL cluster.
        namespace: Kubernetes namespace where the cluster exists.
        format: Output format. 'text' for human-readable (default), 'json' for structured
               data that can be programmatically consumed.

    Returns:
        Formatted list of roles with their attributes. If format='json', returns a JSON
        string with structured data.
    """
    try:
        if namespace is None:
            namespace = get_current_namespace()

        # Verify the cluster exists and pick up any legacy inline role definitions
        cluster = await get_cnpg_cluster(namespace, cluster_name)
        legacy_roles = cluster.get('spec', {}).get('managed', {}).get('roles', [])

        database_roles = await list_cnpg_database_roles(namespace, cluster_name)

        if format == "json":
            role_list = []
            for role in database_roles:
                metadata = role.get('metadata', {})
                spec = role.get('spec', {})
                role_data = {
                    "crd_name": metadata.get('name', 'unknown'),
                    "name": spec.get('name', 'unknown'),
                    "ensure": spec.get('ensure', 'present'),
                    "reclaim_policy": spec.get('databaseRoleReclaimPolicy', 'retain'),
                    "applied": role.get('status', {}).get('applied'),
                }
                role_data.update(database_role_attributes_dict(spec))
                role_list.append(role_data)

            return json.dumps({
                "cluster": f"{namespace}/{cluster_name}",
                "roles": role_list,
                "count": len(role_list),
                "legacy_managed_roles": [r.get('name', 'unknown') for r in legacy_roles],
            }, indent=2)

        if not database_roles and not legacy_roles:
            return (
                f"No roles defined for cluster '{namespace}/{cluster_name}'.\n\n"
                "Roles are managed through DatabaseRole CRDs. Create one with create_postgres_role."
            )

        result = f"PostgreSQL Roles for cluster '{namespace}/{cluster_name}':\n\n"

        for role in database_roles:
            metadata = role.get('metadata', {})
            spec = role.get('spec', {})
            status = role.get('status', {})

            result += f"**{spec.get('name', 'unknown')}**\n"
            result += f"  - DatabaseRole CRD: {metadata.get('name', 'unknown')}\n"
            result += f"  - Ensure: {spec.get('ensure', 'present')}\n"
            result += f"  - Reclaim Policy: {spec.get('databaseRoleReclaimPolicy', 'retain')}\n"
            result += f"  - Applied: {status.get('applied', 'not reported yet')}\n"
            result += "".join(f"  {line}\n" for line in format_database_role_attributes(spec).splitlines())
            result += "\n"

        if not database_roles:
            result += "No DatabaseRole CRDs found.\n\n"

        if legacy_roles:
            result += (
                "Legacy roles in the deprecated Cluster .spec.managed.roles field "
                "(not managed by this server):\n"
            )
            for role in legacy_roles:
                result += f"  - {role.get('name', 'unknown')} (ensure: {role.get('ensure', 'present')})\n"
            result += "\n"

        return result

    except Exception as e:
        return format_error_message(e, f"listing roles in cluster {namespace}/{cluster_name}")



@with_mcp_context
async def get_postgres_role_status(
    context: MCPContext,
    cluster_name: str,
    role_name: str,
    namespace: Optional[str] = None,
    format: Literal["text", "json"] = "text"
) -> str:
    """
    Get the current DatabaseRole CRD spec and operator reconciliation status.

    Args:
        cluster_name: Name of the PostgreSQL cluster.
        role_name: Name of the role inside PostgreSQL.
        namespace: Kubernetes namespace where the cluster exists.
        format: Output format. 'text' for human-readable (default), 'json' for structured
               data that can be programmatically consumed.

    Returns:
        DatabaseRole CRD metadata, current attribute values, and operator
        reconciliation status.
    """
    try:
        if namespace is None:
            namespace = get_current_namespace()

        role = await get_cnpg_database_role(namespace, cluster_name, role_name)
        metadata = role.get("metadata", {})
        spec = role.get("spec", {})
        status = role.get("status", {})

        if format == "json":
            return json.dumps({
                "cluster": f"{namespace}/{cluster_name}",
                "crd_name": metadata.get("name", "unknown"),
                "role_name": spec.get("name", role_name),
                "ensure": spec.get("ensure", "present"),
                "reclaim_policy": spec.get("databaseRoleReclaimPolicy", "retain"),
                "generation": metadata.get("generation"),
                "resource_version": metadata.get("resourceVersion"),
                "attributes": database_role_attributes_dict(spec),
                "status": status,
            }, indent=2)

        result = f"**Role: {namespace}/{metadata.get('name', 'unknown')}**\n"
        result += f"- PostgreSQL Role Name: {spec.get('name', role_name)}\n"
        result += f"- Cluster: {namespace}/{cluster_name}\n"
        result += f"- Ensure: {spec.get('ensure', 'present')}\n"
        result += f"- Reclaim Policy: {spec.get('databaseRoleReclaimPolicy', 'retain')}\n"
        result += f"- Generation: {metadata.get('generation', 'unknown')}\n"
        result += f"- Resource Version: {metadata.get('resourceVersion', 'unknown')}\n"
        result += "\nRole Attributes:\n"
        result += format_database_role_attributes(spec)
        result += "\n\nOperator Status:\n"
        result += format_database_role_object_status(status)
        result += "\n"
        return result

    except Exception as e:
        return format_error_message(e, f"getting role status for {namespace}/{cluster_name}/{role_name}")



@with_mcp_context
async def create_postgres_role(
    context: MCPContext,
    cluster_name: str,
    role_name: str,
    login: bool = True,
    superuser: bool = False,
    inherit: bool = True,
    createdb: bool = False,
    createrole: bool = False,
    replication: bool = False,
    bypassrls: bool = False,
    in_roles: Optional[List[str]] = None,
    connection_limit: Optional[int] = None,
    valid_until: Optional[str] = None,
    comment: Optional[str] = None,
    disable_password: bool = False,
    client_certificate: bool = False,
    reclaim_policy: Literal["retain", "delete"] = "retain",
    namespace: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Create a new PostgreSQL role/user using CloudNativePG's DatabaseRole CRD.

    Creates a DatabaseRole custom resource that the CloudNativePG operator will
    reconcile. Unless disable_password is set, a secure password is generated and
    stored in a Kubernetes Secret referenced by the CRD.

    Args:
        cluster_name: Name of the PostgreSQL cluster.
        role_name: Name of the role to create.
        login: Allow role to log in (default: true).
        superuser: Grant superuser privileges (default: false).
        inherit: Inherit privileges from parent roles (default: true).
        createdb: Allow creating databases (default: false).
        createrole: Allow creating roles (default: false).
        replication: Allow streaming replication (default: false).
        bypassrls: Allow bypassing row-level security (default: false).
        in_roles: Existing roles this role is granted membership in.
        connection_limit: Maximum concurrent connections, -1 for no limit.
        valid_until: RFC 3339 timestamp after which the password expires.
        comment: Description attached to the role in PostgreSQL.
        disable_password: If True, sets the password to NULL and skips Secret creation.
        client_certificate: If True, the operator issues a TLS client certificate for the role.
        reclaim_policy: 'retain' to keep the role after CRD deletion, 'delete' to drop it.
        namespace: Kubernetes namespace.
        dry_run: If True, shows the DatabaseRole CRD that would be created without
                creating it. Useful for previewing the configuration. Default is False.

    Returns:
        Success message with password retrieval instructions.
        If dry_run=True, returns a preview of the DatabaseRole CRD.
    """
    try:
        # Validate role name conforms to RFC 1123 (required for CRD and secret naming)
        validate_rfc1123_name(role_name, "Role")

        if namespace is None:
            namespace = get_current_namespace()

        crd_name = database_role_crd_name(cluster_name, role_name)
        validate_rfc1123_name(crd_name, "DatabaseRole CRD")

        if client_certificate and not login:
            return "Error: client_certificate requires login=True."

        secret_name = None
        if not disable_password:
            secret_name = role_password_secret_name(cluster_name, role_name)
            validate_rfc1123_name(secret_name, "Role secret")

        # Verify the cluster exists and check for a conflicting role definition
        cluster = await get_cnpg_cluster(namespace, cluster_name)

        existing = await find_cnpg_database_role(namespace, cluster_name, role_name)
        if existing is not None:
            return (
                f"Error: Role '{role_name}' already exists in cluster '{namespace}/{cluster_name}' "
                f"(DatabaseRole CRD '{existing.get('metadata', {}).get('name', crd_name)}')."
            )

        legacy_roles = cluster.get('spec', {}).get('managed', {}).get('roles', [])
        if any(r.get('name') == role_name for r in legacy_roles):
            return (
                f"Error: Role '{role_name}' is already defined in the deprecated "
                f".spec.managed.roles field of cluster '{namespace}/{cluster_name}'. "
                "Remove it there before managing the role with a DatabaseRole CRD."
            )

        # Build the DatabaseRole CRD
        role_crd = {
            "apiVersion": f"{CNPG_GROUP}/{CNPG_VERSION}",
            "kind": "DatabaseRole",
            "metadata": {
                "name": crd_name,
                "namespace": namespace,
                "labels": {
                    "cnpg.io/cluster": cluster_name,
                    "cnpg.io/role": role_name
                }
            },
            "spec": {
                "name": role_name,
                "cluster": {
                    "name": cluster_name
                },
                "ensure": "present",
                "databaseRoleReclaimPolicy": reclaim_policy,
                "login": login,
                "superuser": superuser,
                "inherit": inherit,
                "createdb": createdb,
                "createrole": createrole,
                "replication": replication,
                "bypassrls": bypassrls
            }
        }

        optional_role_fields = {
            "inRoles": in_roles,
            "connectionLimit": connection_limit,
            "validUntil": valid_until,
            "comment": comment,
        }
        for field, value in optional_role_fields.items():
            if value is not None:
                role_crd["spec"][field] = value

        if disable_password:
            role_crd["spec"]["disablePassword"] = True
        else:
            role_crd["spec"]["passwordSecret"] = {"name": secret_name}

        if client_certificate:
            role_crd["spec"]["clientCertificate"] = {"enabled": True}

        role_attributes = format_database_role_attributes(role_crd["spec"])

        # If dry_run, show what would be created
        if dry_run:
            role_yaml = yaml.dump(role_crd, default_flow_style=False, sort_keys=False)
            secret_preview = (
                "- Kubernetes secret: (none; disable_password=True)"
                if disable_password
                else f"""- Kubernetes secret: {secret_name}
  - Contains auto-generated password (16 characters)
  - Labeled with cnpg.io/cluster={cluster_name} and cnpg.io/role={role_name}"""
            )

            return f"""Dry run: DatabaseRole CRD definition for '{role_name}' in cluster '{namespace}/{cluster_name}'

This is the DatabaseRole CRD that would be created:

```yaml
{role_yaml}```

Resources that would be created:
- DatabaseRole CRD: {crd_name}
{secret_preview}

Role Attributes:
{role_attributes}

Reclaim Policy Behavior:
- retain: Role is kept in PostgreSQL even if the CRD is deleted
- delete: Role is dropped from PostgreSQL when the CRD is deleted

To create this role, call create_postgres_role again with dry_run=False (or omit the dry_run parameter).
"""

        # Store the generated password before the operator needs it
        password = None
        if not disable_password:
            password = generate_password(16)
            await write_role_secret(namespace, secret_name, cluster_name, role_name, password)

        custom_api, _ = get_kubernetes_clients()
        try:
            await asyncio.to_thread(
                custom_api.create_namespaced_custom_object,
                group=CNPG_GROUP,
                version=CNPG_VERSION,
                namespace=namespace,
                plural=CNPG_DATABASE_ROLE_PLURAL,
                body=role_crd
            )
        except Exception:
            if secret_name is not None:
                try:
                    await delete_role_secret(namespace, secret_name)
                except Exception as cleanup_error:
                    logger.warning(
                        "Failed to clean up role password secret %s/%s after DatabaseRole creation failed: %s",
                        namespace,
                        secret_name,
                        cleanup_error,
                    )
            raise

        if disable_password:
            password_section = "Password: disabled (set to NULL in PostgreSQL)"
        else:
            password_section = f"""Password stored in Kubernetes secret: {secret_name}

To retrieve the password:
kubectl get secret {secret_name} -n {namespace} -o jsonpath='{{.data.password}}' | base64 -d

Connection string:
postgresql://{role_name}:<password>@{cluster_name}-rw.{namespace}.svc:5432/app"""

        certificate_section = (
            f"\n\nClient certificate Secret: {crd_name}-client-cert (issued and renewed by the operator)"
            if client_certificate else ""
        )

        return f"""Successfully created DatabaseRole CRD '{crd_name}' for role '{role_name}' in cluster '{namespace}/{cluster_name}'.

Role Attributes:
{role_attributes}
- Reclaim Policy: {reclaim_policy}

{password_section}{certificate_section}

The CloudNativePG operator will reconcile this role in the database.

To view the role status:
kubectl get databaserole {crd_name} -n {namespace}
"""

    except Exception as e:
        return format_error_message(e, f"creating role {role_name} in cluster {namespace}/{cluster_name}")



@with_mcp_context
async def update_postgres_role(
    context: MCPContext,
    cluster_name: str,
    role_name: str,
    login: Optional[bool] = None,
    superuser: Optional[bool] = None,
    inherit: Optional[bool] = None,
    createdb: Optional[bool] = None,
    createrole: Optional[bool] = None,
    replication: Optional[bool] = None,
    bypassrls: Optional[bool] = None,
    in_roles: Optional[List[str]] = None,
    connection_limit: Optional[int] = None,
    valid_until: Optional[str] = None,
    comment: Optional[str] = None,
    disable_password: Optional[bool] = None,
    client_certificate: Optional[bool] = None,
    reclaim_policy: Optional[Literal["retain", "delete"]] = None,
    password: Optional[str] = None,
    namespace: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Update an existing PostgreSQL role by patching its DatabaseRole CRD.

    Args:
        cluster_name: Name of the PostgreSQL cluster.
        role_name: Name of the role to update.
        login, superuser, inherit, createdb, createrole, replication, bypassrls:
            Optional attribute changes.
        in_roles: Optional replacement list of role memberships.
        connection_limit: Optional new concurrent connection limit.
        valid_until: Optional new password expiry timestamp (RFC 3339).
        comment: Optional new role description.
        disable_password: Optional toggle for setting the password to NULL.
        client_certificate: Optional toggle for operator-issued TLS client certificates.
        reclaim_policy: Optional new end-of-life policy: 'retain' or 'delete'.
        password: Optional new password. If not provided, password remains unchanged.
        namespace: Kubernetes namespace.
        dry_run: If True, shows what changes would be made without applying them.
                Useful for previewing the update. Default is False.

    Returns:
        Success message with updated attributes.
        If dry_run=True, returns a preview of the changes that would be made.
    """
    try:
        if namespace is None:
            namespace = get_current_namespace()

        role = await get_cnpg_database_role(namespace, cluster_name, role_name)
        crd_name = role.get("metadata", {}).get("name")
        spec = role.get("spec", {})

        spec_patch: Dict[str, Any] = {}
        updates: List[str] = []

        flag_changes = {
            "login": login,
            "superuser": superuser,
            "inherit": inherit,
            "createdb": createdb,
            "createrole": createrole,
            "replication": replication,
            "bypassrls": bypassrls,
        }
        for field, value in flag_changes.items():
            if value is None:
                continue
            label, default = DATABASE_ROLE_FLAGS[field]
            spec_patch[field] = value
            updates.append(f"{label}: {spec.get(field, default)} -> {value}")

        if in_roles is not None:
            spec_patch["inRoles"] = in_roles
            current = spec.get("inRoles", [])
            updates.append(f"Member of: {current or 'none'} -> {in_roles or 'none'}")

        if connection_limit is not None:
            spec_patch["connectionLimit"] = connection_limit
            updates.append(f"Connection Limit: {spec.get('connectionLimit', -1)} -> {connection_limit}")

        if valid_until is not None:
            spec_patch["validUntil"] = valid_until
            updates.append(f"Valid Until: {spec.get('validUntil', 'never')} -> {valid_until}")

        if comment is not None:
            spec_patch["comment"] = comment
            updates.append(f"Comment: {spec.get('comment', 'none')} -> {comment}")

        if client_certificate is not None:
            if client_certificate and not spec_patch.get("login", spec.get("login", False)):
                return "Error: client_certificate requires the role to have login enabled."
            spec_patch["clientCertificate"] = {"enabled": client_certificate}
            current = spec.get("clientCertificate", {}).get("enabled", False)
            updates.append(f"Client Certificate: {current} -> {client_certificate}")

        if reclaim_policy is not None:
            spec_patch["databaseRoleReclaimPolicy"] = reclaim_policy
            current = spec.get("databaseRoleReclaimPolicy", "retain")
            updates.append(f"Reclaim Policy: {current} -> {reclaim_policy}")

        if disable_password is not None:
            spec_patch["disablePassword"] = disable_password
            updates.append(f"Disable Password: {spec.get('disablePassword', False)} -> {disable_password}")

        secret_name = spec.get("passwordSecret", {}).get("name") or role_password_secret_name(cluster_name, role_name)

        if password is not None:
            if disable_password:
                return "Error: password cannot be set while disable_password=True."
            validate_rfc1123_name(secret_name, "Role secret")
            updates.append(f"Password: will be updated in Secret '{secret_name}'")
            if spec.get("disablePassword") and disable_password is None:
                spec_patch["disablePassword"] = False
                updates.append("Disable Password: True -> False (implied by setting a password)")
            if not spec.get("passwordSecret", {}).get("name"):
                spec_patch["passwordSecret"] = {"name": secret_name}
                updates.append(f"Password Secret: none -> {secret_name}")

        if not updates:
            return "No updates specified. Please provide at least one attribute to update."

        if dry_run:
            update_text = '\n- '.join(updates)
            return f"""Dry run: Update preview for role '{role_name}' in cluster '{namespace}/{cluster_name}'

DatabaseRole CRD: {crd_name}

Current attributes:
{format_database_role_attributes(spec)}
- Reclaim Policy: {spec.get('databaseRoleReclaimPolicy', 'retain')}

Proposed changes:
- {update_text}

To apply these changes, call update_postgres_role again with dry_run=False (or omit the dry_run parameter).
"""

        if password is not None:
            await write_role_secret(namespace, secret_name, cluster_name, role_name, password)

        if spec_patch:
            await patch_cnpg_database_role_spec(namespace, crd_name, spec_patch)

        updates_text = '\n- '.join(updates)
        return f"""Successfully updated role '{role_name}' in cluster '{namespace}/{cluster_name}' (DatabaseRole CRD '{crd_name}').

Updated Attributes:
- {updates_text}

The CloudNativePG operator will reconcile these changes in the database.
"""

    except Exception as e:
        return format_error_message(e, f"updating role {role_name} in cluster {namespace}/{cluster_name}")



@with_mcp_context
async def delete_postgres_role(
    context: MCPContext,
    cluster_name: str,
    role_name: str,
    drop_role: bool = False,
    namespace: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Delete a PostgreSQL role by removing its DatabaseRole CRD.

    Whether the role is actually dropped from PostgreSQL depends on the
    databaseRoleReclaimPolicy set on the CRD. Pass drop_role=True to force the
    policy to 'delete' before removing the CRD. The associated password Secret
    is deleted in either case.

    Args:
        cluster_name: Name of the PostgreSQL cluster.
        role_name: Name of the role to delete.
        drop_role: If True, force the reclaim policy to 'delete' so the role is
                  dropped from PostgreSQL. Default is False.
        namespace: Kubernetes namespace.
        dry_run: If True, shows what would be deleted without performing the deletion.
                Useful for previewing the deletion impact. Default is False.

    Returns:
        Success message.
        If dry_run=True, returns a preview of what would be deleted.
    """
    try:
        if namespace is None:
            namespace = get_current_namespace()

        role = await find_cnpg_database_role(namespace, cluster_name, role_name)
        spec = role.get("spec", {}) if role else {}
        crd_name = role.get("metadata", {}).get("name") if role else database_role_crd_name(cluster_name, role_name)
        secret_name = spec.get("passwordSecret", {}).get("name") or role_password_secret_name(cluster_name, role_name)
        reclaim_policy = "delete" if drop_role else spec.get("databaseRoleReclaimPolicy", "retain")

        if dry_run:
            secret_exists = await read_role_secret(namespace, secret_name) is not None
            action = "dropped from PostgreSQL" if reclaim_policy == "delete" else "retained in PostgreSQL"
            attributes = format_database_role_attributes(spec) if role else "- (DatabaseRole CRD not found)"

            return f"""Dry run: Deletion preview for role '{role_name}' in cluster '{namespace}/{cluster_name}'

Role details:
- DatabaseRole CRD: {crd_name} {'(exists)' if role else '(not found)'}
{attributes}

Resources that would be deleted:
- DatabaseRole CRD: {crd_name} {'(exists)' if role else '(not found; nothing to delete)'}
- Kubernetes secret: {secret_name} {'(exists)' if secret_exists else '(not found)'}

Impact based on reclaim policy:
- Reclaim Policy: {reclaim_policy}{' (forced by drop_role=True)' if drop_role else ''}
- Result: The role will be {action}

WARNING: If the role is dropped, any objects owned by it or permissions granted
to it will be affected.

To proceed with deletion, call delete_postgres_role again with dry_run=False (or omit the dry_run parameter).
"""

        if role is None:
            secret_deleted = await delete_role_secret(namespace, secret_name)
            if secret_deleted:
                return f"""Cleaned up orphaned PostgreSQL role secret for '{role_name}' in cluster '{namespace}/{cluster_name}'.

No DatabaseRole CRD existed for this role, so no CRD deletion was needed.
Deleted orphaned secret: {secret_name}
"""
            return f"Error: Role '{role_name}' has no DatabaseRole CRD in cluster '{namespace}/{cluster_name}', and associated secret '{secret_name}' was not found."

        if drop_role and spec.get("databaseRoleReclaimPolicy") != "delete":
            await patch_cnpg_database_role_spec(namespace, crd_name, {"databaseRoleReclaimPolicy": "delete"})

        custom_api, _ = get_kubernetes_clients()
        await asyncio.to_thread(
            custom_api.delete_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_DATABASE_ROLE_PLURAL,
            name=crd_name
        )

        secret_deleted = await delete_role_secret(namespace, secret_name)
        secret_msg = f"\nAssociated secret '{secret_name}' was also deleted." if secret_deleted else ""
        action = "will be dropped from PostgreSQL" if reclaim_policy == "delete" else "will be retained in PostgreSQL"

        return f"""Successfully deleted DatabaseRole CRD '{crd_name}' for role '{role_name}' in cluster '{namespace}/{cluster_name}'.{secret_msg}

Reclaim Policy: {reclaim_policy}{' (forced by drop_role=True)' if drop_role else ''}
Result: The role {action}.

The CloudNativePG operator will reconcile this change.
"""

    except Exception as e:
        return format_error_message(e, f"deleting role {role_name} from cluster {namespace}/{cluster_name}")



@with_mcp_context
async def list_postgres_databases(
    context: MCPContext,
    cluster_name: str,
    namespace: Optional[str] = None,
    format: Literal["text", "json"] = "text"
) -> str:
    """
    List all PostgreSQL databases managed by Database CRDs for a cluster.

    Args:
        cluster_name: Name of the PostgreSQL cluster.
        namespace: Kubernetes namespace where the cluster exists.
        format: Output format. 'text' for human-readable (default), 'json' for structured
               data that can be programmatically consumed.

    Returns:
        Formatted list of databases with their details. If format='json', returns a JSON
        string with structured data.
    """
    try:
        if namespace is None:
            namespace = get_current_namespace()

        # List all Database CRDs in the namespace
        custom_api, _ = get_kubernetes_clients()
        databases = await asyncio.to_thread(
            custom_api.list_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_DATABASE_PLURAL
        )

        # Filter for databases belonging to this cluster
        cluster_databases = [
            db for db in databases.get('items', [])
            if db.get('spec', {}).get('cluster', {}).get('name') == cluster_name
        ]

        if not cluster_databases:
            if format == "json":
                return json.dumps({
                    "cluster": f"{namespace}/{cluster_name}",
                    "databases": [],
                    "count": 0
                })
            return f"No managed databases found for cluster '{namespace}/{cluster_name}'.\n\nDatabases are managed through Database CRDs."

        if format == "json":
            # Return structured JSON
            database_list = []
            for db in cluster_databases:
                spec = db.get('spec', {})
                metadata = db.get('metadata', {})

                db_data = {
                    "crd_name": metadata.get('name', 'unknown'),
                    "database_name": spec.get('name', 'unknown'),
                    "owner": spec.get('owner', 'unknown'),
                    "ensure": spec.get('ensure', 'present'),
                    "reclaim_policy": spec.get('databaseReclaimPolicy', 'retain')
                }
                for field in DATABASE_CREATE_OPTION_LABELS:
                    if field in spec:
                        db_data[field] = spec[field]
                database_list.append(db_data)

            return json.dumps({
                "cluster": f"{namespace}/{cluster_name}",
                "databases": database_list,
                "count": len(database_list)
            }, indent=2)

        # Default: human-readable text
        result = f"PostgreSQL Databases for cluster '{namespace}/{cluster_name}':\n\n"

        for db in cluster_databases:
            spec = db.get('spec', {})
            metadata = db.get('metadata', {})

            crd_name = metadata.get('name', 'unknown')
            db_name = spec.get('name', 'unknown')
            owner = spec.get('owner', 'unknown')
            ensure = spec.get('ensure', 'present')
            reclaim_policy = spec.get('databaseReclaimPolicy', 'retain')

            result += f"**{db_name}** (CRD: {crd_name})\n"
            result += f"  - Owner: {owner}\n"
            result += f"  - Ensure: {ensure}\n"
            result += f"  - Reclaim Policy: {reclaim_policy}\n"
            for field, label in DATABASE_CREATE_OPTION_LABELS.items():
                if field in spec:
                    result += f"  - {label}: {spec[field]}\n"
            result += "\n"

        return result

    except Exception as e:
        return format_error_message(e, f"listing databases for cluster {namespace}/{cluster_name}")


@with_mcp_context
async def get_postgres_database_status(
    context: MCPContext,
    cluster_name: str,
    database_name: str,
    namespace: Optional[str] = None,
    format: Literal["text", "json"] = "text"
) -> str:
    """
    Get the current Database CRD status and configured create-time options.

    Args:
        cluster_name: Name of the PostgreSQL cluster.
        database_name: Name of the database inside PostgreSQL.
        namespace: Kubernetes namespace where the cluster exists.
        format: Output format. 'text' for human-readable (default), 'json' for structured
               data that can be programmatically consumed.

    Returns:
        Database CRD metadata, current spec values for encoding/collation/locale options,
        and operator reconciliation status.
    """
    try:
        if namespace is None:
            namespace = get_current_namespace()

        database = await get_cnpg_database(namespace, cluster_name, database_name)
        metadata = database.get("metadata", {})
        spec = database.get("spec", {})
        status = database.get("status", {})

        if format == "json":
            return json.dumps({
                "cluster": f"{namespace}/{cluster_name}",
                "crd_name": metadata.get("name", "unknown"),
                "database_name": spec.get("name", database_name),
                "owner": spec.get("owner", "unknown"),
                "ensure": spec.get("ensure", "present"),
                "reclaim_policy": spec.get("databaseReclaimPolicy", "retain"),
                "generation": metadata.get("generation"),
                "resource_version": metadata.get("resourceVersion"),
                "create_options": database_create_options_dict(spec, include_unset=True),
                "status": status,
            }, indent=2)

        result = f"**Database: {namespace}/{metadata.get('name', 'unknown')}**\n"
        result += f"- PostgreSQL Database Name: {spec.get('name', database_name)}\n"
        result += f"- Cluster: {namespace}/{cluster_name}\n"
        result += f"- Owner: {spec.get('owner', 'unknown')}\n"
        result += f"- Ensure: {spec.get('ensure', 'present')}\n"
        result += f"- Reclaim Policy: {spec.get('databaseReclaimPolicy', 'retain')}\n"
        result += f"- Generation: {metadata.get('generation', 'unknown')}\n"
        result += f"- Resource Version: {metadata.get('resourceVersion', 'unknown')}\n"
        result += "\nCurrent Locale/Encoding Values:\n"
        result += format_database_create_options(spec, include_unset=True)
        result += "\n\nOperator Status:\n"
        result += format_database_object_status(status)
        result += "\n"
        return result

    except Exception as e:
        return format_error_message(e, f"getting database status for {namespace}/{cluster_name}/{database_name}")



@with_mcp_context
async def create_postgres_database(
    context: MCPContext,
    cluster_name: str,
    database_name: str,
    owner: str,
    reclaim_policy: Literal["retain", "delete"] = "retain",
    encoding: Optional[str] = None,
    locale: Optional[str] = None,
    locale_provider: Optional[Literal["builtin", "icu", "libc"]] = None,
    locale_collate: Optional[str] = None,
    locale_ctype: Optional[str] = None,
    icu_locale: Optional[str] = None,
    icu_rules: Optional[str] = None,
    builtin_locale: Optional[str] = None,
    collation_version: Optional[str] = None,
    namespace: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Create a new PostgreSQL database using CloudNativePG's Database CRD.

    Creates a Database custom resource that the CloudNativePG operator will reconcile.

    Args:
        cluster_name: Name of the PostgreSQL cluster.
        database_name: Name of the database to create.
        owner: Name of the role that will own the database.
        reclaim_policy: 'retain' to keep database after CRD deletion, 'delete' to remove it.
        encoding: Optional CREATE DATABASE ENCODING value, for example UTF8.
        locale: Optional CREATE DATABASE LOCALE value.
        locale_provider: Optional CREATE DATABASE LOCALE_PROVIDER value: builtin, icu, or libc.
        locale_collate: Optional CREATE DATABASE LC_COLLATE value.
        locale_ctype: Optional CREATE DATABASE LC_CTYPE value.
        icu_locale: Optional CREATE DATABASE ICU_LOCALE value. Requires locale_provider='icu'.
        icu_rules: Optional CREATE DATABASE ICU_RULES value. Requires locale_provider='icu'.
        builtin_locale: Optional CREATE DATABASE BUILTIN_LOCALE value. Requires locale_provider='builtin'.
        collation_version: Optional CREATE DATABASE COLLATION_VERSION value.
        namespace: Kubernetes namespace.
        dry_run: If True, shows the Database CRD definition that would be created without
                creating it. Useful for previewing the configuration. Default is False.

    Returns:
        Success message with database details.
        If dry_run=True, returns a preview of the Database CRD definition.
    """
    try:
        # Validate database name conforms to RFC 1123 (required for Database CRD naming)
        validate_rfc1123_name(database_name, "Database")

        if namespace is None:
            namespace = get_current_namespace()

        # Create a unique CRD name (cluster-database)
        crd_name = f"{cluster_name}-{database_name}"

        # Validate the resulting CRD name also conforms to RFC 1123
        validate_rfc1123_name(crd_name, "Database CRD")

        if (icu_locale or icu_rules) and locale_provider != "icu":
            return "Error: icu_locale and icu_rules require locale_provider='icu'."

        if builtin_locale and locale_provider != "builtin":
            return "Error: builtin_locale requires locale_provider='builtin'."

        # Build the Database CRD
        database_crd = {
            "apiVersion": f"{CNPG_GROUP}/{CNPG_VERSION}",
            "kind": "Database",
            "metadata": {
                "name": crd_name,
                "namespace": namespace,
                "labels": {
                    "cnpg.io/cluster": cluster_name,
                    "cnpg.io/database": database_name
                }
            },
            "spec": {
                "name": database_name,
                "owner": owner,
                "cluster": {
                    "name": cluster_name
                },
                "ensure": "present",
                "databaseReclaimPolicy": reclaim_policy
            }
        }

        optional_database_fields = {
            "encoding": encoding,
            "locale": locale,
            "localeProvider": locale_provider,
            "localeCollate": locale_collate,
            "localeCType": locale_ctype,
            "icuLocale": icu_locale,
            "icuRules": icu_rules,
            "builtinLocale": builtin_locale,
            "collationVersion": collation_version,
        }
        for field, value in optional_database_fields.items():
            if value is not None:
                database_crd["spec"][field] = value

        database_options = format_database_create_options(database_crd["spec"])

        # If dry_run, return the Database CRD definition
        if dry_run:
            database_yaml = yaml.dump(database_crd, default_flow_style=False, sort_keys=False)
            return f"""Dry run: Database CRD definition for '{database_name}' in cluster '{namespace}/{cluster_name}'

This is the Database CRD that would be created:

```yaml
{database_yaml}```

Database Details:
- Name: {database_name}
- Owner: {owner}
- Reclaim Policy: {reclaim_policy}
- CRD Name: {crd_name}

Database Locale/Encoding Options:
{database_options}

Reclaim Policy Behavior:
- retain: Database will be kept in PostgreSQL even if the CRD is deleted
- delete: Database will be dropped from PostgreSQL when the CRD is deleted

To create this database, call create_postgres_database again with dry_run=False (or omit the dry_run parameter).
"""

        # Create the Database CRD
        custom_api, _ = get_kubernetes_clients()
        await asyncio.to_thread(
            custom_api.create_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_DATABASE_PLURAL,
            body=database_crd
        )

        return f"""Successfully created Database CRD for '{database_name}' in cluster '{namespace}/{cluster_name}'.

Database Details:
- Name: {database_name}
- Owner: {owner}
- Reclaim Policy: {reclaim_policy}
- CRD Name: {crd_name}

Database Locale/Encoding Options:
{database_options}

The CloudNativePG operator will create this database in the cluster.

To view the database status:
kubectl get database {crd_name} -n {namespace}
"""

    except Exception as e:
        return format_error_message(e, f"creating database {database_name} in cluster {namespace}/{cluster_name}")



@with_mcp_context
async def delete_postgres_database(
    context: MCPContext,
    cluster_name: str,
    database_name: str,
    namespace: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Delete a PostgreSQL database by removing its Database CRD.

    Whether the database is actually dropped from PostgreSQL depends on the
    databaseReclaimPolicy set when the database was created.

    Args:
        cluster_name: Name of the PostgreSQL cluster.
        database_name: Name of the database to delete.
        namespace: Kubernetes namespace.
        dry_run: If True, shows what would be deleted without performing the deletion.
                Useful for previewing the deletion impact. Default is False.

    Returns:
        Success message.
        If dry_run=True, returns a preview of what would be deleted.
    """
    try:
        if namespace is None:
            namespace = get_current_namespace()

        # Find the Database CRD
        crd_name = f"{cluster_name}-{database_name}"

        custom_api, _ = get_kubernetes_clients()

        # Get the database to check reclaim policy
        try:
            database_crd = await asyncio.to_thread(
                custom_api.get_namespaced_custom_object,
                group=CNPG_GROUP,
                version=CNPG_VERSION,
                namespace=namespace,
                plural=CNPG_DATABASE_PLURAL,
                name=crd_name
            )
            spec = database_crd.get('spec', {})
            reclaim_policy = spec.get('databaseReclaimPolicy', 'retain')
            owner = spec.get('owner', 'unknown')
        except ApiException as e:
            if e.status == 404:
                return f"Error: Database CRD '{crd_name}' not found for database '{database_name}' in cluster '{namespace}/{cluster_name}'."
            raise

        # If dry_run, show what would be deleted
        if dry_run:
            action = "dropped from PostgreSQL" if reclaim_policy == "delete" else "retained in PostgreSQL"

            return f"""Dry run: Deletion preview for database '{database_name}' in cluster '{namespace}/{cluster_name}'

Database Details:
- Name: {database_name}
- Owner: {owner}
- Reclaim Policy: {reclaim_policy}
- CRD Name: {crd_name}

Resources that would be deleted:
- Database CRD: {crd_name}

Impact based on reclaim policy:
- Reclaim Policy: {reclaim_policy}
- Result: The database will be {action}

Reclaim Policy Behavior:
- retain: Database CRD is deleted but the database remains in PostgreSQL
- delete: Database CRD is deleted AND the database is dropped from PostgreSQL

WARNING: If reclaim_policy is 'delete', all data in this database will be PERMANENTLY LOST.

To proceed with deletion, call delete_postgres_database again with dry_run=False (or omit the dry_run parameter).
"""

        # Delete the Database CRD
        await asyncio.to_thread(
            custom_api.delete_namespaced_custom_object,
            group=CNPG_GROUP,
            version=CNPG_VERSION,
            namespace=namespace,
            plural=CNPG_DATABASE_PLURAL,
            name=crd_name
        )

        action = "will be dropped from PostgreSQL" if reclaim_policy == "delete" else "will be retained in PostgreSQL"

        return f"""Successfully deleted Database CRD '{crd_name}' for database '{database_name}'.

Reclaim Policy: {reclaim_policy}
Result: The database {action}.

The CloudNativePG operator will reconcile this change.
"""

    except Exception as e:
        return format_error_message(e, f"deleting database {database_name} from cluster {namespace}/{cluster_name}")




# ============================================================================
# Prompt and Resource Administration
# ============================================================================

@with_mcp_context
async def admin_reload_prompts_impl(context: MCPContext) -> str:
    """Reload prompts from the ConfigMap-backed prompt registry."""
    user_info = context.preferred_username or context.user_id or "anonymous"
    if getattr(context, "ctx", None) is not None:
        await context.ctx.info(f"User {user_info} requested prompt reload")
    manifest = reload_prompt_registry()
    return f"Prompts reloaded: {json.dumps(manifest, indent=2)}"


def register_resources(mcp):
    """Register read-only MCP resources."""

    @mcp.resource("docs://cnpg-mcp/tools")
    def get_tools_reference() -> str:
        """CloudNativePG MCP tool reference."""
        tools = [
            "list_postgres_clusters",
            "get_cluster_status",
            "create_postgres_cluster",
            "scale_postgres_cluster",
            "delete_postgres_cluster",
            "list_postgres_roles",
            "get_postgres_role_status",
            "create_postgres_role",
            "update_postgres_role",
            "delete_postgres_role",
            "list_postgres_databases",
            "get_postgres_database_status",
            "create_postgres_database",
            "delete_postgres_database",
        ]
        return "# CloudNativePG MCP Tools\n\n" + "\n".join(f"- `{tool}`" for tool in tools) + "\n"


def _build_prompt_handler(registry, prompt_def):
    # FastMCP rejects prompt handlers that use **kwargs, so synthesize an
    # explicit signature derived from the prompt registry metadata.
    prompt_id = prompt_def.id
    arg_names = [a.name for a in prompt_def.arguments]

    for name in arg_names:
        if not name.isidentifier():
            raise ValueError(
                f"Prompt '{prompt_id}' argument '{name}' is not a valid Python identifier."
            )

    required = [a.name for a in prompt_def.arguments if a.required]
    optional = [a.name for a in prompt_def.arguments if not a.required]
    sig_parts = [f"{n}: str" for n in required] + [f"{n}: str = None" for n in optional]
    signature_src = ", ".join(sig_parts)

    if arg_names:
        items = ", ".join(f"{n!r}: {n}" for n in arg_names)
        args_expr = "{k: v for k, v in {" + items + "}.items() if v is not None}"
    else:
        args_expr = "{}"

    src = (
        f"async def _handler({signature_src}) -> str:\n"
        f"    rendered, error = _registry.render_prompt(_prompt_id, {args_expr})\n"
        f"    if error:\n"
        f"        return 'Error rendering prompt: ' + str(error)\n"
        f"    return rendered\n"
    )
    namespace = {"_registry": registry, "_prompt_id": prompt_id}
    exec(src, namespace)
    handler = namespace["_handler"]
    handler.__name__ = prompt_id.replace("-", "_")
    handler.__doc__ = prompt_def.description
    return handler


def register_prompts(mcp):
    """Register prompts from the hot-reloadable prompt registry."""
    registry = get_prompt_registry()

    for prompt_def in registry.get_all_prompts():
        handler = _build_prompt_handler(registry, prompt_def)
        mcp.prompt(name=prompt_def.id, description=prompt_def.description)(handler)

    @mcp.prompt(name="list-prompts", description="List all available prompts with their descriptions")
    async def list_prompts_prompt() -> str:
        """Generate a listing of all available prompts."""
        registry = get_prompt_registry()
        manifest = registry.get_manifest()
        result = "# Available Prompts\n\n"
        result += f"**Version:** {manifest.get('version', 'unknown')}\n"
        result += f"**Bundle Hash:** {manifest.get('bundle_hash', 'unknown')[:24]}...\n\n"
        for prompt in registry.get_all_prompts():
            result += f"## {prompt.name} (`{prompt.id}`)\n\n{prompt.description}\n\n"
            if prompt.arguments:
                result += "**Arguments:**\n"
                for arg in prompt.arguments:
                    required = "required" if arg.required else "optional"
                    result += f"- `{arg.name}` ({required}): {arg.description}\n"
                result += "\n"
        return result


# ============================================================================
# Tool Registration
# ============================================================================

def register_tools(mcp):
    """Register all CloudNativePG tools with the FastMCP server instance."""

    @mcp.tool(name="list_postgres_clusters")
    async def list_postgres_clusters_tool(
        namespace: str = None,
        detail_level: Literal["concise", "detailed"] = "concise",
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        """List all PostgreSQL clusters managed by CloudNativePG."""
        return await list_postgres_clusters(
            context=ctx,
            namespace=namespace,
            detail_level=detail_level,
            format=format,
        )

    @mcp.tool(name="get_cluster_status")
    async def get_cluster_status_tool(
        name: str,
        namespace: str = None,
        detail_level: Literal["concise", "detailed"] = "concise",
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        """Get detailed status of a specific PostgreSQL cluster."""
        return await get_cluster_status(
            context=ctx,
            name=name,
            namespace=namespace,
            detail_level=detail_level,
            format=format,
        )

    @mcp.tool(name="create_postgres_cluster")
    async def create_postgres_cluster_tool(
        name: str,
        instances: int = 3,
        storage_size: str = "10Gi",
        postgres_version: str = "16",
        container_image: str = None,
        storage_class: str = None,
        image_pull_policy: str = None,
        node_selector: Dict[str, str] = None,
        tolerations: List[Dict[str, Any]] = None,
        wait: bool = False,
        timeout: int = None,
        namespace: str = None,
        dry_run: bool = False,
        ctx: Context = None,
    ) -> str:
        """Create a new PostgreSQL cluster with high availability configuration."""
        return await create_postgres_cluster(
            context=ctx,
            name=name,
            instances=instances,
            storage_size=storage_size,
            postgres_version=postgres_version,
            container_image=container_image,
            storage_class=storage_class,
            image_pull_policy=image_pull_policy,
            node_selector=node_selector,
            tolerations=tolerations,
            wait=wait,
            timeout=timeout,
            namespace=namespace,
            dry_run=dry_run,
        )

    @mcp.tool(name="scale_postgres_cluster")
    async def scale_postgres_cluster_tool(
        name: str,
        instances: int,
        namespace: str = None,
        dry_run: bool = False,
        ctx: Context = None,
    ) -> str:
        """Scale a PostgreSQL cluster by changing the number of instances."""
        return await scale_postgres_cluster(
            context=ctx,
            name=name,
            instances=instances,
            namespace=namespace,
            dry_run=dry_run,
        )

    @mcp.tool(name="delete_postgres_cluster")
    async def delete_postgres_cluster_tool(
        name: str,
        confirm_deletion: bool = False,
        namespace: str = None,
        dry_run: bool = False,
        ctx: Context = None,
    ) -> str:
        """Delete a PostgreSQL cluster."""
        return await delete_postgres_cluster(
            context=ctx,
            name=name,
            confirm_deletion=confirm_deletion,
            namespace=namespace,
            dry_run=dry_run,
        )

    @mcp.tool(name="list_postgres_roles")
    async def list_postgres_roles_tool(
        cluster_name: str,
        namespace: str = None,
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        """List all PostgreSQL roles managed by CloudNativePG DatabaseRole CRDs."""
        return await list_postgres_roles(
            context=ctx,
            cluster_name=cluster_name,
            namespace=namespace,
            format=format,
        )

    @mcp.tool(name="get_postgres_role_status")
    async def get_postgres_role_status_tool(
        cluster_name: str,
        role_name: str,
        namespace: Optional[str] = None,
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        """Get a CloudNativePG DatabaseRole CRD's current spec values and reconciliation status."""
        return await get_postgres_role_status(
            context=ctx,
            cluster_name=cluster_name,
            role_name=role_name,
            namespace=namespace,
            format=format,
        )

    @mcp.tool(name="create_postgres_role")
    async def create_postgres_role_tool(
        cluster_name: str,
        role_name: str,
        login: bool = True,
        superuser: bool = False,
        inherit: bool = True,
        createdb: bool = False,
        createrole: bool = False,
        replication: bool = False,
        bypassrls: bool = False,
        in_roles: List[str] = None,
        connection_limit: int = None,
        valid_until: str = None,
        comment: str = None,
        disable_password: bool = False,
        client_certificate: bool = False,
        reclaim_policy: Literal["retain", "delete"] = "retain",
        namespace: str = None,
        dry_run: bool = False,
        ctx: Context = None,
    ) -> str:
        """Create a new PostgreSQL role using a DatabaseRole CRD, with an auto-generated password secret."""
        return await create_postgres_role(
            context=ctx,
            cluster_name=cluster_name,
            role_name=role_name,
            login=login,
            superuser=superuser,
            inherit=inherit,
            createdb=createdb,
            createrole=createrole,
            replication=replication,
            bypassrls=bypassrls,
            in_roles=in_roles,
            connection_limit=connection_limit,
            valid_until=valid_until,
            comment=comment,
            disable_password=disable_password,
            client_certificate=client_certificate,
            reclaim_policy=reclaim_policy,
            namespace=namespace,
            dry_run=dry_run,
        )

    @mcp.tool(name="update_postgres_role")
    async def update_postgres_role_tool(
        cluster_name: str,
        role_name: str,
        login: bool = None,
        superuser: bool = None,
        inherit: bool = None,
        createdb: bool = None,
        createrole: bool = None,
        replication: bool = None,
        bypassrls: bool = None,
        in_roles: List[str] = None,
        connection_limit: int = None,
        valid_until: str = None,
        comment: str = None,
        disable_password: bool = None,
        client_certificate: bool = None,
        reclaim_policy: Literal["retain", "delete"] = None,
        password: str = None,
        namespace: str = None,
        dry_run: bool = False,
        ctx: Context = None,
    ) -> str:
        """Update an existing PostgreSQL role's DatabaseRole CRD and optionally reset its password."""
        return await update_postgres_role(
            context=ctx,
            cluster_name=cluster_name,
            role_name=role_name,
            login=login,
            superuser=superuser,
            inherit=inherit,
            createdb=createdb,
            createrole=createrole,
            replication=replication,
            bypassrls=bypassrls,
            in_roles=in_roles,
            connection_limit=connection_limit,
            valid_until=valid_until,
            comment=comment,
            disable_password=disable_password,
            client_certificate=client_certificate,
            reclaim_policy=reclaim_policy,
            password=password,
            namespace=namespace,
            dry_run=dry_run,
        )

    @mcp.tool(name="delete_postgres_role")
    async def delete_postgres_role_tool(
        cluster_name: str,
        role_name: str,
        drop_role: bool = False,
        namespace: str = None,
        dry_run: bool = False,
        ctx: Context = None,
    ) -> str:
        """Delete a PostgreSQL role's DatabaseRole CRD and its associated secret."""
        return await delete_postgres_role(
            context=ctx,
            cluster_name=cluster_name,
            role_name=role_name,
            drop_role=drop_role,
            namespace=namespace,
            dry_run=dry_run,
        )

    @mcp.tool(name="list_postgres_databases")
    async def list_postgres_databases_tool(
        cluster_name: str,
        namespace: str = None,
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        """List all databases managed by CloudNativePG Database CRDs."""
        return await list_postgres_databases(
            context=ctx,
            cluster_name=cluster_name,
            namespace=namespace,
            format=format,
        )

    @mcp.tool(name="get_postgres_database_status")
    async def get_postgres_database_status_tool(
        cluster_name: str,
        database_name: str,
        namespace: Optional[str] = None,
        format: Literal["text", "json"] = "text",
        ctx: Context = None,
    ) -> str:
        """Get a CloudNativePG Database CRD's current spec values and reconciliation status."""
        return await get_postgres_database_status(
            context=ctx,
            cluster_name=cluster_name,
            database_name=database_name,
            namespace=namespace,
            format=format,
        )

    @mcp.tool(name="create_postgres_database")
    async def create_postgres_database_tool(
        cluster_name: str,
        database_name: str,
        owner: str,
        reclaim_policy: Literal["retain", "delete"] = "retain",
        encoding: Optional[str] = None,
        locale: Optional[str] = None,
        locale_provider: Optional[Literal["builtin", "icu", "libc"]] = None,
        locale_collate: Optional[str] = None,
        locale_ctype: Optional[str] = None,
        icu_locale: Optional[str] = None,
        icu_rules: Optional[str] = None,
        builtin_locale: Optional[str] = None,
        collation_version: Optional[str] = None,
        namespace: Optional[str] = None,
        dry_run: bool = False,
        ctx: Context = None,
    ) -> str:
        """Create a new database using the CloudNativePG Database CRD."""
        return await create_postgres_database(
            context=ctx,
            cluster_name=cluster_name,
            database_name=database_name,
            owner=owner,
            reclaim_policy=reclaim_policy,
            encoding=encoding,
            locale=locale,
            locale_provider=locale_provider,
            locale_collate=locale_collate,
            locale_ctype=locale_ctype,
            icu_locale=icu_locale,
            icu_rules=icu_rules,
            builtin_locale=builtin_locale,
            collation_version=collation_version,
            namespace=namespace,
            dry_run=dry_run,
        )

    @mcp.tool(name="delete_postgres_database")
    async def delete_postgres_database_tool(
        cluster_name: str,
        database_name: str,
        namespace: str = None,
        dry_run: bool = False,
        ctx: Context = None,
    ) -> str:
        """Delete a CloudNativePG Database CRD."""
        return await delete_postgres_database(
            context=ctx,
            cluster_name=cluster_name,
            database_name=database_name,
            namespace=namespace,
            dry_run=dry_run,
        )

    @mcp.tool(name="admin_reload_prompts")
    async def admin_reload_prompts(ctx: Context = None) -> str:
        """Reload prompts from the ConfigMap."""
        return await admin_reload_prompts_impl(context=ctx)

    @mcp.tool(name="admin_get_prompt_manifest")
    async def admin_get_prompt_manifest() -> str:
        """Get the current prompt manifest with version and hash."""
        registry = get_prompt_registry()
        manifest = registry.get_manifest()
        return json.dumps(manifest, indent=2)
