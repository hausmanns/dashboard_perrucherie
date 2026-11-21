"""Pluggable LLM access: a local OpenAI-compatible server first, the cloud as fallback.

Used by the finance documents (payslips, statements, tax bills), which are
private: by default they are read by a model running on this machine.

Providers, tried in the order given by the mode:
- **local**  any OpenAI-compatible server — Ollama (`/v1`), LM Studio,
  llama.cpp server, vLLM… `LOCAL_LLM_BASE_URL` (default: Ollama on the host;
  inside Docker that is `host.docker.internal`), `LOCAL_LLM_MODEL` (default
  `qwen2.5vl:7b`, a vision model good at reading documents), optional
  `LOCAL_LLM_API_KEY`.
- **cloud**  OpenRouter (`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`), the same
  account the rest of the dashboard uses.

Modes: `local_first` (default) · `local_only` (a document never leaves the
house) · `cloud_only`. `allow_cloud=False` turns local_first into local_only.
The finance settings can override URL/model/mode at runtime.

`chat_json()` returns `(data, engine)` where engine says who answered, e.g.
`local:qwen2.5vl:7b` — the review screen shows it.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

import httpx

from . import config


class LLMError(Exception):
    pass


def _in_docker() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("RUNNING_IN_DOCKER") == "1"


def local_defaults() -> dict:
    host = "host.docker.internal" if _in_docker() else "localhost"
    return {
        "base_url": os.environ.get("LOCAL_LLM_BASE_URL") or f"http://{host}:11434/v1",
        "model": os.environ.get("LOCAL_LLM_MODEL") or "qwen2.5vl:7b",
        "api_key": os.environ.get("LOCAL_LLM_API_KEY", ""),
        "timeout": float(os.environ.get("LOCAL_LLM_TIMEOUT", "240")),
    }


def resolve(settings: dict | None = None) -> dict:
    s = (settings or {}).get("llm", {}) if settings else {}
    loc = local_defaults()
    mode = s.get("mode") or os.environ.get("LLM_MODE") or "local_first"
    if mode == "local_first" and s.get("allow_cloud") is False:
        mode = "local_only"
    return {
        "mode": mode,
        "local": {**loc, "base_url": (s.get("local_base_url") or loc["base_url"]).rstrip("/"),
                  "model": s.get("local_model") or loc["model"]},
        "cloud": {"configured": bool(config.OPENROUTER_API_KEY), "model": config.OPENROUTER_MODEL},
    }


def text_message(prompt: str) -> list[dict]:
    return [{"role": "user", "content": prompt}]


def image_message(prompt: str, images: list[tuple[bytes, str]]) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for data, mime in images:
        b64 = base64.b64encode(data).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return [{"role": "user", "content": content}]


def decode_json(content: str) -> dict:
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip())
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise LLMError(f"réponse illisible : {content[:200]}")


def _local(messages: list[dict], cfg: dict) -> dict:
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg.get("api_key") else {}
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0,
               "response_format": {"type": "json_object"}}
    r = httpx.post(f"{cfg['base_url']}/chat/completions", json=payload, headers=headers, timeout=cfg["timeout"])
    if r.status_code != 200:
        raise LLMError(f"serveur local {r.status_code} : {r.text[:200]}")
    return decode_json(r.json()["choices"][0]["message"]["content"])


def _cloud(messages: list[dict]) -> dict:
    if not config.OPENROUTER_API_KEY:
        raise LLMError("OPENROUTER_API_KEY non configurée")
    r = httpx.post(config.OPENROUTER_URL, json={"model": config.OPENROUTER_MODEL, "messages": messages},
                   headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}, timeout=120)
    if r.status_code != 200:
        raise LLMError(f"OpenRouter {r.status_code} : {r.text[:200]}")
    return decode_json(r.json()["choices"][0]["message"]["content"])


def chat_json(messages: list[dict], settings: dict | None = None) -> tuple[dict, str]:
    """Ask for a strict-JSON answer. Returns (data, engine). Raises LLMError when nobody answered."""
    cfg = resolve(settings)
    order = {"local_first": ["local", "cloud"], "local_only": ["local"], "cloud_only": ["cloud"]}.get(
        cfg["mode"], ["local", "cloud"])
    errors = []
    for prov in order:
        try:
            if prov == "local":
                return _local(messages, cfg["local"]), f"local:{cfg['local']['model']}"
            if not cfg["cloud"]["configured"]:
                errors.append("cloud: pas de clé OpenRouter")
                continue
            return _cloud(messages), f"cloud:{cfg['cloud']['model']}"
        except (httpx.HTTPError, LLMError, KeyError, ValueError) as e:
            errors.append(f"{prov}: {e}")
    raise LLMError(" · ".join(errors) or "aucun modèle disponible")


def status(settings: dict | None = None) -> dict:
    """Is the local server up, which models does it have, is the cloud configured?"""
    cfg = resolve(settings)
    loc = cfg["local"]
    reachable, models, error = False, [], ""
    try:
        r = httpx.get(f"{loc['base_url']}/models", timeout=3)
        if r.status_code == 200:
            reachable = True
            models = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except httpx.HTTPError as e:
        error = str(e)[:120]
    return {
        "mode": cfg["mode"],
        "local": {"base_url": loc["base_url"], "model": loc["model"], "reachable": reachable,
                  "model_installed": loc["model"] in models if models else None, "models": models, "error": error},
        "cloud": cfg["cloud"],
        "can_parse": reachable or (cfg["mode"] != "local_only" and cfg["cloud"]["configured"]),
    }
