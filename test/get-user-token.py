#!/usr/bin/env python3
"""
Get a user access token from an OIDC provider using Authorization Code Flow with PKCE.

This mimics how Claude Desktop and other MCP clients authenticate users.
"""

import base64
import hashlib
import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Optional
from urllib.parse import urlencode, parse_qs, urlparse

import requests


# Global to store authorization code
auth_code = None
auth_error = None


class CallbackHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback from an OIDC provider."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_GET(self):
        """Handle GET request with authorization code or error."""
        global auth_code, auth_error

        # Parse query parameters
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if 'code' in params:
            # Success - got authorization code
            auth_code = params['code'][0]

            # Send success page
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()

            html = """
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Authentication Successful</title>
            </head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                <h1 style="color: green;">✅ Authentication Successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                <script>
                    setTimeout(function() {
                        window.close();
                    }, 2000);
                </script>
            </body>
            </html>
            """
            self.wfile.write(html.encode('utf-8'))

        elif 'error' in params:
            # Error during authorization
            auth_error = params.get('error_description', ['Unknown error'])[0]

            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()

            html = f"""
            <html>
            <head>
                <meta charset="UTF-8">
                <title>Authentication Failed</title>
            </head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                <h1 style="color: red;">❌ Authentication Failed</h1>
                <p>{auth_error}</p>
                <p>You can close this window and return to the terminal.</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode('utf-8'))

        else:
            # Unexpected request
            self.send_response(400)
            self.end_headers()


def generate_pkce_pair():
    """
    Generate PKCE code verifier and challenge.

    Returns:
        Tuple of (code_verifier, code_challenge)
    """
    # Generate code verifier (43-128 characters)
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8')
    code_verifier = code_verifier.rstrip('=')  # Remove padding

    # Generate code challenge (SHA256 hash of verifier)
    challenge_bytes = hashlib.sha256(code_verifier.encode('utf-8')).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_bytes).decode('utf-8')
    code_challenge = code_challenge.rstrip('=')  # Remove padding

    return code_verifier, code_challenge


def load_oidc_config(config_path: str = "oidc-config.json") -> dict:
    """Load OIDC configuration."""
    config_file = Path(config_path)
    if not config_file.exists() and config_path == "oidc-config.json":
        legacy = Path("auth0-config.json")
        if legacy.exists():
            config_file = legacy

    if not config_file.exists():
        raise FileNotFoundError(
            f"{config_path} not found. Create oidc-config.json or supply Auth0 config via auth0-config.json."
        )

    with open(config_file, 'r') as f:
        return json.load(f)


def resolve_oidc_endpoints(config: dict) -> tuple[str, str, str]:
    """
    Resolve authorization and token endpoints for an OIDC provider.

    Priority:
    1. authorization_endpoint / token_endpoint in config
    2. OIDC discovery via issuer
    3. Issuer-derived Auth0-style endpoints (fallback)
    """
    issuer = config.get("issuer")
    domain = config.get("domain")

    if not issuer and domain:
        issuer = domain if domain.startswith("http") else f"https://{domain}"

    auth_endpoint = config.get("authorization_endpoint")
    token_endpoint = config.get("token_endpoint")

    if issuer and (not auth_endpoint or not token_endpoint):
        discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            response = requests.get(discovery_url, timeout=10)
            if response.status_code == 200:
                discovery = response.json()
                auth_endpoint = auth_endpoint or discovery.get("authorization_endpoint")
                token_endpoint = token_endpoint or discovery.get("token_endpoint")
        except requests.RequestException:
            pass

    if issuer:
        auth_endpoint = auth_endpoint or f"{issuer.rstrip('/')}/authorize"
        token_endpoint = token_endpoint or f"{issuer.rstrip('/')}/oauth/token"

    if not auth_endpoint or not token_endpoint:
        raise ValueError("Could not resolve OIDC authorization/token endpoints.")

    return issuer or "", auth_endpoint, token_endpoint


def get_user_token_pkce(
    authorization_endpoint: str,
    token_endpoint: str,
    client_id: str,
    audience: str,
    callback_port: int = 8888,
    scope: str = "openid profile email mcp:read mcp:write"
) -> Optional[str]:
    """
    Get user token using Authorization Code Flow with PKCE.

    This is how Claude Desktop authenticates users.

    Args:
        authorization_endpoint: OIDC authorization endpoint URL
        token_endpoint: OIDC token endpoint URL
        client_id: OIDC client ID
        audience: API audience
        callback_port: Local port for callback (default: 8888)
        scope: OAuth scopes to request

    Returns:
        Access token or None if failed
    """
    global auth_code, auth_error

    # Reset globals
    auth_code = None
    auth_error = None

    # Generate PKCE pair
    code_verifier, code_challenge = generate_pkce_pair()

    # Callback URL
    callback_url = f"http://localhost:{callback_port}/callback"

    # Build authorization URL
    auth_params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': callback_url,
        'scope': scope,
        'audience': audience,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
    }

    authorization_url = f"{authorization_endpoint}?{urlencode(auth_params)}"

    print("=" * 70)
    print("🔐 USER AUTHENTICATION (Authorization Code Flow with PKCE)")
    print("=" * 70)
    print()
    print("This is the same flow Claude Desktop uses for authentication.")
    print()
    print(f"1. Starting local callback server on http://localhost:{callback_port}")
    print("2. Opening browser for OIDC login...")
    print()

    # Start local HTTP server for callback
    server = HTTPServer(('localhost', callback_port), CallbackHandler)

    def run_server():
        server.handle_request()  # Handle one request then stop

    server_thread = Thread(target=run_server, daemon=True)
    server_thread.start()

    # Open browser for authentication
    print(f"🌐 Opening: {authorization_url[:80]}...")
    print()

    try:
        webbrowser.open(authorization_url)
        print("✅ Browser opened")
    except:
        print("⚠️  Could not open browser automatically")
        print()
        print("Please manually visit:")
        print(authorization_url)

    print()
    print("⏳ Waiting for authentication in browser...")
    print("   (Login with your IdP user credentials)")
    print()

    # Wait for callback
    server_thread.join(timeout=120)  # Wait up to 2 minutes

    # Check if we got the code
    if auth_error:
        print(f"❌ Authentication error: {auth_error}")
        return None

    if not auth_code:
        print("❌ Timeout waiting for authentication")
        print("   Make sure you completed the login in the browser")
        return None

    print("✅ Authorization code received")
    print()
    print("🔄 Exchanging code for access token...")
    print()

    # Exchange authorization code for access token
    token_data = {
        'grant_type': 'authorization_code',
        'client_id': client_id,
        'code': auth_code,
        'redirect_uri': callback_url,
        'code_verifier': code_verifier,
    }

    try:
        response = requests.post(
            token_endpoint,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            data=token_data,
            timeout=10
        )

        if response.status_code != 200:
            print(f"❌ Token exchange failed: {response.status_code}")
            print(response.text)
            return None

        token_response = response.json()
        access_token = token_response.get('access_token')
        id_token = token_response.get('id_token')
        refresh_token = token_response.get('refresh_token')
        expires_in = token_response.get('expires_in')

        print("=" * 70)
        print("✅ AUTHENTICATION SUCCESSFUL!")
        print("=" * 70)
        print()
        print(f"Access Token: {access_token[:30]}...{access_token[-20:]}")
        print(f"Expires in: {expires_in} seconds ({expires_in // 60} minutes)")

        if refresh_token:
            print(f"Refresh Token: {refresh_token[:30]}... (can be used to get new tokens)")

        print()

        # Decode ID token to show user info
        if id_token:
            try:
                # Decode payload (middle part of JWT)
                payload = id_token.split('.')[1]
                # Add padding if needed
                payload += '=' * (4 - len(payload) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(payload))

                print("👤 User Information:")
                if 'email' in decoded:
                    print(f"   Email: {decoded['email']}")
                if 'name' in decoded:
                    print(f"   Name: {decoded['name']}")
                if 'sub' in decoded:
                    print(f"   User ID: {decoded['sub']}")
                print()
            except Exception as e:
                print(f"   (Could not decode ID token: {e})")
                print()

        # Save tokens to files in /tmp
        token_file = Path("/tmp/user-token.txt")
        token_file.write_text(access_token)
        print(f"💾 Access token saved to: {token_file}")

        if refresh_token:
            refresh_file = Path("/tmp/refresh-token.txt")
            refresh_file.write_text(refresh_token)
            print(f"💾 Refresh token saved to: {refresh_file}")

        print()

        return access_token

    except requests.RequestException as e:
        print(f"❌ Token exchange failed: {e}")
        return None


def main():
    print()
    print("=" * 70)
    print("OIDC User Token Generator")
    print("Authorization Code Flow with PKCE (Claude Desktop compatible)")
    print("=" * 70)
    print()

    # Load config
    try:
        config = load_oidc_config()
    except FileNotFoundError as e:
        print(f"❌ Error: {e}")
        print()
        print("If you're using Auth0, you can run:")
        print("  python bin/setup-auth0.py --token YOUR_AUTH0_MGMT_TOKEN")
        return 1

    audience = config.get("audience")

    # Check for test_client (SPA/Native client for test harness)
    test_client_config = config.get("test_client", {})
    client_id = test_client_config.get("client_id") or config.get("client_id") or config.get("clientId")

    try:
        issuer, authorization_endpoint, token_endpoint = resolve_oidc_endpoints(config)
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    if not all([client_id, audience]):
        print("❌ Incomplete OIDC configuration")
        print(f"   Issuer: {issuer}")
        print(f"   Client ID: {client_id}")
        print(f"   Audience: {audience}")
        print()
        print("Ensure your OIDC client and audience are configured.")
        return 1

    print(f"Issuer: {issuer}")
    print(f"Audience: {audience}")
    print(f"Client ID: {client_id[:20]}...")
    print()

    # Get token
    token = get_user_token_pkce(
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        client_id=client_id,
        audience=audience,
        callback_port=8888,
        scope="openid profile email mcp:read mcp:write"
    )

    if token:
        print()
        print("🎉 You can now test with MCP Inspector:")
        print()
        print("  ./test-inspector.py --transport http \\")
        print("    --url https://your-mcp.example.com \\")
        print("    --token-file /tmp/user-token.txt")
        print()
        print("Or use curl:")
        print()
        print("  curl -H 'Authorization: Bearer $(cat /tmp/user-token.txt)' \\")
        print("    https://your-mcp.example.com/mcp")
        print()
        return 0
    else:
        print()
        print("❌ Failed to get user token")
        print()
        print("Common issues:")
        print("  - Client doesn't have 'Authorization Code' grant type enabled")
        print("  - Callback URL http://localhost:8888/callback not in allowed callbacks")
        print("  - User not allowed for this application (connection not enabled)")
        print("  - User cancelled authentication")
        print()
        print("To fix, re-run your IdP setup for the test client.")
        print()
        return 1


if __name__ == "__main__":
    exit(main())
