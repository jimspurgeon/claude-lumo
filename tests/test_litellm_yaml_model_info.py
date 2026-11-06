"""Tests for gateway/litellm.yaml per-model metadata and the #202 custom auth hook.

Two layers:
- Pure checks (run everywhere, including CI, which installs pytest and pyyaml but
  not litellm): the yaml carries the model_info token limits for lumo-max and
  lumo-lite, and gateway/lumo_models_auth.py makes the right decision for every
  key combination without importing litellm.
- LiteLLM checks (skipped when litellm is not importable; they run against the
  same installed LiteLLM 1.104.2 that serves the proxy): the MASTER_KEY_ALIAS
  mirror matches the installed constant, and the yaml's model_info merges
  through Router.get_model_listing_info into what /v1/models reports.

The live end-to-end proof (no key -> 401, not 500) is
gateway/models-auth.test.mjs, which boots a real LiteLLM on a test port with
the shipped hook.
"""

import importlib.util
import os

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAML_PATH = os.path.join(ROOT, "gateway", "litellm.yaml")
AUTH_PATH = os.path.join(ROOT, "gateway", "lumo_models_auth.py")

# The Lumo context window: 131k tokens shared by prompt and reply, replies up
# to 32k (same on both hosted tiers; see docs/GATEWAY.md).
MAX_INPUT_TOKENS = 131072
MAX_OUTPUT_TOKENS = 32768
MODELS_WITH_INFO = ("lumo-max", "lumo-lite")

HAS_LITELLM = importlib.util.find_spec("litellm") is not None

requires_litellm = pytest.mark.skipif(
    not HAS_LITELLM, reason="litellm not installed (CI installs only pytest and pyyaml)"
)


def _config():
    with open(YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _entry(model_name):
    for entry in _config()["model_list"]:
        if entry["model_name"] == model_name:
            return entry
    raise AssertionError(f"no deployment named {model_name!r}")


def _auth_module():
    spec = importlib.util.spec_from_file_location("lumo_models_auth", AUTH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- yaml

def test_hosted_models_carry_token_limits():
    for name in MODELS_WITH_INFO:
        info = _entry(name).get("model_info")
        assert isinstance(info, dict), f"{name} has no model_info block"
        assert info.get("max_input_tokens") == MAX_INPUT_TOKENS, name
        assert info.get("max_output_tokens") == MAX_OUTPUT_TOKENS, name


def test_model_info_keys_exist_in_litellm_types():
    """model_info only reaches /v1/models if the keys are fields LiteLLM reads
    (get_model_listing_info picks max_input_tokens/max_output_tokens from the
    merged model_info). Guarded by requires_litellm below; this pure variant at
    least pins the key names against typos."""
    for name in MODELS_WITH_INFO:
        assert set(_entry(name)["model_info"]) == {"max_input_tokens", "max_output_tokens"}, name


def test_yaml_registers_the_custom_auth_hook():
    settings = _config()["general_settings"]
    assert settings.get("custom_auth") == "lumo_models_auth.user_api_key_auth"


@requires_litellm
def test_router_listing_reports_the_token_limits():
    """The same merge Router.get_model_listing_info performs for /v1/models."""
    from litellm import Router

    router = Router(model_list=_config()["model_list"], disable_cooldowns=True)
    for name in MODELS_WITH_INFO:
        listing = router.get_model_listing_info(name)
        assert listing is not None, f"no listing info for {name}"
        assert listing.max_input_tokens == MAX_INPUT_TOKENS, name
        assert listing.max_output_tokens == MAX_OUTPUT_TOKENS, name


# -------------------------------------------------------------- the hook

@pytest.fixture(scope="module")
def auth():
    return _auth_module()


def test_missing_key_is_rejected(auth):
    with pytest.raises(auth.AuthRejected) as e:
        auth.check_key(None, "sk-real")
    assert e.value.detail


def test_empty_key_is_rejected(auth):
    with pytest.raises(auth.AuthRejected):
        auth.check_key("", "sk-real")


def test_wrong_key_is_rejected(auth):
    with pytest.raises(auth.AuthRejected):
        auth.check_key("sk-attacker", "sk-real")


def test_master_key_passes(auth):
    auth.check_key("sk-real", "sk-real")  # must not raise


def test_no_master_key_configured_allows_all(auth):
    """Native LiteLLM accepts everything when master_key is unset; the hook
    must not brick that setup (the shipped config always sets it)."""
    auth.check_key(None, None)
    auth.check_key("anything", None)


def test_blank_prefix_never_passes(auth):
    """A key equal except for one character must fail (compare_digest, not a
    prefix match)."""
    with pytest.raises(auth.AuthRejected):
        auth.check_key("sk-rea", "sk-real")


def test_non_ascii_key_is_rejected_not_raised(auth):
    """Headers arrive latin-1-decoded, so a prober can send a bearer key like
    "caf\xe9"; compare_digest raises TypeError against the ASCII master key,
    and that must become AuthRejected (401), never an escaped 500."""
    with pytest.raises(auth.AuthRejected):
        auth.check_key("caf\xe9", "sk-real")
    assert auth.is_master_key("caf\xe9", "sk-real") is False


@requires_litellm
def test_master_key_alias_matches_litellm_constant(auth):
    """The hook substitutes the alias instead of the raw key, exactly like the
    native path (litellm.constants.LITELLM_PROXY_MASTER_KEY_ALIAS)."""
    from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS

    assert auth.MASTER_KEY_ALIAS == LITELLM_PROXY_MASTER_KEY_ALIAS


@requires_litellm
def test_master_key_token_shape(auth):
    """What the hook returns for a valid key must mirror the native master-key
    token: PROXY_ADMIN role, alias instead of the raw key, default_user_id."""
    from litellm.proxy._types import LitellmUserRoles

    token = auth.master_key_token()
    assert token.user_role == LitellmUserRoles.PROXY_ADMIN
    assert token.api_key == auth.MASTER_KEY_ALIAS
    assert token.user_id == "default_user_id"
    assert token.via_virtual_key is True
