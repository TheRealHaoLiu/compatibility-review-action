"""One model call, no tools, schema-constrained JSON; the verdict is derived afterwards.

APIs (pick by what is in front of the model, not by model family):
  responses  OpenAI Responses API (default). Standard library only.
  chat       OpenAI Chat Completions API, for endpoints that only speak chat.
             Env for both: OPENAI_BASE_URL, OPENAI_API_KEY.
  anthropic  Anthropic Messages API via the official SDK (`pip install anthropic`).
             Env: ANTHROPIC_API_KEY, optionally ANTHROPIC_BASE_URL.

COMPAT_REVIEW_HEADERS may hold a JSON object of extra request headers for a gateway.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config
from . import PROMPT_VERSION, RULE_VERSION, derive

HERE = Path(__file__).resolve().parent
RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}


def load_assets(context_text, enriched=True):
    """(system prompt, output schema)."""
    system = (HERE / "prompt.md").read_text() + "\n" + context_text
    if enriched:
        system += (HERE / "enriched.md").read_text()
    schema = json.loads((HERE / "schema.json").read_text())
    return system, schema


def _headers(key):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    headers.update(config.headers(config.current()))
    return headers


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def call_responses(model, system, user, schema, base_url=None, effort=None, timeout=300):
    base = (base_url or config.current().BASE_URL or "https://api.openai.com").rstrip("/")
    key = config.current().API_KEY or ""
    body = {
        "model": model,
        "instructions": system,
        "input": user,
        "text": {"format": {"type": "json_schema", "name": "compat_review", "strict": True, "schema": schema}},
        "store": False,
    }
    if effort:
        body["reasoning"] = {"effort": effort}
    resp = _post(base + "/v1/responses", body, _headers(key), timeout)
    if resp.get("error"):
        raise RuntimeError(f"error={resp['error']}")
    if resp.get("status") not in (None, "completed"):
        raise RuntimeError(f"status={resp.get('status')} incomplete={resp.get('incomplete_details')}")
    text = next(c["text"] for o in resp["output"] if o.get("type") == "message"
                for c in o["content"] if c.get("type") == "output_text")
    u = resp.get("usage") or {}
    return text, {"in": u.get("input_tokens"), "out": u.get("output_tokens"),
                  "reasoning": (u.get("output_tokens_details") or {}).get("reasoning_tokens")}


def call_chat(model, system, user, schema, base_url=None, effort=None, timeout=300):
    base = (base_url or config.current().BASE_URL or "https://api.openai.com").rstrip("/")
    key = config.current().API_KEY or ""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "compat_review", "strict": True, "schema": schema}},
    }
    if effort:
        body["reasoning_effort"] = effort
    resp = _post(base + "/v1/chat/completions", body, _headers(key), timeout)
    if resp.get("error"):
        raise RuntimeError(f"error={resp['error']}")
    choice = resp["choices"][0]
    if choice.get("finish_reason") not in (None, "stop"):
        raise RuntimeError(f"finish_reason={choice.get('finish_reason')}")
    u = resp.get("usage") or {}
    return choice["message"]["content"], {
        "in": u.get("prompt_tokens"), "out": u.get("completion_tokens"),
        "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens")}


def call_anthropic(model, system, user, schema, base_url=None, effort=None, timeout=300):
    import anthropic

    client = anthropic.Anthropic(api_key=config.current().ANTHROPIC_API_KEY or None, base_url=base_url,
                                 max_retries=0, timeout=timeout)
    output_config = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        output_config["effort"] = effort
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config=output_config,
    )
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"stop_reason={response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    return text, {"in": response.usage.input_tokens, "out": response.usage.output_tokens, "reasoning": None}


CALLS = {"responses": call_responses, "chat": call_chat, "anthropic": call_anthropic}


def retryable(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status in RETRYABLE
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}


def review(user, context_text, model, api="responses", base_url=None, effort="medium",
           enriched=True, timeout=300, attempts=4):
    """Review one assembled input. Returns the answers with the derived verdict and a _meta block."""
    system, schema = load_assets(context_text, enriched)
    call = CALLS[api]
    err = None
    for attempt in range(1, attempts + 1):
        start = time.monotonic()
        try:
            text, usage = call(model, system, user, schema, base_url, effort, timeout)
            result = json.loads(text)
            break
        except json.JSONDecodeError as e:
            err = e
        except Exception as e:  # noqa: BLE001 - classified by retryable()
            if not retryable(e):
                raise
            err = e
        if attempt == attempts:
            raise RuntimeError(f"gave up after {attempt} attempts: {type(err).__name__}: {str(err)[:300]}")
        time.sleep(min(60, 5 * 2 ** attempt))
    derive.add_verdicts(result)
    result["_meta"] = {"prompt": PROMPT_VERSION, "rule": RULE_VERSION, "model": model, "api": api,
                       "effort": effort, "enriched": enriched, "usage": usage,
                       "latency_s": round(time.monotonic() - start, 1)}
    return result
