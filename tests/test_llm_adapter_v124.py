import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import llm_adapter_v124 as adapter  # noqa: E402
import hk_us_report_writer_v124 as writer  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _http_error(status, body=""):
    return urllib.error.HTTPError(
        "https://llm.example/v1/chat/completions",
        status,
        "error",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


class LLMAdapterTests(unittest.TestCase):
    def _patch_roots(self, tmp):
        root = Path(tmp)
        return patch.multiple(
            adapter,
            _project_root=lambda: root / "project",
            _skill_root=lambda: root / "skill",
            _script_dir=lambda: root / "skill" / "scripts",
        )

    def test_endpoint_normalization(self):
        self.assertEqual(
            adapter._normalize_openai_endpoint("https://x.test/v1/chat/completions"),
            "https://x.test/v1/chat/completions",
        )
        self.assertEqual(
            adapter._normalize_openai_endpoint("https://x.test/v1"),
            "https://x.test/v1/chat/completions",
        )
        self.assertEqual(
            adapter._normalize_anthropic_endpoint("https://x.test/v1/messages"),
            "https://x.test/v1/messages",
        )

    def test_cli_overrides_env(self):
        cfg = adapter.resolve_llm_config(
            SimpleNamespace(
                llm_api_key="cli-key",
                llm_base_url="https://cli.test",
                llm_model="cli-model",
                llm_format="openai",
                llm_timeout=12,
            ),
            environ={
                "LLM_API_KEY": "env-key",
                "LLM_BASE_URL": "https://env.test",
                "LLM_MODEL": "env-model",
            },
        )
        self.assertEqual(cfg.api_key, "cli-key")
        self.assertEqual(cfg.model, "cli-model")
        self.assertEqual(cfg.api_format, "openai")
        self.assertEqual(cfg.timeout, 12)

    def test_env_complete_config(self):
        cfg = adapter.resolve_llm_config(environ={
            "OPENAI_API_KEY": "env-key",
            "OPENAI_BASE_URL": "https://env.example/v1",
            "OPENAI_MODEL": "env-model",
        })
        self.assertFalse(cfg.error_type)
        self.assertEqual(cfg.credential_source, "environment:OPENAI_API_KEY")
        self.assertEqual(cfg.base_url_source, "environment:OPENAI_BASE_URL")
        self.assertEqual(cfg.model_source, "environment:OPENAI_MODEL")

    def test_env_auto_formats(self):
        openai_cfg = adapter.resolve_llm_config(environ={
            "LLM_API_KEY": "k",
            "LLM_BASE_URL": "https://openai.test",
            "LLM_MODEL": "m",
        })
        self.assertEqual(openai_cfg.api_format, "openai")
        anthropic_cfg = adapter.resolve_llm_config(environ={
            "ANTHROPIC_API_KEY": "k",
            "ANTHROPIC_BASE_URL": "https://anthropic.test",
            "ANTHROPIC_MODEL": "m",
        })
        self.assertEqual(anthropic_cfg.api_format, "anthropic")

    def test_dotenv_complete_config(self):
        with tempfile.TemporaryDirectory() as td, self._patch_roots(td):
            scripts = Path(td) / "skill" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / ".env").write_text(
                "LLM_API_KEY='dotenv-key'\nLLM_BASE_URL=\"https://dotenv.example\"\nLLM_MODEL=dotenv-model\n",
                encoding="utf-8",
            )
            cfg = adapter.resolve_llm_config(environ={"HOME": str(Path(td) / "home")})
        self.assertFalse(cfg.error_type)
        self.assertEqual(cfg.api_key, "dotenv-key")
        self.assertEqual(cfg.credential_source, ".env:scripts:LLM_API_KEY")

    def test_models_json_complete_config_and_aliases(self):
        with tempfile.TemporaryDirectory() as td, self._patch_roots(td):
            model_path = Path(td) / "models.json"
            model_path.write_text(json.dumps({
                "providers": [{
                    "providerId": "p1",
                    "apiKey": "json-key",
                    "endpoint": "https://json.example",
                    "modelId": "json-model",
                    "protocol": "openai",
                    "default": True,
                }]
            }), encoding="utf-8")
            cfg = adapter.resolve_llm_config(environ={
                "HOME": str(Path(td) / "home"),
                "LLM_MODELS_CONFIG": str(model_path),
            })
        self.assertFalse(cfg.error_type)
        self.assertEqual(cfg.api_key, "json-key")
        self.assertEqual(cfg.base_url, "https://json.example")
        self.assertEqual(cfg.model, "json-model")
        self.assertEqual(cfg.api_format, "openai")

    def test_cli_model_can_use_models_json_key_and_base_url(self):
        with tempfile.TemporaryDirectory() as td, self._patch_roots(td):
            model_path = Path(td) / "models.json"
            model_path.write_text(json.dumps([
                {"provider_id": "p1", "api_key": "json-key", "base_url": "https://json.example", "model": "chosen"}
            ]), encoding="utf-8")
            cfg = adapter.resolve_llm_config(
                SimpleNamespace(llm_api_key=None, llm_base_url=None, llm_model="chosen", llm_format="openai", llm_timeout=None),
                environ={"HOME": str(Path(td) / "home"), "LLM_MODELS_CONFIG": str(model_path)},
            )
        self.assertFalse(cfg.error_type)
        self.assertEqual(cfg.model_source, "cli:llm_model")
        self.assertEqual(cfg.credential_source, "platform_models_json")

    def test_env_key_not_overridden_by_models_json(self):
        with tempfile.TemporaryDirectory() as td, self._patch_roots(td):
            model_path = Path(td) / "models.json"
            model_path.write_text(json.dumps([
                {"provider_id": "p1", "api_key": "json-key", "base_url": "https://json.example", "model": "json-model"}
            ]), encoding="utf-8")
            cfg = adapter.resolve_llm_config(environ={
                "HOME": str(Path(td) / "home"),
                "LLM_MODELS_CONFIG": str(model_path),
                "LLM_API_KEY": "env-key",
            })
        self.assertFalse(cfg.error_type)
        self.assertEqual(cfg.api_key, "env-key")
        self.assertEqual(cfg.credential_source, "environment:LLM_API_KEY")

    def test_active_and_single_candidate_selection(self):
        with tempfile.TemporaryDirectory() as td, self._patch_roots(td):
            path1 = Path(td) / "active.json"
            path1.write_text(json.dumps([
                {"provider_id": "a", "api_key": "ka", "base_url": "https://a.example", "model": "ma", "active": True},
                {"provider_id": "b", "api_key": "kb", "base_url": "https://b.example", "model": "mb"},
            ]), encoding="utf-8")
            cfg = adapter.resolve_llm_config(environ={"HOME": str(Path(td) / "home"), "LLM_MODELS_CONFIG": str(path1), "LLM_FORMAT": "openai"})
            self.assertEqual(cfg.model, "ma")
            path2 = Path(td) / "single.json"
            path2.write_text(json.dumps([{"provider_id": "s", "api_key": "ks", "base_url": "https://s.example", "model": "ms"}]), encoding="utf-8")
            cfg2 = adapter.resolve_llm_config(environ={"HOME": str(Path(td) / "home"), "LLM_MODELS_CONFIG": str(path2), "LLM_FORMAT": "openai"})
            self.assertEqual(cfg2.model, "ms")

    def test_multiple_candidates_ambiguous(self):
        with tempfile.TemporaryDirectory() as td, self._patch_roots(td):
            model_path = Path(td) / "models.json"
            model_path.write_text(json.dumps([
                {"provider_id": "a", "api_key": "ka", "base_url": "https://a.example", "model": "ma"},
                {"provider_id": "b", "api_key": "kb", "base_url": "https://b.example", "model": "mb"},
            ]), encoding="utf-8")
            cfg = adapter.resolve_llm_config(environ={"HOME": str(Path(td) / "home"), "LLM_MODELS_CONFIG": str(model_path)})
        self.assertEqual(cfg.error_type, "CONFIG_AMBIGUOUS")
        self.assertTrue(cfg.candidate_summaries)

    def test_does_not_scan_unknown_nested_model_file(self):
        with tempfile.TemporaryDirectory() as td, self._patch_roots(td):
            hidden = Path(td) / "project" / "nested"
            hidden.mkdir(parents=True)
            (hidden / "models.json").write_text(json.dumps({"api_key": "k", "base_url": "https://x", "model": "m"}), encoding="utf-8")
            cfg = adapter.resolve_llm_config(environ={"HOME": str(Path(td) / "home")})
        self.assertEqual(cfg.error_type, "LLM_CONFIG_MISSING")

    def test_auto_does_not_infer_from_model_name(self):
        cfg = adapter.resolve_llm_config(
            SimpleNamespace(llm_api_key=None, llm_base_url=None, llm_model="claude-looking-name", llm_format=None, llm_timeout=None),
            environ={
                "CUSTOM_API_KEY": "k",
                "CUSTOM_BASE_URL": "https://custom.test",
            },
        )
        self.assertEqual(cfg.error_type, "CONFIG_AMBIGUOUS")

    def test_401_no_blind_retry(self):
        cfg = adapter.LLMConfig("secret", "https://x.test", "m", "openai", 2, "test", "https://x.test/v1/chat/completions")
        calls = {"n": 0}

        def fake_urlopen(req, timeout):
            calls["n"] += 1
            raise _http_error(401, "unauthorized")

        with patch("urllib.request.urlopen", fake_urlopen):
            result = adapter.call_llm("p", config=cfg)
        self.assertFalse(result.ok)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result.http_status, 401)

    def test_429_retries_then_success(self):
        cfg = adapter.LLMConfig("secret", "https://x.test", "m", "openai", 2, "test", "https://x.test/v1/chat/completions")
        calls = {"n": 0}

        def fake_urlopen(req, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(429, "rate limited")
            return _Resp({"choices": [{"message": {"content": "ok"}}]})

        with patch("urllib.request.urlopen", fake_urlopen), patch("time.sleep", lambda *_: None):
            result = adapter.call_llm("p", config=cfg)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "ok")
        self.assertEqual(calls["n"], 2)

    def test_empty_response_error(self):
        cfg = adapter.LLMConfig("secret", "https://x.test", "m", "openai", 2, "test", "https://x.test/v1/chat/completions")
        with patch("urllib.request.urlopen", lambda req, timeout: _Resp({"choices": [{"message": {"content": ""}}]})), patch("time.sleep", lambda *_: None):
            result = adapter.call_llm("p", config=cfg)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "EMPTY_TEXT_RESPONSE")

    def test_anthropic_ignores_thinking_blocks(self):
        cfg = adapter.LLMConfig("secret", "https://x.test", "m", "anthropic", 2, "test", "https://x.test/v1/messages")
        payload = {"content": [{"type": "thinking", "text": "hidden"}, {"type": "text", "text": "visible"}]}
        with patch("urllib.request.urlopen", lambda req, timeout: _Resp(payload)):
            result = adapter.call_llm("p", config=cfg)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "visible")

    def test_error_redacts_api_key_and_diagnostics_omit_key(self):
        cfg = adapter.LLMConfig("secret-token", "https://x.test", "m", "openai", 2, "test", "https://x.test/v1/chat/completions")
        with patch("urllib.request.urlopen", lambda req, timeout: (_ for _ in ()).throw(OSError("boom secret-token"))):
            result = adapter.call_llm("p", config=cfg)
        self.assertNotIn("secret-token", result.error_message)
        diag = adapter.config_for_diagnostics(cfg)
        self.assertNotIn("api_key", diag)
        self.assertNotIn("secret-token", json.dumps(diag))
        self.assertEqual(diag["credential_source"], "test")

    def test_startup_missing_config_marks_manifest_failed(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            out.mkdir()
            cfg = adapter.resolve_llm_config(environ={"HOME": str(Path(td) / "home")})
            writer._LLM_CONFIG = cfg
            writer._LLM_DIAGNOSTICS = writer._diagnostics_payload(cfg)
            writer._write_llm_diagnostics(str(out))
            writer._finalize_failed_run(str(out), stage="startup", error_type=cfg.error_type, message=cfg.error_message)
            manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
            diag = json.loads((out / "llm_diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure"]["error_type"], "LLM_CONFIG_MISSING")
        self.assertFalse(diag["config_resolution"]["ok"])

    def test_startup_ambiguous_config_marks_manifest_failed(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            out.mkdir()
            cfg = adapter.LLMConfig(error_type="CONFIG_AMBIGUOUS", error_message="ambiguous", candidate_summaries=[{"provider_id": "a"}])
            writer._finalize_failed_run(str(out), stage="startup", error_type=cfg.error_type, message=cfg.error_message)
            manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["failure"]["error_type"], "CONFIG_AMBIGUOUS")

    def test_normal_config_manifest_can_continue(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            out.mkdir()
            writer._update_run_manifest(str(out), stage="startup", status="running")
            writer._update_run_manifest(str(out), stage="collect_materials", duration_s=1.2)
            manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "running")
        self.assertIn("collect_materials", manifest["completed_stages"])


if __name__ == "__main__":
    unittest.main()
