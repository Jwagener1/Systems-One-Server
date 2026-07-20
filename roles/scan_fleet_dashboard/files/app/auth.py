"""Row-level security: map the caller to the customers they may see.

Returns None for unrestricted access (auth disabled, or admin user).
Full mapping via dbo.customer_login_map lands in Task 12.
"""
import config


def resolve_user(request) -> str | None:
    # TODO: verify the Cloudflare Access JWT instead of trusting the header.
    return request.headers.get("X-Auth-User")


def allowed_customers(q, user, enabled=None, admins=None):
    enabled = config.AUTH_ENABLED if enabled is None else enabled
    if not enabled:
        return None
    raise NotImplementedError("auth mapping implemented in Task 12")
