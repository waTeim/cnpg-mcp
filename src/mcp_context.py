"""
MCP Context wrapper with user identification.

This module provides MCPContext class and with_mcp_context decorator for
extracting user information from JWT tokens in HTTP requests.

Usage:
    from fastmcp import Context
    from mcp_context import MCPContext, with_mcp_context

    @with_mcp_context
    async def my_tool_impl(context: MCPContext, param1: str) -> str:
        # context.user_id is available here
        return f"User {context.user_id} called with {param1}"

    @mcp.tool(name="my_tool")
    async def my_tool(param1: str, ctx: Context = None) -> str:
        return await my_tool_impl(ctx=ctx, param1=param1)
"""

import functools
import inspect
import logging
from typing import Any, Optional

from fastmcp import Context as FastMCPContext
try:
    from fastmcp.server.dependencies import get_http_request
except ImportError:
    # Fallback for older FastMCP versions
    get_http_request = None

# Import user identification utilities
from user_hash import extract_user_info_from_request

# Configure logging
logger = logging.getLogger(__name__)


class MCPContext:
    """
    Extended MCP Context that includes user identification.

    Wraps FastMCP's Context and adds user-specific information extracted
    from JWT token claims (user_id, preferred_username, issuer).

    Attributes:
        ctx: The underlying FastMCP Context object
        user_id: RFC 1123 compatible user identifier (username-hash)
        preferred_username: User's preferred name from JWT token
        issuer: Token issuer (iss claim)
    """

    def __init__(self, ctx: FastMCPContext):
        """
        Initialize MCPContext with user information.

        Automatically extracts user info from the HTTP request if available.

        Args:
            ctx: FastMCP Context object
        """
        self.ctx = ctx
        self.user_id: Optional[str] = None
        self.preferred_username: Optional[str] = None
        self.issuer: Optional[str] = None

        # Extract user info from request
        self._extract_user_info()

    def _extract_user_info(self) -> None:
        """Extract user information from the HTTP request."""
        try:
            # Use new API if available, fallback to deprecated method
            if get_http_request is not None:
                request = get_http_request()
            else:
                request = self.ctx.get_http_request()

            user_info = extract_user_info_from_request(request)

            if user_info:
                self.user_id = user_info['user_id']
                self.preferred_username = user_info['preferred_username']
                self.issuer = user_info['issuer']
                logger.debug(f"User authenticated: {self.user_id} ({self.preferred_username})")
        except Exception as e:
            # In stdio mode or other non-HTTP transports, this is expected
            logger.debug(f"Could not extract user info (likely stdio mode): {e}")

    def __getattr__(self, name: str) -> Any:
        """
        Delegate attribute access to the underlying FastMCP Context.

        This allows MCPContext to be used as a drop-in replacement for Context,
        providing access to all FastMCP Context methods like info(), debug(), etc.
        """
        return getattr(self.ctx, name)

    def __repr__(self) -> str:
        return f"MCPContext(user_id={self.user_id}, user={self.preferred_username})"


def with_mcp_context(func):
    """
    Decorator that wraps FastMCP Context into MCPContext before calling the tool function.

    This decorator should be applied to tool functions that expect MCPContext as their
    first parameter. It intercepts the FastMCP Context that FastMCP automatically injects,
    wraps it into MCPContext (which includes user_id), and passes it to the tool.

    Usage:
        @with_mcp_context
        async def my_tool_impl(context: MCPContext, param1: str) -> str:
            # context.user_id is available here
            return f"User {context.user_id} called with {param1}"

        @mcp.tool(name="my_tool")
        async def my_tool(param1: str, ctx: Context = None) -> str:
            return await my_tool_impl(ctx=ctx, param1=param1)

    This allows all user identification logic to be in one place rather than
    duplicated in every tool function.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # Get function signature to find the context parameter
        sig = inspect.signature(func)
        params = list(sig.parameters.keys())

        # Find FastMCPContext in args or kwargs
        fastmcp_ctx = None

        # Check if first parameter is annotated as FastMCPContext or is a Context instance
        if args and len(args) > 0:
            if isinstance(args[0], FastMCPContext):
                fastmcp_ctx = args[0]
                args = args[1:]  # Remove it from args

        # Check kwargs for 'ctx' or 'context'
        if not fastmcp_ctx:
            for key in ['ctx', 'context']:
                if key in kwargs:
                    if isinstance(kwargs[key], FastMCPContext):
                        fastmcp_ctx = kwargs.pop(key)
                        break

        # Wrap FastMCP Context into MCPContext
        if fastmcp_ctx:
            mcp_context = MCPContext(fastmcp_ctx)
            # Pass MCPContext as first positional argument
            return await func(mcp_context, *args, **kwargs)
        else:
            # No context found - shouldn't happen with FastMCP tools, but handle gracefully
            logger.warning(f"No FastMCP Context found for {func.__name__}")
            return await func(*args, **kwargs)

    return wrapper
