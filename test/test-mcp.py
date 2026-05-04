#!/usr/bin/env python3
"""
cnpg-mcp MCP Server Test Runner

Primary testing tool with two modes:
1. Automated tests via plugin system (default)
2. Interactive Inspector UI (--use-inspector flag)

Automatically obtains tokens from oidc-config.json if available.

Development Mode (No-Auth):
  For rapid development and testing without authentication:
    ./test-mcp.py --url http://localhost:4201/test --no-auth

  Requires the test server to be started in no-auth mode:
    python cnpg_mcp_test_server.py --no-auth --port 4201
"""

import os
import sys
import json
import argparse
import subprocess
import shutil
import time
import asyncio
import importlib
import inspect
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime

try:
    import yaml  # PyYAML — listed in requirements.txt
except ImportError:  # pragma: no cover
    yaml = None  # falls back to hard-coded defaults below


# ----------------------------------------------------------------------
# Canonical project config — port defaults come from ./mcp-project.yaml.
# ----------------------------------------------------------------------
DEFAULT_PROJECT_CONFIG = Path("mcp-project.yaml")
DEFAULT_MAIN_PORT = 4200
DEFAULT_TEST_PORT = 4201


def load_project_ports(config_path: Path) -> Tuple[int, int]:
    """
    Read `ports.main` and `ports.test` from mcp-project.yaml. Returns the
    hard-coded defaults if the file is missing or PyYAML is unavailable.
    Raises ValueError if the file exists but the values are out of range.
    """
    if yaml is None or not config_path.exists():
        return DEFAULT_MAIN_PORT, DEFAULT_TEST_PORT

    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    ports = config.get("ports", {})
    main_port = ports.get("main", DEFAULT_MAIN_PORT)
    test_port = ports.get("test", DEFAULT_TEST_PORT)

    for name, port in (("main", main_port), ("test", test_port)):
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ValueError(f"ports.{name} in {config_path} must be 1..65535")

    return main_port, test_port


class Colors:
    """Colors for terminal output."""
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'  # No Color

    @staticmethod
    def red(text): return f"{Colors.RED}{text}{Colors.NC}"
    @staticmethod
    def green(text): return f"{Colors.GREEN}{text}{Colors.NC}"
    @staticmethod
    def yellow(text): return f"{Colors.YELLOW}{text}{Colors.NC}"
    @staticmethod
    def blue(text): return f"{Colors.BLUE}{text}{Colors.NC}"


