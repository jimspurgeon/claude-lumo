"""LiteLLM custom auth: no or wrong key gets a clean 401, not a 500 (issue #202).

With ``master_key`` configured, a request without an Authorization header reaches
LiteLLM's ``_user_api_key_auth_builder`` with ``api_key=None`` (every APIKeyHeader
scheme in the proxy uses ``auto_error=False``) and the builder raises a bare
``Exception("No api key passed in.")``. FastAPI's generic Exception handler turns
that into 500 "Internal Server Error" (LiteLLM 1.104.2,
litellm.proxy.auth.user_api_key_auth.py:1867 and litellm.proxy.proxy_server.py:1999). A keyless
model-list probe (DeepSeek Harness's "Fetch available models" step) reads that 500
as a broken gateway.

This module replaces the native key check (``general_settings.custom_auth`` in
gateway/litellm.yaml; LiteLLM loads it from the yaml's directory, the same
mechanism as the ``lumo_websearch`` callback). Three outcomes:

- no Authorization header, or an empty/blank bearer value -> 401
- a key that is not the master key -> 401 (native code answered 400 here,
  because with no database connected a non-master key falls into the
  "No connected db." ProxyException; a 401 is the truthful answer)
- the master key -> the same UserAPIKeyAuth the native path builds (PROXY_ADMIN
  role, the ``litellm_proxy_master_key`` alias standing in for the raw key,
  user_id ``default_user_id``), so valid-key requests behave as before.

When ``master_key`` is not configured at all, LiteLLM's native path accepts
every request as an internal user; the hook preserves that rather than locking
the gateway, but the shipped config always sets ``master_key``.

Side effect to know about: when ``custom_auth`` is set, LiteLLM skips its
common_checks authorization gate unless ``custom_auth_run_common_checks`` is
also set (user_api_key_auth.py:2742). This gateway has no database, no virtual
keys, no teams or budgets, only the single master key, so that gate has nothing
to enforce; the key comparison in this module is the whole policy. Valid-key
behavior is otherwise unchanged (gateway/models-auth.test.mjs locks it in).

The pure functions below have no LiteLLM or FastAPI dependency, so they are
unit-testable without the proxy installed (tests/test_litellm_yaml_model_info.py).
"""

from __future__ import annotations

import secrets
from typing import Any

# Mirror of litellm.constants.LITELLM_PROXY_MASTER_KEY_ALIAS: substituting a
# stable alias for the raw master key keeps the real key out of spend logs and
# metrics labels, exactly like the native path (constants.py:1677). A test
# cross-checks this against the installed constant when litellm is importable.
MASTER_KEY_ALIAS = "litellm_proxy_master_key"

DEFAULT_401_DETAIL = (
    "Missing or invalid API key. Send the gateway master key as the bearer "
    "credential (Authorization: Bearer <key>)."
)


class AuthRejected(Exception):
    """Raised when the key is missing or does not match the master key."""

    def __init__(self, detail: str = DEFAULT_401_DETAIL) -> None:
        super().__init__(detail)
        self.detail = detail


def is_master_key(api_key: Any, master_key: str | None) -> bool:
    """True only when api_key is exactly the configured master key.

    Constant-time, and never true for a missing key or a missing master key.
    Non-str types are screened out first, and compare_digest runs inside the
    same catch-all the native path uses (user_api_key_auth.py:2052): headers
    arrive latin-1-decoded, so a bearer key like ``caf\xe9`` reaches
    compare_digest as non-ASCII text, which raises TypeError against the
    ASCII master key — anything escaping here would surface as a 500, the
    exact bug class this hook exists to kill, so every failure mode rejects."""
    if not isinstance(api_key, str) or not isinstance(master_key, str):
        return False
    try:
        return secrets.compare_digest(api_key, master_key)
    except Exception:
        return False


def check_key(api_key: Any, master_key: str | None) -> None:
    """Decide auth against one master key. Raises AuthRejected on failure.

    With no master key configured, LiteLLM's native path accepts every request
    as an internal user; this preserves that behavior instead of bricking the
    gateway."""
    if master_key is None:
        return
    if not is_master_key(api_key, master_key):
        raise AuthRejected()


def master_key_token(parent_otel_span: Any = None) -> Any:
    """The UserAPIKeyAuth the native path builds for the master key.

    Needs litellm importable, so only called at request time, never from unit
    tests."""
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

    token = UserAPIKeyAuth.model_validate({
        "api_key": MASTER_KEY_ALIAS,
        "user_role": LitellmUserRoles.PROXY_ADMIN,
        "user_id": "default_user_id",  # litellm.constants.LITELLM_PROXY_ADMIN_NAME
        "parent_otel_span": parent_otel_span,
    })
    token.via_virtual_key = True  # matches the native master-key path
    return token


# ------------------------------------------------------------- LiteLLM glue
# The custom-auth entry point LiteLLM calls (signature per LiteLLM's own
# example_config_yaml/custom_auth.py): async def user_api_key_auth(request,
# api_key) -> UserAPIKeyAuth. It must either return a token or raise; LiteLLM
# 1.104.2 validates the returned value unconditionally, so there is no way to
# decline and fall back to native auth.


async def user_api_key_auth(request: Any, api_key: str | None) -> Any:
    from litellm.proxy import proxy_server

    try:
        check_key(api_key, proxy_server.master_key)
    except AuthRejected as e:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail=e.detail) from e
    if proxy_server.master_key is None:
        from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

        return UserAPIKeyAuth(user_role=LitellmUserRoles.INTERNAL_USER)
    return master_key_token(getattr(request.state, "parent_otel_span", None))
