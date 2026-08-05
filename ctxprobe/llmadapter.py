"""A real model as the compactor: the claude study adapter.

Standard library only — raw HTTPS via ``urllib`` rather than the ``anthropic``
SDK — for the same reason as trapcpu's oracle backend: this project's premise
is code you can paste into a sandbox with no package manager. In any ordinary
project, use the official SDK instead.

The adapter asks the model to do exactly what agent harnesses do to long
conversations: compact the transcript, keep what matters. No hints about the
probes — whether sentinel sectors read as "what matters" to a summarizer is
precisely the measurement.

Requires ``ANTHROPIC_API_KEY`` (or ``ANTHROPIC_AUTH_TOKEN``) in the
environment. ``ANTHROPIC_BASE_URL`` overrides the endpoint. Server-side
refusal fallbacks are enabled by default so a safety-classifier decline
re-runs on a fallback model instead of failing the trial.
"""

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "claude-opus-5"
API_VERSION = "2023-06-01"

# Non-streaming ceiling per the current API guidance; thinking is on by
# default on this model tier and shares the budget with the response text.
MAX_TOKENS = 16000

RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}

COMPACT_PROMPT = (
    "The following is a transcript of a long working conversation. Compact "
    "it to roughly half its length while preserving everything that would "
    "matter to someone resuming the work later. Return ONLY the compacted "
    "transcript text, with no commentary before or after it.\n\n"
    "--- TRANSCRIPT START ---\n{transcript}\n--- TRANSCRIPT END ---"
)


class AdapterError(Exception):
    pass


def _credentials():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key and not auth_token:
        raise AdapterError(
            "the claude adapter needs ANTHROPIC_API_KEY or "
            "ANTHROPIC_AUTH_TOKEN in the environment")
    return api_key, auth_token


def compact_with_claude(transcript, model=None, max_retries=4):
    """Ask a Claude model to compact ``transcript``; return its text."""
    api_key, auth_token = _credentials()
    endpoint = os.environ.get(
        "ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")

    payload = {
        "model": model or DEFAULT_MODEL,
        "max_tokens": MAX_TOKENS,
        "fallbacks": "default",
        "messages": [{
            "role": "user",
            "content": COMPACT_PROMPT.format(transcript=transcript),
        }],
    }

    headers = {
        "content-type": "application/json",
        "anthropic-version": API_VERSION,
        "anthropic-beta": "server-side-fallback-2026-07-01",
    }
    if api_key:
        headers["x-api-key"] = api_key
    else:
        headers["authorization"] = "Bearer %s" % auth_token
        headers["anthropic-beta"] += ",oauth-2025-04-20"

    request = urllib.request.Request(
        endpoint + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                body = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if error.code in RETRYABLE and attempt < max_retries:
                retry_after = error.headers.get("retry-after")
                delay = (float(retry_after) if retry_after
                         else min(2 ** attempt, 30))
                time.sleep(delay)
                continue
            detail = error.read().decode("utf-8", "replace")[:300]
            raise AdapterError(
                "API error %d: %s" % (error.code, detail)) from error
        except urllib.error.URLError as error:
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise AdapterError("network error: %s" % error) from error

    if body.get("stop_reason") == "refusal":
        raise AdapterError(
            "the model (and any fallback) declined the request: %s"
            % (body.get("stop_details") or {}))

    text = "".join(block.get("text", "")
                   for block in body.get("content", [])
                   if block.get("type") == "text")
    if not text.strip():
        raise AdapterError("the model returned no text content")
    return text