class LoggingSessionWrapper:
    """Wraps MCP session to log all requests and responses for debugging."""

    def __init__(self, session, log_file: str):
        self._session = session
        self._log_file = log_file
        self._request_counter = 0

        # Initialize log file
        with open(log_file, 'w') as f:
            f.write(f"# MCP Test Debug Log\n")
            f.write(f"# Started: {datetime.now().isoformat()}\n")
            f.write("=" * 80 + "\n\n")

    def _log_call(self, method: str, args: tuple, kwargs: dict, result: Any = None, error: Exception = None):
        """Log a method call with its arguments and result."""
        self._request_counter += 1
        timestamp = datetime.now().isoformat()

        with open(self._log_file, 'a') as f:
            f.write(f"\n{'=' * 100}\n")
            f.write(f"REQUEST #{self._request_counter}\n")
            f.write(f"{'=' * 100}\n")
            f.write(f"Time:   {timestamp}\n")
            f.write(f"Method: {method}\n")
            f.write(f"{'-' * 100}\n")

            # Log arguments
            if args or kwargs:
                f.write("ARGUMENTS:\n")
                f.write("-" * 100 + "\n")
                if args:
                    for i, arg in enumerate(args):
                        f.write(f"  Position {i}:\n")
                        f.write(self._format_value(arg, indent=4))
                if kwargs:
                    for key, value in kwargs.items():
                        f.write(f"  {key}:\n")
                        f.write(self._format_value(value, indent=4))

            # Log result or error
            if error:
                f.write("ERROR:\n")
                f.write("-" * 100 + "\n")
                f.write(f"{type(error).__name__}: {error}\n")
                import traceback
                f.write("\nTraceback:\n")
                f.write(traceback.format_exc())
            elif result is not None:
                f.write("RESPONSE:\n")
                f.write("-" * 100 + "\n")
                f.write(f"Type: {type(result).__name__}\n")
                f.write(self._format_value(result, indent=0))

            f.write("=" * 100 + "\n")

    def _format_value(self, value, indent=0):
        """Format a value for logging - NO TRUNCATION."""
        indent_str = " " * indent

        if hasattr(value, '__dict__'):
            # Object with attributes - show all attributes
            result = f"{indent_str}{type(value).__name__}:\n"
            attrs = {k: v for k, v in value.__dict__.items() if not k.startswith('_')}
            for key, val in attrs.items():
                result += f"{indent_str}  {key}: "
                result += self._format_value(val, indent + 4).lstrip()
            return result
        elif isinstance(value, (list, tuple)):
            if len(value) == 0:
                return f"[]\n"
            result = f"[{len(value)} items]\n"
            for i, item in enumerate(value):
                result += f"{indent_str}  [{i}]: "
                result += self._format_value(item, indent + 4).lstrip()
            return result
        elif isinstance(value, dict):
            if len(value) == 0:
                return f"{{}}\n"
            result = "\n"
            for key, val in value.items():
                result += f"{indent_str}  {key}: "
                result += self._format_value(val, indent + 4).lstrip()
            return result
        elif isinstance(value, str):
            # Multi-line strings get special formatting
            if '\n' in value:
                lines = value.split('\n')
                result = f"'''\n"
                for line in lines:
                    result += f"{indent_str}{line}\n"
                result += f"{indent_str}'''\n"
                return result
            else:
                return f"{value}\n"
        else:
            return f"{str(value)}\n"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    # Wrap common MCP session methods
    async def initialize(self, *args, **kwargs):
        try:
            result = await self._session.initialize(*args, **kwargs)
            self._log_call("initialize", args, kwargs, result=result)
            return result
        except Exception as e:
            self._log_call("initialize", args, kwargs, error=e)
            raise

    async def list_tools(self, *args, **kwargs):
        try:
            result = await self._session.list_tools(*args, **kwargs)
            self._log_call("list_tools", args, kwargs, result=result)
            return result
        except Exception as e:
            self._log_call("list_tools", args, kwargs, error=e)
            raise

    async def list_resources(self, *args, **kwargs):
        try:
            result = await self._session.list_resources(*args, **kwargs)
            self._log_call("list_resources", args, kwargs, result=result)
            return result
        except Exception as e:
            self._log_call("list_resources", args, kwargs, error=e)
            raise

    async def list_prompts(self, *args, **kwargs):
        try:
            result = await self._session.list_prompts(*args, **kwargs)
            self._log_call("list_prompts", args, kwargs, result=result)
            return result
        except Exception as e:
            self._log_call("list_prompts", args, kwargs, error=e)
            raise

    async def call_tool(self, *args, **kwargs):
        # Extract tool name for better logging
        tool_name = kwargs.get('name') or (args[0] if args else 'unknown')
        method_desc = f"call_tool({tool_name})"

        try:
            result = await self._session.call_tool(*args, **kwargs)
            self._log_call(method_desc, args, kwargs, result=result)
            return result
        except Exception as e:
            self._log_call(method_desc, args, kwargs, error=e)
            raise

    async def read_resource(self, *args, **kwargs):
        # Extract resource URI for better logging
        resource_uri = kwargs.get('uri') or (args[0] if args else 'unknown')
        method_desc = f"read_resource({resource_uri})"

        try:
            result = await self._session.read_resource(*args, **kwargs)
            self._log_call(method_desc, args, kwargs, result=result)
            return result
        except Exception as e:
            self._log_call(method_desc, args, kwargs, error=e)
            raise

    def __getattr__(self, name):
        """Forward other attributes to the wrapped session."""
        return getattr(self._session, name)


