"""ClaudeOracle: a frontier model on the bus, over the Anthropic Messages API.

This module is standard library only — raw HTTPS via ``urllib`` rather than the
``anthropic`` SDK — because TRAPCPU's entire premise is a system you can paste
into a sandbox with no package manager. That constraint is why this file
exists; in any ordinary project you would use the official SDK instead.

The design principle is the same one the rest of the machine follows: **the
model speaks the protocol, not the transport.** ``ask`` sends the rendered
request frame as the user message, verbatim, and returns whatever text comes
back. No JSON mode, no schema, no parsing on this side — if the model breaks
the frame format, the validator catches it and the retry logic re-asks, which
is precisely the failure path this project exists to exercise.

One API-level outcome does get translated: a safety-classifier decline arrives
as HTTP 200 with ``stop_reason: "refusal"``, and this backend renders it as a
protocol-level ``STATUS: REFUSED`` frame. The mapping is exact — both mean
"the device declined to answer" — and it keeps the guest program's REFUSED
branch honest.

Requires ``ANTHROPIC_API_KEY`` (or ``ANTHROPIC_AUTH_TOKEN``) in the
environment, or an explicit key argument.
"""

import json
import os
import random
import time
import urllib.error
import urllib.request

from .oracle import Oracle
from .protocol import find_reply_frames, render_reply

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_ENDPOINT = "https://api.anthropic.com"
API_VERSION = "2023-06-01"

# The oracle's answers are bounded by the descriptor's CAPACITY (<= 64 KiB of
# RAM in the pathological case, tens of bytes in practice), so a small output
# ceiling is correct here, not stingy.
DEFAULT_MAX_TOKENS = 1024

SYSTEM_PROMPT = (
    "You are the oracle coprocessor of TRAPCPU, a 16-bit computer that has "
    "mapped you into its I/O space at port 0x30. Each user message is a "
    "hardware request frame published by the machine while it sits suspended "
    "on a TRAP instruction. Answer the question inside the PROMPT fences and "
    "reply with exactly one reply frame in the format the request specifies, "
    "at column zero, with no surrounding prose. Follow the RULES section "
    "precisely: the NONCE must match, LEN must be the exact UTF-8 byte count "
    "of your payload, and the payload must respect the MODE. Your reply is "
    "parsed by an emulator, not read by a person."
)

RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}


class OracleTransportError(Exception):
    """A non-retryable transport failure: bad key, bad model, bad request."""


