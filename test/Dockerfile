# cnpg-mcp MCP Server - Test Container Dockerfile
#
# Thin layer over the production image. The test server reuses the exact same
# Python environment and source tree; only the CMD differs:
#   - launches cnpg_mcp_test_server.py instead of the production server
#   - --no-auth disables OIDC authentication
#   - --identity supplies the mock sub / iss claims that downstream tools
#     (mcp_context, user_hash) require
#
# Built by `make build-test` after `make build`. The Makefile passes the main
# image tag via --build-arg BASE_IMAGE so the FROM line resolves correctly.
#
# Because the test surface has no OIDC, the deployment chart MUST keep this
# container reachable only from inside the cluster (ClusterIP) or via
# `kubectl port-forward` — never via Ingress.

# Default is a placeholder so `docker build` without --build-arg still parses
# cleanly (silences BuildKit's InvalidDefaultArgInFrom warning). The Makefile
# always overrides BASE_IMAGE with the freshly-built main image tag.
ARG BASE_IMAGE=cnpg_mcp-server:latest
FROM ${BASE_IMAGE}

LABEL component="test-sidecar"
LABEL description=" - Test Server (no-auth, in-cluster only)"

# The production image deliberately omits the test server entrypoint to keep
# the production attack surface clean. Add it here for the test image only.
USER root
COPY src/cnpg_mcp_test_server.py /app/cnpg_mcp_test_server.py
RUN chown mcpuser:mcpuser /app/cnpg_mcp_test_server.py
USER mcpuser

EXPOSE 4201

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:4201/healthz || exit 1

CMD ["python", "cnpg_mcp_test_server.py", \
     "--no-auth", \
     "--identity", "test-user", \
     "--host", "0.0.0.0", \
     "--port", "4201"]