def get_user_token_interactive() -> Optional[str]:
    """
    Get user token by running get-user-token.py script.

    Returns:
        Access token or None if failed
    """
    print()
    print("=" * 70)
    print(Colors.blue("🔐 USER AUTHENTICATION REQUIRED"))
    print("=" * 70)
    print()
    print("The MCP server requires the 'openid' scope, which needs user login.")
    print("Running get-user-token.py to authenticate...")
    print()

    script_path = Path(__file__).parent / "get-user-token.py"

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=False,
            text=True
        )

        if result.returncode != 0:
            print()
            print(Colors.red("❌ User authentication failed"))
            return None

        token_file = Path("/tmp/user-token.txt")
        if token_file.exists():
            token = token_file.read_text().strip()
            print()
            print(Colors.green("✅ User token obtained successfully"))
            return token
        else:
            print()
            print(Colors.red("❌ Token file not found after authentication"))
            return None

    except Exception as e:
        print(Colors.red(f"❌ Error running get-user-token.py: {e}"))
        return None


def check_npx() -> bool:
    """Check if npx is available."""
    return shutil.which("npx") is not None


def load_oidc_config(config_path: str = "oidc-config.json") -> Optional[Dict[str, Any]]:
    """Load OIDC configuration from file (Auth0 supported)."""
    config_file = Path(config_path)
    if not config_file.exists() and config_path == "oidc-config.json":
        legacy = Path("auth0-config.json")
        if legacy.exists():
            config_file = legacy

    if not config_file.exists():
        return None

    try:
        with open(config_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(Colors.yellow(f"Warning: Failed to load {config_path}: {e}"))
        return None


def get_token_from_oidc(config: Dict[str, Any]) -> Optional[str]:
    """
    Get an access token using user authentication (Authorization Code + PKCE).

    Args:
        config: OIDC configuration dictionary

    Returns:
        Access token or None if failed
    """
    print(Colors.blue("Using user authentication (same as Claude Desktop)"))
    print()
    return get_user_token_interactive()


def topological_sort_plugins(plugins: List) -> List:
    """
    Sort plugins based on dependencies using topological sort.

    Args:
        plugins: List of plugin instances

    Returns:
        Sorted list of plugins (dependencies first)
    """
    plugin_map = {p.get_name(): p for p in plugins}
    visited = set()
    result = []

    def visit(plugin):
        if plugin.get_name() in visited:
            return
        visited.add(plugin.get_name())
        all_deps = list(set(plugin.depends_on + plugin.run_after))
        for dep_name in all_deps:
            if dep_name in plugin_map:
                visit(plugin_map[dep_name])
        result.append(plugin)

    for plugin in plugins:
        visit(plugin)

    return result


def discover_plugins(plugins_dir: Path) -> List:
    """Discover all test plugins in the plugins directory."""
    plugins = []

    if not plugins_dir.exists():
        return plugins

    sys.path.insert(0, str(plugins_dir.parent))

    for plugin_file in plugins_dir.glob("test_*.py"):
        try:
            module_name = f"plugins.{plugin_file.stem}"
            module = importlib.import_module(module_name)

            for name, obj in inspect.getmembers(module, inspect.isclass):
                if (hasattr(obj, 'test') and
                    callable(obj.test) and
                    obj.__module__ == module_name):
                    plugins.append(obj())

        except Exception as e:
            print(Colors.yellow(f"⚠️  Failed to load plugin {plugin_file.name}: {e}"))

    plugins = topological_sort_plugins(plugins)
    return plugins


async def run_automated_tests(transport: str, url: str = None, token: str = None,
                              token_file: str = None, oidc_config_path: str = "oidc-config.json",
                              output_file: str = None, output_format: str = "json",
                              no_auth: bool = False, debug_log: str = None,
                              include_integration: bool = False) -> int:
    """
    Run automated tests using plugin system.

    Args:
        transport: 'http' (stdio not supported for remote MCP)
        url: HTTP URL for MCP server
        token: JWT bearer token for HTTP authentication
        token_file: Path to file containing JWT token
        oidc_config_path: Path to oidc-config.json for automatic token retrieval
        output_file: Path to save test results (optional)
        output_format: Format for saved results ('json' or 'junit')
        no_auth: Skip authentication (for no-auth server mode)
        debug_log: Path to save detailed request/response log
        include_integration: Run mutating CloudNativePG Kubernetes integration tests

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    print("=" * 70)
    print(Colors.blue("cnpg-mcp MCP Server - Automated Test Suite"))
    print("=" * 70)
    print()

    plugins_dir = Path(__file__).parent / "plugins"
    plugins = discover_plugins(plugins_dir)

    if not plugins:
        print(Colors.yellow("⚠️  No test plugins found"))
        print(f"   Expected plugins in: {plugins_dir}")
        print()
        print("To create a test plugin, add a file like test/plugins/test_my_tool.py:")
        print("  from plugins import TestPlugin, TestResult")
        print("  class MyToolTest(TestPlugin):")
        print("      tool_name = 'my_tool'")
        print("      async def test(self, session): ...")
        return 1

    print(f"📋 Discovered {len(plugins)} test plugin(s)")
    for plugin in plugins:
        print(f"   • {plugin.tool_name}: {plugin.description}")
    print()

    # HTTP transport only
    print(Colors.blue("Transport: HTTP"))
    print(f"URL: {url}")
    print()

    auth_token = None
    token_source = None

    if no_auth:
        print(Colors.yellow("⚠️  Running without authentication (--no-auth)"))
        print()
    elif token:
        auth_token = token.strip()
        token_source = "command line argument"
    elif token_file:
        token_path = Path(token_file)
        if not token_path.exists():
            print(Colors.red(f"❌ Token file not found: {token_file}"))
            return 1
        auth_token = token_path.read_text().strip()
        if not auth_token:
            print(Colors.red(f"❌ Token file is empty: {token_file}"))
            return 1
        token_source = f"file: {token_file}"
    else:
        oidc_config = load_oidc_config(oidc_config_path)
        if oidc_config:
            print(Colors.green(f"✅ Found {oidc_config_path}"))
            print()
            auth_token = get_token_from_oidc(oidc_config)
            if auth_token:
                token_source = "user authentication (Authorization Code Flow)"
            else:
                print()
                print(Colors.red("❌ Failed to obtain authentication token"))
                return 1
        else:
            print(Colors.yellow(f"⚠️  No {oidc_config_path} found"))
            print("   Attempting connection without authentication...")
            print()

    if auth_token:
        print(Colors.green(f"✅ Using token from: {token_source}"))
        print()

    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.client.session import ClientSession

        # Construct MCP endpoint URL
        if auth_token and not url.endswith(('/mcp', '/mcp/', '/test', '/test/')):
            mcp_url = f"{url}/test/"
            print(Colors.blue("Using /test/ endpoint (OIDC token with standard OIDC)"))
        else:
            mcp_url = f"{url}/mcp" if not url.endswith(('/mcp', '/mcp/', '/test', '/test/')) else url

        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        print(Colors.blue(f"Connecting to: {mcp_url}"))
        print()

        async with streamablehttp_client(mcp_url, headers=headers) as (read, write, get_session_id):
            async with ClientSession(read, write) as raw_session:
                # Wrap session with logging if debug_log is specified
                if debug_log:
                    session = LoggingSessionWrapper(raw_session, debug_log)
                    print(Colors.blue(f"Debug logging enabled: {debug_log}"))
                else:
                    session = raw_session

                init_result = await session.initialize()
                print(Colors.green(f"Connected to server"))
                print(f"   Name: {init_result.serverInfo.name}")
                print(f"   Version: {init_result.serverInfo.version}")
                print()

                from plugins import TestContext
                ctx = TestContext(base_url=mcp_url, include_integration=include_integration)
                exit_code, results = await run_plugin_tests(session, plugins, ctx=ctx)

                if output_file:
                    save_test_results(results, output_file, output_format, transport, url)

                if debug_log:
                    print()
                    print(Colors.green(f"Debug log saved to: {debug_log}"))

                return exit_code

    except ImportError as e:
        print(Colors.red(f"❌ Failed to import MCP Streamable HTTP client library: {e}"))
        print()
        print("Install with: pip install mcp")
        return 1
    except Exception as e:
        print(Colors.red(f"❌ Failed to connect to server: {e}"))
        import traceback
        traceback.print_exc()
        return 1


async def run_plugin_tests(session, plugins: List, ctx=None) -> tuple[int, List]:
    """
    Run all plugin tests and report results.

    Args:
        session: Live MCP ClientSession.
        plugins: Discovered + topologically sorted plugin instances.
        ctx: Optional TestContext passed to plugins whose `test()` signature
            declares a `ctx` parameter. Older single-arg plugins are called
            without it.

    Returns:
        Tuple of (exit_code, results_list)
    """
    print("=" * 70)
    print("Running Tests")
    print("=" * 70)
    print()

    results = []
    passed = 0
    failed = 0
    failed_tests = set()

    for plugin in plugins:
        plugin_name = plugin.get_name()

        deps_failed = [dep for dep in plugin.depends_on if dep in failed_tests]
        if deps_failed:
            print(f"⏭️  {plugin_name}... ", end="")
            print(Colors.yellow(f"SKIPPED (dependency failed: {', '.join(deps_failed)})"))
            print()
            from plugins import TestResult
            results.append(TestResult(
                plugin_name=plugin_name,
                tool_name=plugin.tool_name,
                passed=False,
                message=f"Skipped because dependency failed: {', '.join(deps_failed)}"
            ))
            failed += 1
            failed_tests.add(plugin_name)
            continue

        print(f"▶️  {plugin_name}...", end=" ", flush=True)

        try:
            sig = inspect.signature(plugin.test)
            if ctx is not None and "ctx" in sig.parameters:
                result = await plugin.test(session, ctx=ctx)
            else:
                result = await plugin.test(session)
            results.append(result)

            if result.passed:
                print(Colors.green("✅ PASS"))
                passed += 1
            else:
                print(Colors.red("❌ FAIL"))
                failed += 1
                failed_tests.add(plugin_name)

            if result.duration_ms:
                print(f"   Duration: {result.duration_ms:.1f}ms")
            print(f"   {result.message}")
            if result.error:
                print(Colors.red(f"   Error: {result.error}"))
            print()

        except Exception as e:
            print(Colors.red("❌ EXCEPTION"))
            print(Colors.red(f"   Unexpected error: {e}"))
            print()
            failed += 1
            failed_tests.add(plugin_name)

            from plugins import TestResult
            results.append(TestResult(
                plugin_name=plugin_name,
                tool_name=plugin.tool_name,
                passed=False,
                message=f"Unexpected exception during test",
                error=str(e)
            ))

    print("=" * 70)
    print("Test Summary")
    print("=" * 70)
    print()
    print(f"Total:  {passed + failed} tests")
    print(Colors.green(f"Passed: {passed}"))
    print(Colors.red(f"Failed: {failed}"))
    print()

    if failed == 0:
        print(Colors.green("🎉 All tests passed!"))
        exit_code = 0
    else:
        print(Colors.red(f"❌ {failed} test(s) failed"))
        exit_code = 1

    return exit_code, results


def save_test_results(results: List, output_file: str, format: str = "json",
                      transport: str = "http", url: str = None):
    """Save test results to a file."""
    from datetime import datetime, timezone

    if format == "json":
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "transport": transport,
            "url": url,
            "summary": {
                "total": len(results),
                "passed": sum(1 for r in results if r.passed),
                "failed": sum(1 for r in results if not r.passed),
                "duration_ms": sum(r.duration_ms or 0 for r in results)
            },
            "tests": [
                {
                    "plugin_name": r.plugin_name,
                    "tool_name": r.tool_name,
                    "passed": r.passed,
                    "message": r.message,
                    "error": r.error,
                    "duration_ms": r.duration_ms
                }
                for r in results
            ]
        }

        with open(output_file, 'w') as f:
            json.dump(output, f, indent=2)

        print()
        print(Colors.green(f"✅ Test results saved to: {output_file}"))
        print(f"   Format: JSON")

    elif format == "junit":
        import xml.etree.ElementTree as ET

        total = len(results)
        failures = sum(1 for r in results if not r.passed)
        duration_s = sum(r.duration_ms or 0 for r in results) / 1000.0

        testsuite = ET.Element("testsuite", {
            "name": "cnpg-mcp MCP Automated Tests",
            "tests": str(total),
            "failures": str(failures),
            "errors": "0",
            "time": f"{duration_s:.3f}",
            "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        })

        properties = ET.SubElement(testsuite, "properties")
        ET.SubElement(properties, "property", {"name": "transport", "value": transport})
        if url:
            ET.SubElement(properties, "property", {"name": "url", "value": url})

        for r in results:
            testcase = ET.SubElement(testsuite, "testcase", {
                "name": r.plugin_name,
                "classname": f"mcp.tools.{r.tool_name}",
                "time": f"{(r.duration_ms or 0) / 1000:.3f}"
            })

            if not r.passed:
                failure = ET.SubElement(testcase, "failure", {
                    "message": r.message
                })
                if r.error:
                    failure.text = r.error

        tree = ET.ElementTree(testsuite)
        ET.indent(tree, space="  ")
        tree.write(output_file, encoding="utf-8", xml_declaration=True)

        print()
        print(Colors.green(f"✅ Test results saved to: {output_file}"))
        print(f"   Format: JUnit XML")


def main():
    parser = argparse.ArgumentParser(
        description="cnpg-mcp MCP Server Test Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run automated tests with HTTP transport
  ./test-mcp.py --url https://cnpg-mcp.example.com

  # Save test results to JSON file
  ./test-mcp.py --url https://cnpg-mcp.example.com --output results.json

  # Save test results to JUnit XML (for CI/CD)
  ./test-mcp.py --url https://cnpg-mcp.example.com --output results.xml --format junit

  # Development mode (no authentication) - requires no-auth server
  ./test-mcp.py --url http://localhost:4201/test --no-auth

  # With debug logging for troubleshooting
  ./test-mcp.py --url http://localhost:4201/test --no-auth --debug-log /tmp/mcp-debug.log

  # Launch Inspector UI for manual testing
  ./test-mcp.py --use-inspector --url https://cnpg-mcp.example.com --use-proxy

  # Inspector with kubectl port-forward
  ./test-mcp.py --use-inspector --port-forward --namespace default

Testing Modes:
  Default         - Automated tests via plugin system (no Inspector)
  --use-inspector - Launch Inspector UI for manual testing
  --no-auth       - Development mode (skip authentication, use with no-auth server)
  --include-integration
                  - Run mutating CloudNativePG Kubernetes integration tests

Inspector Options (only with --use-inspector):
  --use-proxy     - Auto-start auth proxy (eliminates manual header setup)
  --port-forward  - Use kubectl to access in-cluster service

Development Mode (No-Auth):
  Start the test server without authentication:
    python cnpg_mcp_test_server.py --no-auth --port 4201

  Then run tests without authentication:
    ./test-mcp.py --url http://localhost:4201/test --no-auth

Environment Variables:
  MCP_HTTP_URL    Default HTTP URL (default: http://localhost:4200)

Notes:
  - Automated tests use plugin system (test/plugins/test_*.py)
  - Automatically obtains user token when needed
"""
    )

    parser.add_argument(
        '-u', '--url',
        default=None,
        help='HTTP URL. Defaults to http://localhost:<ports.main> from mcp-project.yaml.'
    )
    parser.add_argument(
        '--project-config',
        type=Path,
        default=DEFAULT_PROJECT_CONFIG,
        help='Project config file used for port defaults (default: mcp-project.yaml)'
    )
    parser.add_argument(
        '--token',
        help='JWT bearer token for HTTP mode'
    )
    parser.add_argument(
        '--token-file',
        help='File containing JWT bearer token'
    )
    parser.add_argument(
        '--oidc-config',
        default='oidc-config.json',
        help='Path to oidc-config.json (default: ./oidc-config.json)'
    )
    parser.add_argument(
        '--auth0-config',
        dest='oidc_config',
        help='Deprecated alias for --oidc-config'
    )
    parser.add_argument(
        '--use-inspector',
        action='store_true',
        help='Launch Inspector UI for manual testing (default: run automated tests)'
    )
    parser.add_argument(
        '--use-proxy',
        action='store_true',
        help='[Inspector only] Start local auth proxy automatically'
    )
    parser.add_argument(
        '--proxy-port',
        type=int,
        default=8889,
        help='Auth proxy port (default: 8889)'
    )
    parser.add_argument(
        '--port-forward',
        action='store_true',
        help='Spawn kubectl port-forward and run tests against the forwarded local port. Works in both automated and Inspector mode.'
    )
    parser.add_argument(
        '--namespace',
        default='default',
        help='Kubernetes namespace for port-forward (default: default)'
    )
    parser.add_argument(
        '--service',
        default='cnpg-mcp-cnpg-mcp',
        help='Kubernetes service name for port-forward'
    )
    parser.add_argument(
        '--local-port',
        type=int,
        default=None,
        help='Local port for kubectl port-forward. Defaults to ports.main from mcp-project.yaml (or ports.test when --no-auth is set).'
    )
    parser.add_argument(
        '--remote-port',
        type=int,
        default=None,
        help='Remote service port for kubectl port-forward. Defaults to ports.main from mcp-project.yaml (or ports.test when --no-auth is set).'
    )
    parser.add_argument(
        '-o', '--output',
        dest='output_file',
        help='Save test results to file (automated tests only)'
    )
    parser.add_argument(
        '-f', '--format',
        dest='output_format',
        choices=['json', 'junit'],
        default='json',
        help='Output format for test results (default: json)'
    )
    parser.add_argument(
        '--no-auth',
        action='store_true',
        help='Skip authentication and target the no-auth /test endpoint (production endpoint stays /mcp).'
    )
    parser.add_argument(
        '--include-integration',
        action='store_true',
        help='Run mutating CloudNativePG Kubernetes integration tests.'
    )
    parser.add_argument(
        '--debug-log',
        dest='debug_log',
        help='Save detailed request/response log for debugging'
    )

    args = parser.parse_args()

    # Resolve port defaults from mcp-project.yaml unless overridden.
    main_port, test_port = load_project_ports(args.project_config)
    if args.url is None:
        args.url = (
            f"http://localhost:{test_port}" if args.no_auth
            else f"http://localhost:{main_port}"
        )
    forwarded_port = test_port if args.no_auth else main_port
    if args.local_port is None:
        args.local_port = forwarded_port
    if args.remote_port is None:
        args.remote_port = forwarded_port

    if not args.use_inspector:
        port_forward_proc = None
        try:
            test_url = args.url
            if args.port_forward:
                print(Colors.blue("Mode: kubectl port-forward"))
                print(f"{Colors.blue('Namespace:')} {args.namespace}")
                print(f"{Colors.blue('Service:')} {args.service}")
                print(f"{Colors.blue('Forward:')} localhost:{args.local_port} -> {args.service}:{args.remote_port}")
                print()

                port_forward_proc = subprocess.Popen(
                    [
                        'kubectl', 'port-forward',
                        '-n', args.namespace,
                        f'svc/{args.service}',
                        f'{args.local_port}:{args.remote_port}',
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                # Tiny grace period for kubectl to bind locally before
                # the MCP client attempts the connect.
                time.sleep(2)
                if port_forward_proc.poll() is not None:
                    stderr = port_forward_proc.stderr.read().decode(errors='replace')
                    print(Colors.red(f"❌ kubectl port-forward exited with code {port_forward_proc.returncode}"))
                    if stderr:
                        print(stderr.strip())
                    sys.exit(1)

                # Override the URL the runner connects to. /test for the
                # no-auth sidecar, /mcp for the production endpoint.
                if args.no_auth:
                    test_url = f"http://localhost:{args.local_port}/test"
                else:
                    test_url = f"http://localhost:{args.local_port}/mcp"

            exit_code = asyncio.run(run_automated_tests(
                transport='http',
                url=test_url,
                token=args.token,
                token_file=args.token_file,
                oidc_config_path=args.oidc_config,
                output_file=args.output_file,
                output_format=args.output_format,
                no_auth=args.no_auth,
                debug_log=args.debug_log,
                include_integration=args.include_integration
            ))
            sys.exit(exit_code)
        finally:
            if port_forward_proc is not None and port_forward_proc.poll() is None:
                port_forward_proc.terminate()
                try:
                    port_forward_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    port_forward_proc.kill()

    # Inspector mode
    if not check_npx():
        print(Colors.red("Error: npx is not installed"))
        print("Please install Node.js and npm to use the MCP Inspector")
        sys.exit(1)

    print("=" * 50)
    print("cnpg-mcp MCP Inspector")
    print("=" * 50)
    print()

    token = None
    token_source = None

    if args.token:
        token = args.token.strip()
        token_source = "command line argument"
    elif args.token_file:
        token_file_path = Path(args.token_file)
        if not token_file_path.exists():
            print(Colors.red(f"Error: Token file not found: {args.token_file}"))
            sys.exit(1)
        token = token_file_path.read_text().strip()
        if not token:
            print(Colors.red(f"Error: Token file is empty: {args.token_file}"))
            sys.exit(1)
        token_source = f"file: {args.token_file}"
    else:
        oidc_config = load_oidc_config(args.oidc_config)
        if oidc_config:
            print(Colors.green(f"✅ Found {args.oidc_config}"))
            print()
            token = get_token_from_oidc(oidc_config)
            if token:
                token_source = "user authentication (Authorization Code Flow)"
            else:
                print()
                print(Colors.red("❌ Failed to obtain token via user authentication"))
                if args.use_proxy:
                    sys.exit(1)
                else:
                    print()
                    print(Colors.yellow("⚠️  Could not automatically obtain token"))
                    print()
        else:
            print(Colors.yellow(f"⚠️  No {args.oidc_config} found"))
            print()

    print(f"{Colors.blue('Transport:')} HTTP")

    background_processes = []

    try:
        if args.port_forward:
            print(f"{Colors.blue('Mode:')} kubectl port-forward")
            print(f"{Colors.blue('Namespace:')} {args.namespace}")
            print(f"{Colors.blue('Service:')} {args.service}")
            print()

            print(Colors.green("Starting kubectl port-forward..."))
            forward_port = 4200
            port_forward_cmd = [
                'kubectl', 'port-forward',
                '-n', args.namespace,
                f'svc/{args.service}',
                f'{forward_port}:4200'
            ]

            port_forward_proc = subprocess.Popen(
                port_forward_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            background_processes.append(('kubectl port-forward', port_forward_proc))
            time.sleep(2)

            mcp_endpoint = f"http://localhost:{forward_port}/mcp"
            print(f"✅ Port-forward established")
            print()

        elif args.use_proxy:
            print(f"{Colors.blue('Mode:')} Auth proxy (auto-injects headers)")
            print(f"{Colors.blue('Backend:')} {args.url}")
            print(f"{Colors.blue('Proxy port:')} {args.proxy_port}")
            print()

            if not token:
                print(Colors.red("Error: --use-proxy requires a token"))
                sys.exit(1)

            token_file_path = Path("/tmp/mcp-user-token.txt")
            token_file_path.write_text(token)

            print(Colors.green("Starting auth proxy..."))
            proxy_cmd = [
                sys.executable,
                './test/mcp-auth-proxy.py',
                '--backend', args.url,
                '--port', str(args.proxy_port),
                '--token-file', str(token_file_path)
            ]

            proxy_proc = subprocess.Popen(proxy_cmd)
            background_processes.append(('auth proxy', proxy_proc))
            time.sleep(2)

            if proxy_proc.poll() is not None:
                print(Colors.red(f"✗ Proxy exited with code {proxy_proc.returncode}"))
                sys.exit(1)

            mcp_endpoint = f"http://localhost:{args.proxy_port}/mcp"
            print(f"✅ Auth proxy running at http://localhost:{args.proxy_port}")
            print()

        else:
            print(f"{Colors.blue('Mode:')} Direct connection")
            print(f"{Colors.blue('URL:')} {args.url}")
            print()

            if token:
                print(f"{Colors.blue('Authentication:')} JWT Bearer Token ({token_source})")
            else:
                print(f"{Colors.yellow('Authentication:')} None (development mode only!)")
            print()

            mcp_endpoint = f"{args.url}/mcp"

        print(Colors.green("Starting MCP Inspector..."))
        print(f"{Colors.blue('Connecting to:')} {mcp_endpoint}")
        print()

        cmd = [
            'npx', '@modelcontextprotocol/inspector',
            '--transport', 'http',
            '--url', mcp_endpoint
        ]

        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(Colors.red(f"Error: Inspector exited with code {e.returncode}"))
            sys.exit(e.returncode)
        except KeyboardInterrupt:
            print()
            print("Interrupted by user")

    finally:
        for name, proc in background_processes:
            print()
            print(Colors.yellow(f"Stopping {name}..."))
            proc.terminate()
            try:
                proc.wait(timeout=5)
                print(f"✅ {name} stopped")
            except subprocess.TimeoutExpired:
                proc.kill()
                print(f"⚠️  {name} killed")


if __name__ == "__main__":
    main()
