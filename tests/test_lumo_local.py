"""Tests for lumo-local backend integration (issue #150). Zero-dependency."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest


def run_statusline(stdin, args=("--lumo",), env_extra=None):
    """Run statusline.py as a subprocess with given stdin JSON, return stdout."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_LUMO",)}
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, os.path.join(here, "statusline.py"), *args],
                       input=stdin, env=env, capture_output=True, text=True,
                       encoding="utf-8", check=True)
    return r.stdout


class TestLiteLLMConfig(unittest.TestCase):
    """Verify proxy/litellm.yaml has the lumo-local deployment and correct timeout."""

    def setUp(self):
        self.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.yaml_path = os.path.join(self.repo_root, "proxy", "litellm.yaml")
        with open(self.yaml_path, encoding="utf-8") as f:
            self.content = f.read()

    def test_litellm_yaml_has_lumo_local_deployment(self):
        """lumo-local deployment exists with api_base http://127.0.0.1:8080/v1."""
        self.assertIn("model_name: lumo-local", self.content)
        self.assertIn("http://127.0.0.1:8080/v1", self.content)
        # Must use openai provider for llama.cpp
        self.assertIn("model: openai/", self.content)

    def test_litellm_yaml_lumo_local_has_generous_timeout(self):
        """lumo-local has a generous per-deployment timeout for long local processing."""
        # Multi-line deployment block: find the lumo-local entry, then the next model_name.
        block = re.search(r"- model_name: lumo-local\n(.*?)(?=- model_name:)", self.content, re.DOTALL)
        self.assertIsNotNone(block, "lumo-local deployment should exist in the yaml")
        match = re.search(r"timeout:\s*(\d+)", block.group(1))
        self.assertIsNotNone(match, "lumo-local deployment should have timeout field")
        timeout = int(match.group(1))
        self.assertGreaterEqual(timeout, 600,
                                "lumo-local timeout should be at least 600 seconds for local GPU pressure")

    def test_litellm_yaml_callbacks_register_websearch_hook(self):
        """callbacks still includes lumo_websearch.handler for hosted models."""
        self.assertRegex(self.content, r"callbacks:\s+lumo_websearch\.handler")


class TestSettingsLumoJson(unittest.TestCase):
    """Verify settings-lumo.json exposes lumo-local and clears fallbackModel."""

    def setUp(self):
        self.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.settings_path = os.path.join(self.repo_root, "settings-lumo.json")
        with open(self.settings_path, encoding="utf-8") as f:
            self.content = f.read()
        # Parse after placeholder substitution simulation (just basic JSON validity check)
        self.json_str = self.content
        for placeholder in ["{{LUMO_DIR}}", "{{PYTHON}}", "{{STATE_DIR}}", "{{LUMO_LOCAL}}"]:
            self.json_str = self.json_str.replace(placeholder, "PLACEHOLDER")
        # AUTO_COMPACT_WINDOW is substituted unquoted by render.py, so it must become a number.
        self.json_str = self.json_str.replace("{{AUTO_COMPACT_WINDOW}}", "100000")
        self.settings = json.loads(self.json_str)

    def test_settings_has_fallback_model_empty(self):
        """fallbackModel is an empty array to prevent unintended fallbacks."""
        self.assertIn("fallbackModel", self.settings)
        self.assertEqual([], self.settings["fallbackModel"])

    def test_settings_model_picker_exposes_lumo_local(self):
        """modelPicker options include a lumo-local entry."""
        self.assertIn("modelPicker", self.settings)
        picker = self.settings["modelPicker"]
        self.assertIn("options", picker)
        options = picker["options"]
        self.assertIsInstance(options, list)
        ids = [opt.get("model") for opt in options if isinstance(opt, dict)]
        self.assertIn("lumo-local", ids,
                      "modelPicker should expose lumo-local for /model selection")

    def test_settings_model_picker_lumo_local_has_label(self):
        """lumo-local picker entry has a human-readable label."""
        picker = self.settings.get("modelPicker", {})
        for opt in picker.get("options", []):
            if opt.get("model") == "lumo-local":
                self.assertIn("label", opt)
                self.assertIn("local", opt["label"].lower())
                return
        self.fail("lumo-local option not found in modelPicker")


class TestWebSearchLocalGuard(unittest.TestCase):
    """Verify lumo_websearch skips the local llama.cpp backend (issue #150)."""

    def setUp(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, repo_root)
        from proxy import lumo_websearch
        self.ws = lumo_websearch

    def test_is_local_model_detects_lumo_local(self):
        self.assertTrue(self.ws.is_local_model({"model": "lumo-local"}))

    def test_is_local_model_detects_backend_model_id(self):
        """LiteLLM rewrites data['model'] to the deployment string once routed."""
        self.assertTrue(self.ws.is_local_model({"model": "openai/lfm2.5-24b"}))
        self.assertTrue(self.ws.is_local_model({"model": "lfm2.5-24b"}))

    def test_is_local_model_false_for_hosted_models(self):
        for model in ("lumo-max", "lumo-lite", "claude-opus-5-5", "haiku"):
            self.assertFalse(self.ws.is_local_model({"model": model}), model)

    def test_is_local_model_handles_bad_shapes(self):
        self.assertFalse(self.ws.is_local_model(None))
        self.assertFalse(self.ws.is_local_model({}))
        self.assertFalse(self.ws.is_local_model("not a dict"))

    def test_hooks_guard_on_local_model(self):
        """Both hooks check is_local_model before injecting search blocks."""
        import inspect
        src = inspect.getsource(self.ws.LumoWebSearchBlocks.async_post_call_success_hook)
        self.assertIn("is_local_model", src)
        src = inspect.getsource(self.ws.LumoWebSearchBlocks.async_post_call_streaming_iterator_hook)
        self.assertIn("is_local_model", src)


class TestStatuslineLocalBackend(unittest.TestCase):
    """Verify statusline.py shows 'via local llama.cpp' for lumo-local sessions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"CLAUDE_LUMO_STATE_DIR": self.tmp.name,
                    "CLAUDE_LUMO_UPDATE_CHECK": "0"}  # no real update check from a test (#120)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def status_json(model_id):
        return json.dumps({"model": {"id": model_id, "display_name": model_id},
                            "workspace": {"current_dir": "/tmp"}})

    def test_lumo_local_shows_local_llamacpp(self):
        out = run_statusline(self.status_json("lumo-local"), env_extra=self.env)
        self.assertIn("via local llama.cpp", out)
        self.assertNotIn("via local proxy", out)

    def test_hosted_models_keep_via_local_proxy(self):
        for model in ("lumo-max", "lumo-lite"):
            out = run_statusline(self.status_json(model), env_extra=self.env)
            self.assertIn("via local proxy", out)
            self.assertNotIn("via local llama.cpp", out)


if __name__ == "__main__":
    unittest.main()
