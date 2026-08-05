"""ClaudeOracle is tested against a fake transport — no network, no key.

The fake speaks the Messages API response shape: a `content` list of typed
blocks, `stop_reason`, `usage`. What these tests pin down is the contract the
real API enforces: the request must not carry sampling parameters, the reply
text must be read from text blocks only, and `stop_reason: "refusal"` must be
handled before content is touched.
"""

import unittest

from trapcpu.llm import ClaudeOracle, DEFAULT_MODEL, OracleTransportError
from trapcpu.protocol import Mode, Status, TrapFrame, render_reply, validate


def frame(**kwargs):
    kwargs.setdefault("nonce", 0xBEEF)
    kwargs.setdefault("prompt", "Name a colour.")
    kwargs.setdefault("capacity", 32)
    return TrapFrame(**kwargs)


def api_response(text, stop_reason="end_turn"):
    return {
        "content": [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": text},
        ],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


class FakeTransport:
    """Scripted (status, body, headers) responses; records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, payload, headers):
        self.requests.append((payload, headers))
        status, body = self.responses.pop(0)
        return status, body, {}


def oracle(responses, **kwargs):
    transport = FakeTransport(responses)
    kwargs.setdefault("rng", __import__("random").Random(0))
    backend = ClaudeOracle(transport=transport, **kwargs)
    backend._sleep_log = []
    return backend, transport


class TestRequestShape(unittest.TestCase):

    def test_the_frame_is_the_user_message_verbatim(self):
        request = frame()
        backend, transport = oracle([(200, api_response(
            render_reply(request.nonce, ["BLUE"], checksum="crc")))])
        backend.ask(request)

        payload, headers = transport.requests[0]
        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][0]["content"], request.render())
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    def test_no_sampling_parameters_are_ever_sent(self):
        """temperature/top_p/top_k return 400 on current models."""
        request = frame()
        backend, transport = oracle([(200, api_response("x"))])
        backend.ask(request)
        payload, _ = transport.requests[0]
        for banned in ("temperature", "top_p", "top_k", "thinking"):
            self.assertNotIn(banned, payload)

    def test_effort_goes_inside_output_config(self):
        request = frame()
        backend, transport = oracle([(200, api_response("x"))], effort="low")
        backend.ask(request)
        payload, _ = transport.requests[0]
        self.assertEqual(payload["output_config"], {"effort": "low"})

    def test_api_key_header(self):
        backend, transport = oracle([(200, api_response("x"))], api_key="sk-test")
        backend.ask(frame())
        _, headers = transport.requests[0]
        self.assertEqual(headers["x-api-key"], "sk-test")
        self.assertNotIn("Authorization", headers)

    def test_oauth_token_uses_bearer_plus_beta(self):
        backend, transport = oracle(
            [(200, api_response("x"))], api_key="", auth_token="tok"
        )
        backend.ask(frame())
        _, headers = transport.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")

    def test_missing_credentials_fail_at_construction_not_mid_trap(self):
        import os
        old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        old_tok = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        try:
            with self.assertRaises(OracleTransportError):
                ClaudeOracle()
        finally:
            if old_key:
                os.environ["ANTHROPIC_API_KEY"] = old_key
            if old_tok:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = old_tok


class TestResponseHandling(unittest.TestCase):

    def test_a_protocol_speaking_reply_validates_end_to_end(self):
        request = frame()
        reply = render_reply(request.nonce, ["BLUE"], checksum="crc")
        backend, _ = oracle([(200, api_response(reply))])
        result = validate(backend.ask(request), request)
        self.assertEqual(result.status, Status.OK)
        self.assertEqual(result.payload, b"BLUE")

    def test_only_text_blocks_are_read(self):
        """Thinking blocks and other block types must not leak into the reply."""
        request = frame()
        body = {
            "content": [
                {"type": "thinking", "thinking": "hmm let me think"},
                {"type": "text", "text": "part one\n"},
                {"type": "text", "text": "part two"},
            ],
            "stop_reason": "end_turn",
        }
        backend, _ = oracle([(200, body)])
        self.assertEqual(backend.ask(request), "part one\npart two")

    def test_refusal_stop_reason_becomes_a_refused_frame(self):
        """stop_reason must be checked before content is read."""
        request = frame()
        body = {"content": [], "stop_reason": "refusal",
                "stop_details": {"type": "refusal", "category": "cyber"}}
        backend, _ = oracle([(200, body)])
        result = validate(backend.ask(request), request)
        self.assertEqual(result.status, Status.REFUSED)

    def test_a_chatty_model_reply_still_validates(self):
        """The backend does no parsing — tolerance lives in the validator."""
        request = frame()
        reply = "Sure!\n\n" + render_reply(request.nonce, ["BLUE"], checksum="crc")
        backend, _ = oracle([(200, api_response(reply))])
        self.assertTrue(validate(backend.ask(request), request).ok)


class TestRetries(unittest.TestCase):

    def setUp(self):
        # patch out real sleeping
        import trapcpu.llm as llm
        self._sleep = llm.time.sleep
        llm.time.sleep = lambda seconds: None

    def tearDown(self):
        import trapcpu.llm as llm
        llm.time.sleep = self._sleep

    def test_rate_limit_is_retried_then_succeeds(self):
        request = frame()
        backend, transport = oracle([
            (429, {"error": {"type": "rate_limit_error", "message": "slow down"}}),
            (200, api_response(render_reply(request.nonce, ["BLUE"]))),
        ])
        self.assertIsNotNone(backend.ask(request))
        self.assertEqual(len(transport.requests), 2)

    def test_overload_and_5xx_are_retryable(self):
        request = frame()
        backend, transport = oracle([
            (529, {"error": {"type": "overloaded_error", "message": ""}}),
            (500, {"error": {"type": "api_error", "message": ""}}),
            (200, api_response(render_reply(request.nonce, ["BLUE"]))),
        ])
        self.assertIsNotNone(backend.ask(request))
        self.assertEqual(len(transport.requests), 3)

    def test_exhausted_retries_return_none_for_status_retries(self):
        """None → the machine completes the trap with Status.RETRIES."""
        backend, transport = oracle(
            [(429, {"error": {}})] * 4, max_retries=3
        )
        self.assertIsNone(backend.ask(frame()))
        self.assertEqual(len(transport.requests), 4)

    def test_auth_errors_raise_immediately(self):
        backend, transport = oracle(
            [(401, {"error": {"type": "authentication_error",
                              "message": "invalid x-api-key"}})]
        )
        with self.assertRaises(OracleTransportError):
            backend.ask(frame())
        self.assertEqual(len(transport.requests), 1)

    def test_bad_requests_raise_rather_than_retry(self):
        backend, transport = oracle(
            [(400, {"error": {"type": "invalid_request_error",
                              "message": "bad model"}})]
        )
        with self.assertRaises(OracleTransportError):
            backend.ask(frame())
        self.assertEqual(len(transport.requests), 1)

    def test_network_failures_are_retried(self):
        request = frame()
        backend, transport = oracle([
            (599, {"error": {"type": "connection_error", "message": "reset"}}),
            (200, api_response(render_reply(request.nonce, ["BLUE"]))),
        ])
        self.assertIsNotNone(backend.ask(request))


class TestIndependentReplicas(unittest.TestCase):

    def test_one_call_per_replica_and_an_assembled_frame(self):
        request = frame(replicas=3)
        single = lambda payload: api_response(
            render_reply(request.nonce, [payload], checksum="crc"))
        backend, transport = oracle(
            [(200, single("BLUE")), (200, single("BLUE")), (200, single("BLXE"))],
            independent_replicas=True,
        )
        reply = backend.ask(request)
        self.assertEqual(len(transport.requests), 3)

        # each sub-call was asked for exactly one block
        sub_frame = transport.requests[0][0]["messages"][0]["content"]
        self.assertIn("REPLICAS: 1", sub_frame)

        result = validate(reply, request)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload, b"BLUE")
        self.assertEqual(result.corrected, 1)

    def test_a_malformed_sub_reply_is_surfaced_not_hidden(self):
        request = frame(replicas=2)
        backend, _ = oracle(
            [(200, api_response("I refuse to speak your format"))],
            independent_replicas=True,
        )
        reply = backend.ask(request)
        self.assertEqual(validate(reply, request).status, Status.NO_FRAME)

    def test_single_replica_requests_take_the_plain_path(self):
        request = frame(replicas=1)
        backend, transport = oracle(
            [(200, api_response(render_reply(request.nonce, ["BLUE"])))],
            independent_replicas=True,
        )
        backend.ask(request)
        self.assertEqual(len(transport.requests), 1)
        self.assertIn("REPLICAS: 1", transport.requests[0][0]["messages"][0]["content"])


class TestMachineIntegration(unittest.TestCase):

    def test_a_full_trap_through_the_fake_api(self):
        from trapcpu import Machine, assemble
        from trapcpu.protocol import DESCRIPTOR_SIZE, pack_descriptor

        machine = Machine(seed=9)
        machine.load(assemble("LDIA 0x0300\nOUTP 0x30\nTRAP\nHLT\n"))
        machine.ram[0x0300:0x0300 + DESCRIPTOR_SIZE] = pack_descriptor(
            0x0400, 0x0500, 32
        )
        machine.ram[0x0400:0x0404] = b"hi?\x00"

        result = machine.run()
        reply = render_reply(result.frame.nonce, ["hello, tiny machine"][:1])
        backend, _ = oracle([(200, api_response(reply))])
        final = machine.resume(backend.ask(result.frame))

        self.assertEqual(final.state, "HALTED")
        self.assertEqual(
            bytes(machine.ram[0x0500:0x0513]), b"hello, tiny machine"
        )


if __name__ == "__main__":
    unittest.main()
