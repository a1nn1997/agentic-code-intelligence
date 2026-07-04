"""The gateway: the ONE module that binds an HTTP port.

Every other module is an in-process library reached only through the gateway,
which is how the "API-only; index/sandbox/model unreachable by consumers"
requirement holds structurally — there is no other listener to reach. Phase 0
exposes only ``/healthz``, ``/readyz``, and a ``/metrics`` stub; task/auth
endpoints arrive in Phase 6.
"""

from acp.gateway.app import create_app

__all__ = ["create_app"]
