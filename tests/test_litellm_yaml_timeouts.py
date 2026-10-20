"""Tests for proxy/litellm.yaml per-deployment timeout/retry settings (#133).

Two layers:
- Pure YAML checks (run everywhere, including CI, which installs pytest and pyyaml but
  not litellm): every deployment carries stream_timeout, timeout and num_retries
  with in-range values.
- LiteLLM Router checks (skipped when litellm is not importable; they run against the
  same installed LiteLLM 1.104.2 that serves the proxy, e.g. with the
  ~/.claude-lumo/litellm-venv site-packages on PYTHONPATH): the keys parse and pass
  through to Router deployment lookups.
"""

import importlib.util
import os

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YAML_PATH = os.path.join(ROOT, "proxy", "litellm.yaml")

EXPECTED_DEPLOYMENTS = 4  # lumo-lite, *haiku*, lumo-max, *
SPEC_RANGES = {
    "stream_timeout": (60, 120),  # stream call timeout, seconds
    "timeout": (30, 60),  # whole non-streaming call, seconds
    "num_retries": (1, 2),  # router retries per deployment
}

HAS_LITELLM = importlib.util.find_spec("litellm") is not None

requires_litellm = pytest.mark.skipif(
    not HAS_LITELLM, reason="litellm not installed (CI installs only pytest and pyyaml)"
)


def _config():
    with open(YAML_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_every_deployment_carries_the_timeout_and_retry_keys():
    model_list = _config()["model_list"]
    assert len(model_list) == EXPECTED_DEPLOYMENTS
    for entry in model_list:
        params = entry["litellm_params"]
        for key in SPEC_RANGES:
            assert key in params, f"deployment {entry['model_name']!r} missing {key}"


def test_key_values_are_within_the_spec_ranges():
    for entry in _config()["model_list"]:
        params = entry["litellm_params"]
        for key, (low, high) in SPEC_RANGES.items():
            value = params[key]
            assert isinstance(value, int), f"{key} should be an int, got {value!r}"
            assert low <= value <= high, f"{key}={value} outside [{low}, {high}]"


def test_all_deployments_share_the_same_settings():
    """One value per key across deployments keeps behaviour predictable."""
    for key in SPEC_RANGES:
        values = {entry["litellm_params"][key] for entry in _config()["model_list"]}
        assert len(values) == 1, f"{key} differs across deployments: {values}"


def test_deployment_timeouts_sit_below_request_timeout():
    """stream_timeout > timeout (a streamed call waits longer than a plain one), and
    both stay far under litellm_settings.request_timeout so the per-deployment
    values are what actually fires (#133)."""
    config = _config()
    first = config["model_list"][0]["litellm_params"]
    assert first["stream_timeout"] > first["timeout"]
    assert first["stream_timeout"] < config["litellm_settings"]["request_timeout"]


@requires_litellm
def test_router_init_accepts_the_new_keys():
    """Router initialization must not reject the per-deployment settings."""
    from litellm import Router

    router = Router(model_list=_config()["model_list"], disable_cooldowns=True)
    assert len(router.model_list) == EXPECTED_DEPLOYMENTS


@requires_litellm
def test_router_sees_the_deployment_timeouts():
    """Router._get_timeout reads the deployment's litellm_params (Router
    _get_stream_timeout / _get_non_stream_timeout): streamed calls resolve to
    stream_timeout, plain calls to timeout."""
    from litellm import Router

    router = Router(model_list=_config()["model_list"], disable_cooldowns=True)
    deployments = router.get_model_list()
    assert len(deployments) == EXPECTED_DEPLOYMENTS
    for deployment in deployments:
        data = deployment["litellm_params"]
        assert router._get_timeout({}, data) == data["timeout"]
        assert router._get_timeout({"stream": True}, data) == data["stream_timeout"]


@requires_litellm
def test_router_keeps_the_deployment_num_retries():
    """Per-deployment num_retries survives into the deployment dicts the retry
    logic reads (Router._set_deployment_num_retries_on_exception)."""
    from litellm import Router

    router = Router(model_list=_config()["model_list"], disable_cooldowns=True)
    for deployment in router.get_model_list():
        assert deployment["litellm_params"].get("num_retries") in (1, 2)
