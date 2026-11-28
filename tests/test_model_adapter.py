"""NX-009 transport tests. Socket peers in this suite are HTTP fixtures, never models."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from nexus import demo_disclosure, model_adapter as adapter, reasoning
from nexus.storage import encode


class ModelAdapterTests(unittest.TestCase):
    def setUp(self):
        example = Path(__file__).resolve().parents[1] / "examples/nx-008/engineering-handoff.json"
        data = json.loads(example.read_text())
        del data["followup_observations"]
        self.request = reasoning.make_input(demo_disclosure.OBJECTIVE, 12, data)
        self.body = adapter.prepare(self.request)
        self.answer = reasoning.investigate(self.request)
        self.completion = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": encode(self.answer)}}], "model": "nexus-pilot",
            "usage": {"completion_tokens": 9, "prompt_tokens": 1030}}
        self.raw = encode(self.completion).encode("ascii")

    def test_mock_is_offline_and_redacts_unrestricted_provider_labels(self):
        with patch.object(socket, "socket", side_effect=AssertionError):
            answer, meta = adapter.infer("mock-v1", None, self.body, lambda: True, send_gate=nullcontext)
        self.assertEqual(answer, self.answer)
        self.assertIsNone(meta["model"])
        record = {**self.completion, "model": "/home/private/models/model.gguf"}
        self.assertIsNone(adapter._response(encode(record).encode())[1]["model"])
        with self.assertRaisesRegex(ValueError, "model_payload_invalid"):
            adapter.infer("mock-v1", None, self.body + b" ", lambda: True, send_gate=nullcontext)

    def test_server_tool_calls_incomplete_or_excessive_outputs_are_rejected(self):
        choice = self.completion["choices"][0]
        for record in (
                {"choices": [{"finish_reason": "tool_calls", "message": choice["message"]}]},
                {"choices": [dict(choice, message=dict(choice["message"], tool_calls=[]))]},
                {"choices": [dict(choice, message=dict(choice["message"], content="x"*8193))]},
                dict(self.completion, usage={"completion_tokens": adapter.MAX_TOKENS + 1}),
                {"choices": [choice, choice]}):
            with self.subTest(keys=record.keys()), self.assertRaises(ValueError):
                adapter._response(encode(record).encode())

    def test_exact_wire_disclosure_single_gate_no_tools_and_bounded_metadata(self):
        client, peer = socket.socketpair()
        calls = []

        @contextmanager
        def gate():
            calls.append("send")
            yield

        def serve():
            with peer:
                peer.settimeout(3)
                received = bytearray()
                while b"\r\n\r\n" not in received:
                    received.extend(peer.recv(4096))
                head, _, body = received.partition(b"\r\n\r\n")
                while len(body) < len(self.body):
                    body.extend(peer.recv(4096))
                peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(self.raw)).encode() + b"\r\n\r\n" + self.raw)
                return bytes(head), bytes(body)

        # A connected AF_UNIX socketpair provides the synthetic peer. Only the dial
        # and expected service UID are substituted; peer credentials and bytes are real.
        with client, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(serve)
            with patch.object(adapter, "_peer_uid", return_value=os.getuid()), patch.object(socket, "socket") as factory:
                dial = factory.return_value.__enter__.return_value
                for name in ("getsockopt", "settimeout", "sendall", "fileno", "recv"):
                    setattr(dial, name, getattr(client, name))
                answer, meta = adapter.infer("llama-unix-v1", None, self.body, lambda: True,
                                             send_gate=gate, approved_uid=os.getuid())
                factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
                dial.connect.assert_called_once_with(str(adapter.SOCKET_PATH))
            head, wire = future.result(timeout=3)
        self.assertEqual(calls, ["send"])
        self.assertTrue(head.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n"))
        self.assertNotIn(b"Authorization", head)
        self.assertEqual(wire, self.body)
        self.assertEqual(json.loads(json.loads(wire)["messages"][1]["content"]), self.request)
        self.assertNotIn("tools", json.loads(wire))
        self.assertEqual(meta["response_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(meta["completion_tokens"], 9)
        self.assertEqual(answer, self.answer)

    def test_replaced_service_identity_discloses_no_bytes(self):
        with patch.object(adapter, "_peer_uid", return_value=101), patch.object(socket, "socket") as factory:
            with self.assertRaisesRegex(ValueError, "model_peer_mismatch"):
                adapter.infer("llama-unix-v1", None, self.body, lambda: True,
                              send_gate=nullcontext, approved_uid=102)
            factory.assert_not_called()
            peer = factory.return_value.__enter__.return_value
            peer.getsockopt.return_value = adapter.struct.pack("3i", 222, 102, 102)
            with self.assertRaisesRegex(ValueError, "model_peer_mismatch"):
                adapter.infer("llama-unix-v1", None, self.body, lambda: True,
                              send_gate=nullcontext, approved_uid=101)
            peer.sendall.assert_not_called()

    def test_fail_closed_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "inference.sock"
            target.touch()
            with patch.object(adapter, "SOCKET_PATH", target), self.assertRaisesRegex(ValueError, "model_endpoint_identity_invalid"):
                adapter._peer_uid()

    def test_read_deadline_cancel_redirect_and_framing(self):
        left, right = socket.socketpair()
        with left, right:
            with self.assertRaisesRegex(ValueError, "model_timeout"):
                adapter._receive(left, time.monotonic() + .02, lambda: True)
            with self.assertRaisesRegex(ValueError, "model_cancelled"):
                adapter._receive(left, time.monotonic() + 1, lambda: False)
            right.sendall(b"HTTP/1.1 307 Temporary Redirect\r\nLocation: https://elsewhere.invalid/\r\n\r\n")
            with self.assertRaisesRegex(ValueError, "model_http_failure"):
                adapter._receive(left, time.monotonic() + 1, lambda: True)
            right.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 99999\r\n\r\n")
            with self.assertRaisesRegex(ValueError, "model_response_too_large"):
                adapter._receive(left, time.monotonic() + 1, lambda: True)