class ClaudeOracle(Oracle):
    """Attach a Claude model to port 0x30.

    ``transport`` is injectable for tests: a callable
    ``(payload: dict, headers: dict) -> (status: int, body: dict, headers: dict)``.
    The default transport POSTs to the Messages API with retry/backoff on
    429/5xx, honouring ``retry-after``.

    ``independent_replicas=True`` makes one API call per replica (each asked
    for a single payload block) and assembles the combined reply frame in the
    transport. That buys genuinely independent samples for the L4 vote at the
    cost of N calls; the default single-call mode asks the model to produce all
    N blocks itself, which is cheaper and measures the correlated-replica case
    the docs warn about.
    """

    name = "claude"

    def __init__(self, model=DEFAULT_MODEL, api_key=None, auth_token=None,
                 base_url=None, max_tokens=DEFAULT_MAX_TOKENS, system=None,
                 effort=None, transport=None, max_retries=3, rng=None,
                 independent_replicas=False):
        self.model = model
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        self.auth_token = auth_token if auth_token is not None else os.environ.get("ANTHROPIC_AUTH_TOKEN")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL")
                         or DEFAULT_ENDPOINT).rstrip("/")
        self.max_tokens = max_tokens
        self.system = SYSTEM_PROMPT if system is None else system
        self.effort = effort
        self.transport = transport or self._http_transport
        self.max_retries = max_retries
        self.rng = rng or random.Random()
        self.independent_replicas = independent_replicas
        self.calls = 0
        self.last_usage = None

        if transport is None and not (self.api_key or self.auth_token):
            raise OracleTransportError(
                "no Anthropic credential: set ANTHROPIC_API_KEY (or "
                "ANTHROPIC_AUTH_TOKEN), or pass api_key="
            )

    # -- request construction ---------------------------------------------

    def _headers(self):
        headers = {
            "content-type": "application/json",
            "anthropic-version": API_VERSION,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        else:
            # OAuth tokens ride Authorization: Bearer plus the oauth beta.
            headers["Authorization"] = f"Bearer {self.auth_token}"
            headers["anthropic-beta"] = "oauth-2025-04-20"
        return headers

    def _payload(self, frame_text):
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": [{"role": "user", "content": frame_text}],
        }
        # No temperature/top_p/top_k: removed on current models (400), and the
        # protocol gets its per-attempt variation from resampling, not knobs.
        if self.effort:
            payload["output_config"] = {"effort": self.effort}
        return payload

    # -- transport ---------------------------------------------------------

    def _http_transport(self, payload, headers):
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read())
            except Exception:
                body = {"error": {"type": "unparsable", "message": str(error)}}
            return error.code, body, dict(error.headers or {})
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # Network-level failure: modelled as a retryable 599.
            return 599, {"error": {"type": "connection_error", "message": str(error)}}, {}

    def _call(self, frame_text):
        """One Messages API call with retry/backoff. Returns the body dict."""
        payload = self._payload(frame_text)
        headers = self._headers()
        last = None

        for attempt in range(self.max_retries + 1):
            status, body, response_headers = self.transport(payload, headers)
            self.calls += 1

            if status == 200:
                self.last_usage = body.get("usage")
                return body

            last = (status, body)
            if status in (401, 403):
                message = body.get("error", {}).get("message", "")
                raise OracleTransportError(
                    f"authentication failed ({status}): {message}"
                )
            if status not in RETRYABLE and status != 599:
                message = body.get("error", {}).get("message", "")
                raise OracleTransportError(f"API error {status}: {message}")

            if attempt < self.max_retries:
                retry_after = response_headers.get("retry-after")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = min(2.0 ** attempt + self.rng.random(), 30.0)
                time.sleep(delay)

        return {"_exhausted": last}

    # -- the oracle interface ----------------------------------------------

    @staticmethod
    def _text_of(body):
        """Extract the text blocks; check stop_reason before touching content."""
        if body.get("stop_reason") == "refusal":
            return None  # caller renders STATUS: REFUSED
        return "".join(
            block.get("text", "")
            for block in body.get("content", [])
            if block.get("type") == "text"
        )

    def ask(self, frame):
        if self.independent_replicas and frame.replicas > 1:
            return self._ask_independent(frame)

        body = self._call(frame.render())
        if "_exhausted" in body:
            return None  # machine completes the trap with Status.RETRIES

        text = self._text_of(body)
        if text is None:
            return render_reply(frame.nonce, [], status="REFUSED")
        return text

    def _ask_independent(self, frame):
        """One call per replica; the transport assembles the combined frame.

        Each sub-call sees a replicas=1 rendering of the same request (same
        nonce, same prompt), so its reply is a complete single-block frame.
        The payloads are extracted and re-framed. No checksum is attached:
        the transport is lossless and a CRC it computed itself would vouch
        for nothing the model said — the vote is the integrity layer here.
        """
        from .protocol import TrapFrame

        sub = TrapFrame(
            nonce=frame.nonce, prompt=frame.prompt, mode=frame.mode,
            replicas=1, capacity=frame.capacity, attempt=frame.attempt,
            retries=frame.retries, flags=frame.flags,
            registers=frame.registers, cycle=frame.cycle,
            descriptor=frame.descriptor,
        )
        rendered = sub.render()

        payloads = []
        for _ in range(frame.replicas):
            body = self._call(rendered)
            if "_exhausted" in body:
                return None
            text = self._text_of(body)
            if text is None:
                return render_reply(frame.nonce, [], status="REFUSED")

            parsed = find_reply_frames(text)
            if not parsed or not parsed[-1].blocks:
                # The sub-reply is malformed; hand the raw text to the
                # validator so the failure is reported truthfully.
                return text
            payloads.append(parsed[-1].blocks[0].text.strip())

        return render_reply(
            frame.nonce, payloads, replicas=frame.replicas, checksum=None
        )


__all__ = ["ClaudeOracle", "OracleTransportError", "DEFAULT_MODEL", "SYSTEM_PROMPT"]
