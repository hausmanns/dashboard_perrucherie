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
`local:qwen2.5vl:7b` — the review screen shows it. Pass `trace={}` to keep,
for debugging, what was sent and, per provider tried, the model's thinking
(when it gives one), its raw answer, tokens, time and error.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
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


TRACE_MAX = 60_000  # characters kept per text in a debug trace


def _describe(messages: list[dict]) -> dict:
    """What was sent, for the trace: the text parts and how many images (not the images)."""
    texts, images = [], 0
    for m in messages:
        content = m.get("content")
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        for p in parts:
            if p.get("type") == "text":
                texts.append(p.get("text") or "")
            elif p.get("type") == "image_url":
                images += 1
    return {"prompt": "\n\n".join(texts)[:TRACE_MAX], "images": images}


def _answer(body: dict, call: dict) -> dict:
    """Decode the JSON answer; note the thinking, raw text and tokens in `call`."""
    msg = body["choices"][0]["message"]
    content = msg.get("content") or ""
    inline = re.match(r"\s*<think>(.*?)</think>\s*(.*)", content, re.S)  # thinking left in the text
    if inline:
        content = inline.group(2)
    # Ollama, OpenRouter: `reasoning` · vLLM, llama.cpp, LM Studio: `reasoning_content`
    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or (inline.group(1).strip() if inline else "")
    if not isinstance(reasoning, str):
        reasoning = json.dumps(reasoning, ensure_ascii=False)
    call.update(reasoning=reasoning[:TRACE_MAX], raw=content[:TRACE_MAX], usage=body.get("usage") or {})
    return decode_json(content)


def _local(messages: list[dict], cfg: dict, call: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg.get("api_key") else {}
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0,
               "response_format": {"type": "json_object"}}
    r = httpx.post(f"{cfg['base_url']}/chat/completions", json=payload, headers=headers, timeout=cfg["timeout"])
    if r.status_code != 200:
        raise LLMError(f"serveur local {r.status_code} : {r.text[:200]}")
    return _answer(r.json(), {} if call is None else call)


def _cloud(messages: list[dict], call: dict | None = None) -> dict:
    if not config.OPENROUTER_API_KEY:
        raise LLMError("OPENROUTER_API_KEY non configurée")
    r = httpx.post(config.OPENROUTER_URL, json={"model": config.OPENROUTER_MODEL, "messages": messages},
                   headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}, timeout=120)
    if r.status_code != 200:
        raise LLMError(f"OpenRouter {r.status_code} : {r.text[:200]}")
    return _answer(r.json(), {} if call is None else call)


def chat_json(messages: list[dict], settings: dict | None = None,
              trace: dict | None = None) -> tuple[dict, str]:
    """Ask for a strict-JSON answer. Returns (data, engine). Raises LLMError when nobody answered.

    `trace`, when given, is filled even on failure: {prompt, images, attempts: [{engine, seconds,
    reasoning, raw, usage, error?}]} — one attempt per provider tried."""
    cfg = resolve(settings)
    order = {"local_first": ["local", "cloud"], "local_only": ["local"], "cloud_only": ["cloud"]}.get(
        cfg["mode"], ["local", "cloud"])
    if trace is not None:
        trace.update(_describe(messages), attempts=[])
    errors = []
    for prov in order:
        if prov == "cloud" and not cfg["cloud"]["configured"]:
            errors.append("cloud: pas de clé OpenRouter")
            continue
        engine = f"local:{cfg['local']['model']}" if prov == "local" else f"cloud:{cfg['cloud']['model']}"
        call: dict = {"engine": engine}
        t0 = time.monotonic()
        try:
            data = _local(messages, cfg["local"], call) if prov == "local" else _cloud(messages, call)
            return data, engine
        except (httpx.HTTPError, LLMError, KeyError, ValueError) as e:
            errors.append(f"{prov}: {e}")
            call["error"] = str(e)[:500]
        finally:
            call["seconds"] = round(time.monotonic() - t0, 1)
            if trace is not None:
                trace["attempts"].append(call)
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
