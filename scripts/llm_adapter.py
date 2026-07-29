#!/usr/bin/env python3
"""Unified LLM adapter for HK/US v1.2.6 writers.

The adapter is intentionally provider-neutral: model names do not determine the
transport protocol. Callers provide CLI/env config and receive structured
success or failure details instead of a swallowed ("", False) result.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import Any


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    api_format: str = "auto"
    timeout: int = 180
    source: str = ""
    endpoint: str = ""
    credential_source: str = ""
    base_url_source: str = ""
    model_source: str = ""
    format_source: str = ""
    error_type: str = ""
    error_message: str = ""
    missing_fields: list[str] | None = None
    searched_sources: list[str] | None = None
    candidate_summaries: list[dict[str, str]] | None = None


@dataclass
class LLMResult:
    ok: bool
    text: str
    error_type: str
    error_message: str
    http_status: int | None
    latency_s: float
    attempt_count: int
    model: str
    api_format: str
    endpoint: str


def _arg_value(args: Any, name: str) -> Any:
    if args is None:
        return None
    return getattr(args, name, None)


def _env(environ: dict[str, str], *names: str) -> tuple[str, str]:
    for name in names:
        value = environ.get(name, "")
        if value:
            return value, name
    return "", ""


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _skill_root() -> Path:
    return _script_dir().parent


def _project_root() -> Path:
    # company-onepager-eval/skills/v1.2.6/scripts -> company-onepager-eval
    try:
        return _script_dir().parents[2]
    except IndexError:
        return Path.cwd()


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return data
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                data[key] = value
    except OSError:
        return {}
    return data


def _candidate_env_files(environ: dict[str, str]) -> list[tuple[Path, str]]:
    home = Path(environ.get("HOME") or os.path.expanduser("~"))
    return [
        (_project_root() / ".env", ".env:project"),
        (_skill_root() / ".env", ".env:skill"),
        (_script_dir() / ".env", ".env:scripts"),
        (home / ".env", ".env:home"),
    ]


def _load_env_layers(environ: dict[str, str]) -> list[tuple[dict[str, str], str]]:
    layers = []
    for path, label in _candidate_env_files(environ):
        values = _parse_env_file(path)
        if values:
            layers.append((values, label))
    return layers


def _candidate_model_files(environ: dict[str, str]) -> list[tuple[Path, str]]:
    paths: list[tuple[Path, str]] = []
    for env_name in ("LLM_MODELS_CONFIG", "MODELS_CONFIG_PATH"):
        value = environ.get(env_name, "")
        if value:
            paths.append((Path(value).expanduser(), "platform_models_json"))
    home = Path(environ.get("HOME") or os.path.expanduser("~"))
    paths.extend([
        (home / ".datayesclaw" / "agents" / "main" / "agent" / "models.json", "platform_models_json"),
        (home / ".openclaw" / "agents" / "main" / "agent" / "models.json", "platform_models_json"),
        (home / ".qoder" / "agents" / "main" / "agent" / "models.json", "platform_models_json"),
        (home / ".qoder" / "models.json", "platform_models_json"),
        (home / ".workbuddy" / "agents" / "main" / "agent" / "models.json", "platform_models_json"),
        (home / ".workbuddy" / "models.json", "platform_models_json"),
        (home / ".codex" / "models.json", "platform_models_json"),
        (home / ".config" / "codex" / "models.json", "platform_models_json"),
        (home / ".claude" / "models.json", "platform_models_json"),
        (home / ".config" / "claude" / "models.json", "platform_models_json"),
        (_project_root() / "models.json", "platform_models_json"),
        (_skill_root() / "models.json", "platform_models_json"),
        (_script_dir() / "models.json", "platform_models_json"),
    ])
    seen = set()
    out = []
    for path, label in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append((path, label))
    return out


def _pick_any(obj: dict, names: tuple[str, ...]) -> str:
    for name in names:
        value = obj.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _candidate_from_obj(obj: dict, source: str, provider_id: str = "") -> dict[str, Any]:
    provider = _pick_any(obj, ("provider", "provider_id", "providerId")) or provider_id
    return {
        "provider_id": provider,
        "api_key": _pick_any(obj, ("api_key", "apiKey", "key", "token")),
        "base_url": _pick_any(obj, ("base_url", "baseUrl", "endpoint")),
        "model_id": _pick_any(obj, ("model", "model_id", "modelId")),
        "api_format": (_pick_any(obj, ("api_format", "apiFormat", "protocol")) or "auto").lower(),
        "source": source,
        "active": bool(obj.get("active") or obj.get("default") or obj.get("selected")),
    }


def _extract_model_candidates(payload: Any, source: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                candidates.append(_candidate_from_obj(item, source))
        return candidates
    if not isinstance(payload, dict):
        return candidates
    candidates.append(_candidate_from_obj(payload, source))
    for key in ("models", "providers", "configs", "model_configs", "modelConfigs"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    candidates.append(_candidate_from_obj(item, source))
        elif isinstance(value, dict):
            for provider_id, item in value.items():
                if isinstance(item, dict):
                    candidates.append(_candidate_from_obj(item, source, str(provider_id)))
    for provider_id, value in payload.items():
        if isinstance(value, dict) and provider_id not in {"models", "providers", "configs"}:
            cand = _candidate_from_obj(value, source, str(provider_id))
            if cand.get("api_key") or cand.get("base_url") or cand.get("model_id"):
                candidates.append(cand)
    return [c for c in candidates if c.get("api_key") or c.get("base_url") or c.get("model_id")]


def discover_platform_model_configs(environ: dict[str, str] | None = None) -> list[dict]:
    environ = environ if environ is not None else os.environ
    configs: list[dict] = []
    for path, label in _candidate_model_files(environ):
        if not path.exists() or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        configs.extend(_extract_model_candidates(payload, label))
    return configs


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, str]:
    host = ""
    try:
        host = urlparse(candidate.get("base_url", "")).hostname or ""
    except Exception:
        host = ""
    return {
        "provider_id": str(candidate.get("provider_id", "")),
        "model_id": str(candidate.get("model_id", "")),
        "hostname": host,
        "api_format": str(candidate.get("api_format", "")),
        "source": str(candidate.get("source", "")),
    }


def _strip_known_suffix(base_url: str, suffixes: tuple[str, ...]) -> str:
    url = (base_url or "").strip().rstrip("/")
    lower = url.lower()
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if lower.endswith(suffix):
                url = url[: -len(suffix)].rstrip("/")
                lower = url.lower()
                changed = True
                break
    return url


def _normalize_openai_endpoint(base_url: str) -> str:
    root = _strip_known_suffix(base_url, ("/v1/chat/completions", "/chat/completions", "/v1/messages", "/messages", "/v1"))
    return root.rstrip("/") + "/v1/chat/completions"


def _normalize_anthropic_endpoint(base_url: str) -> str:
    root = _strip_known_suffix(base_url, ("/v1/messages", "/messages", "/v1/chat/completions", "/chat/completions", "/v1"))
    return root.rstrip("/") + "/v1/messages"


def _resolve_format(requested: str, base_url: str, sources: set[str]) -> tuple[str, str, str]:
    requested = (requested or "auto").lower()
    if requested in {"openai", "anthropic"}:
        return requested, "", ""
    if requested != "auto":
        return "", "CONFIG_INVALID_FORMAT", f"unsupported llm format: {requested}"
    if any(s.startswith("ANTHROPIC_") for s in sources) and not any(s.startswith(("OPENAI_", "CODEX_", "LLM_")) for s in sources):
        return "anthropic", "", ""
    if any(s.startswith(("OPENAI_", "CODEX_", "LLM_")) for s in sources):
        return "openai", "", ""
    lower = (base_url or "").lower()
    if "/v1/messages" in lower or lower.endswith("/messages"):
        return "anthropic", "", ""
    return "", "CONFIG_AMBIGUOUS", "api_format=auto but provider cannot be determined"


def _env_lookup(layers: list[tuple[dict[str, str], str]], names: tuple[str, ...]) -> tuple[str, str]:
    for values, label in layers:
        for name in names:
            value = values.get(name, "")
            if value:
                return value, f"{label}:{name}"
    return "", ""


def _select_model_candidate(candidates: list[dict[str, Any]], model_hint: str = "",
                            format_hint: str = "") -> tuple[dict[str, Any] | None, str, list[dict[str, str]]]:
    summaries = [_candidate_summary(c) for c in candidates]
    if not candidates:
        return None, "", summaries
    complete = [c for c in candidates if c.get("api_key") and c.get("base_url") and c.get("model_id")]
    if model_hint:
        matches = [c for c in candidates if c.get("model_id") == model_hint]
        if len(matches) == 1:
            return matches[0], "", summaries
        if len(matches) > 1:
            return None, "CONFIG_AMBIGUOUS", [_candidate_summary(c) for c in matches]
    if format_hint in {"openai", "anthropic"}:
        matches = [c for c in candidates if c.get("api_format") in {format_hint, "auto", ""}]
        complete_matches = [c for c in matches if c.get("api_key") and c.get("base_url") and c.get("model_id")]
        if len(complete_matches) == 1:
            return complete_matches[0], "", summaries
    active = [c for c in complete if c.get("active")]
    if len(active) == 1:
        return active[0], "", summaries
    if len(complete) == 1:
        return complete[0], "", summaries
    if len(complete) > 1:
        return None, "CONFIG_AMBIGUOUS", summaries
    return candidates[0], "", summaries


def _source_provider_sources(*sources: str) -> set[str]:
    out = set()
    for source in sources:
        if not source:
            continue
        token = source.rsplit(":", 1)[-1]
        out.add(token)
    return out


def _default_base_url_for_provider(sources: set[str], fmt: str) -> tuple[str, str]:
    if fmt == "openai" and any(s.startswith("OPENAI_") for s in sources):
        return "https://api.openai.com", "provider_default"
    if fmt == "anthropic" and any(s.startswith("ANTHROPIC_") for s in sources):
        return "https://api.anthropic.com", "provider_default"
    return "", ""


def resolve_llm_config(args: Any = None, environ: dict[str, str] | None = None) -> LLMConfig:
    environ = environ if environ is not None else os.environ
    searched_sources = ["cli", "environment", ".env", "platform_models_json"]
    env_layers = [(environ, "environment")] + _load_env_layers(environ)

    api_key = _arg_value(args, "llm_api_key") or ""
    key_source = "cli:llm_api_key" if api_key else ""
    if not api_key:
        api_key, key_source = _env_lookup(env_layers, (
            "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN", "OPENAI_ACCESS_TOKEN",
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
            "CUSTOM_API_KEY", "OPENCLAW_API_KEY", "CODEX_API_KEY", "CODEX_AUTH_TOKEN",
        ))

    base_url = _arg_value(args, "llm_base_url") or ""
    base_source = "cli:llm_base_url" if base_url else ""
    if not base_url:
        base_url, base_source = _env_lookup(env_layers, (
            "LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ENDPOINT",
            "ANTHROPIC_BASE_URL", "CUSTOM_BASE_URL", "CODEX_BASE_URL", "CODEX_OPENAI_BASE_URL",
        ))

    model = _arg_value(args, "llm_model") or ""
    model_source = "cli:llm_model" if model else ""
    if not model:
        model, model_source = _env_lookup(env_layers, (
            "LLM_MODEL", "OPENAI_MODEL", "OPENAI_MODEL_NAME", "ANTHROPIC_MODEL", "MODEL_NAME", "MODEL_ID",
        ))

    requested_format = (_arg_value(args, "llm_format") or "")
    format_source = "cli:llm_format" if requested_format else ""
    if not requested_format:
        requested_format, format_source = _env_lookup(env_layers, ("LLM_FORMAT", "OPENAI_API_FORMAT", "ANTHROPIC_API_FORMAT"))
    requested_format = (requested_format or "auto").lower()
    if not format_source:
        format_source = "default:auto"

    timeout_raw = _arg_value(args, "llm_timeout") or environ.get("LLM_TIMEOUT") or 180
    try:
        timeout = int(timeout_raw)
    except Exception:
        timeout = 180

    candidates = discover_platform_model_configs(environ)
    selected, select_error, summaries = _select_model_candidate(
        candidates,
        model_hint=model,
        format_hint=requested_format if requested_format in {"openai", "anthropic"} else "",
    )
    if selected:
        if not api_key and selected.get("api_key"):
            api_key, key_source = selected["api_key"], "platform_models_json"
        if not base_url and selected.get("base_url"):
            base_url, base_source = selected["base_url"], "platform_models_json"
        if not model and selected.get("model_id"):
            model, model_source = selected["model_id"], "platform_models_json"
        if requested_format == "auto" and selected.get("api_format") in {"openai", "anthropic"}:
            requested_format, format_source = selected["api_format"], "platform_models_json"

    provider_sources = _source_provider_sources(key_source, base_source, model_source, format_source)
    cfg = LLMConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        api_format=requested_format,
        timeout=timeout,
        source=key_source,
        credential_source=key_source,
        base_url_source=base_source,
        model_source=model_source,
        format_source=format_source,
        searched_sources=searched_sources,
        candidate_summaries=summaries,
    )
    if select_error and (not api_key or not base_url or not model):
        cfg.error_type = select_error
        cfg.error_message = "multiple platform model candidates cannot be disambiguated"
        return cfg

    fmt, err_type, err_msg = _resolve_format(requested_format, base_url, provider_sources)
    if err_type and requested_format == "auto" and not base_url:
        fmt = ""
    elif err_type:
        cfg.error_type = err_type
        cfg.error_message = err_msg
        return cfg

    if not base_url and fmt:
        default_url, default_source = _default_base_url_for_provider(provider_sources, fmt)
        if default_url:
            base_url, base_source = default_url, default_source
            cfg.base_url = base_url
            cfg.base_url_source = base_source

    missing = [name for name, value in (("api_key", api_key), ("base_url", base_url), ("model", model)) if not value]
    if missing:
        cfg.error_type = "LLM_CONFIG_MISSING"
        cfg.missing_fields = missing
        cfg.error_message = "missing " + ",".join(missing)
        return cfg

    if not fmt:
        fmt, err_type, err_msg = _resolve_format(requested_format, base_url, provider_sources)
        if err_type:
            cfg.error_type = err_type
            cfg.error_message = err_msg
            return cfg
    cfg.api_format = fmt
    cfg.endpoint = _normalize_openai_endpoint(base_url) if fmt == "openai" else _normalize_anthropic_endpoint(base_url)
    return cfg


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("text", "output_text") and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        return "".join(parts)
    return ""


def _parse_openai_response(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return _content_to_text(message.get("content"))


def _parse_anthropic_response(payload: dict) -> str:
    parts = []
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _should_retry(error_type: str, status: int | None) -> bool:
    if status in {429, 502, 503, 504}:
        return True
    if error_type in {"NETWORK_TEMPORARY", "EMPTY_TEXT_RESPONSE"}:
        return True
    return False


def _redact_secret(text: str, config: LLMConfig) -> str:
    redacted = text or ""
    if config.api_key:
        redacted = redacted.replace(config.api_key, "[REDACTED_API_KEY]")
    return redacted[:500]


def call_llm(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 8000,
    timeout: int | None = None,
    max_attempts: int = 2,
    config: LLMConfig,
) -> LLMResult:
    started = time.time()
    if config.error_type or not config.api_key or not config.endpoint or not config.model:
        return LLMResult(False, "", config.error_type or "LLM_CONFIG_MISSING", config.error_message or "invalid llm config", None, 0.0, 0, config.model, config.api_format, config.endpoint)

    # Writer v1.2.5 r11f splits large prompts into smaller tasks. Keep transport
    # retry bounded: the same HTTP-scale prompt should not repeat three times on
    # 504/temporary network errors. JSON repair is handled by the writer's
    # schema-aware small-task retry, not by another full HTTP attempt here.
    max_attempts = max(1, min(int(max_attempts or 1), 2))
    attempts = [(max_tokens, timeout or config.timeout) for _ in range(max_attempts)]
    last_error = ""
    last_type = ""
    last_status: int | None = None
    headers = {"Content-Type": "application/json"}
    if config.api_format == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if config.api_key.startswith("sk-ant-"):
            headers["x-api-key"] = config.api_key
        else:
            headers["Authorization"] = f"Bearer {config.api_key}"
    else:
        headers["Authorization"] = f"Bearer {config.api_key}"

    for idx, (mt, to) in enumerate(attempts, start=1):
        if config.api_format == "anthropic":
            body: dict[str, Any] = {"model": config.model, "max_tokens": mt, "thinking": {"type": "disabled"}, "messages": [{"role": "user", "content": prompt}]}
            if system:
                body["system"] = system
        else:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            body = {"model": config.model, "max_tokens": mt, "messages": messages}
        try:
            req = urllib.request.Request(config.endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=to) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            text = _parse_anthropic_response(payload) if config.api_format == "anthropic" else _parse_openai_response(payload)
            if text.strip():
                return LLMResult(True, text, "", "", None, round(time.time() - started, 3), idx, config.model, config.api_format, config.endpoint)
            import sys as _sys
            _content = payload.get("content")
            _ct = type(_content).__name__
            _dump = str(payload)[:800]
            _sys.stderr.write(f"[DEBUG adapter EMPTY_TEXT_RESPONSE] content_type={_ct} content_repr={repr(_content)[:400]} payload_keys={list(payload.keys())[:20]}\n")
            _sys.stderr.write(f"[DEBUG adapter EMPTY_TEXT_RESPONSE] payload[:800]={_dump}\n")
            _sys.stderr.flush()
            last_type, last_error, last_status = "EMPTY_TEXT_RESPONSE", "response has no text blocks", None
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            try:
                detail = _redact_secret(exc.read().decode("utf-8", errors="replace"), config)
            except Exception:
                detail = _redact_secret(str(exc), config)
            last_type = "HTTP_ERROR"
            last_error = f"http {exc.code}: {detail}"
        except (TimeoutError, ConnectionError, OSError) as exc:
            last_status = None
            last_type = "NETWORK_TEMPORARY"
            last_error = _redact_secret(str(exc), config)
        except Exception as exc:
            last_status = None
            last_type = "REQUEST_FAILED"
            last_error = _redact_secret(str(exc), config)
        if idx >= len(attempts) or not _should_retry(last_type, last_status):
            break
        time.sleep(min(2 * idx, 5))
    return LLMResult(False, "", last_type or "REQUEST_FAILED", last_error, last_status, round(time.time() - started, 3), idx, config.model, config.api_format, config.endpoint)


def config_for_diagnostics(config: LLMConfig) -> dict[str, Any]:
    data = asdict(config)
    data.pop("api_key", None)
    data["credential_source"] = config.source
    data["credential_present"] = bool(config.api_key)
    data["config_resolution"] = {
        "ok": not bool(config.error_type),
        "error_type": config.error_type,
        "missing_fields": config.missing_fields or [],
        "searched_sources": config.searched_sources or ["cli", "environment", ".env", "platform_models_json"],
        "candidate_summaries": config.candidate_summaries or [],
    }
    return data
