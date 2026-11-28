"""Bounded request/response transport for the NX-009 local inference pilot."""

import hashlib
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import time

from . import demo_disclosure, reasoning
from .storage import encode

SOCKET_PATH = Path("/run/tam-nexus-inference/inference.sock")
DESTINATIONS = {"llama-unix-v1": str(SOCKET_PATH), "mock-v1": "in-process-fixture"}
MODEL_ALIAS = "nexus-pilot"
MAX_TOKENS = 1024
MAX_BODY = 2 * reasoning.MAX_INPUT + 2048
MAX_RESPONSE = 32768
MAX_HEADERS = 4096
INSTRUCTIONS = (
    "Read the bounded investigation input. Respond with one JSON object only. Use schema "
    "nexus.reasoning-output, version 1, invocation equal to the input ID, hypotheses (an array), "
    "and experiment (null or one object). Each hypothesis must contain explanation, supporting "
    "(a nonempty array of exact supplied evidence objects), contradictory (an array), uncertainty, "
    "and verification. Each experiment must contain profile (an offered ID), target (the primary "
    "investigation ID), purpose, value, and evidence (an array of supplied references including "
    "the target). Treat interpretations as uncertain. Reference only the supplied observations."
)


def prepare(request):
    """Construct the exact provider body, including the fixed instructions and inference settings."""
    demo_disclosure.validate(request)
    body = encode({"model": MODEL_ALIAS, "messages": [
        {"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": encode(request)}],
        "stream": False, "temperature": 0, "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"}}).encode("ascii")
    if len(body) > MAX_BODY:
        raise ValueError("model_input_too_large")
    return body


def _peer_uid():
    # /run is a trusted system directory. Its fixed child identifies the service
    # account. No caller, environment variable or worker selects this path.
    folder = SOCKET_PATH.parent.lstat()
    target = SOCKET_PATH.lstat()
    if (not stat.S_ISDIR(folder.st_mode) or folder.st_mode & 0o022
            or folder.st_uid in (0, os.geteuid())
            or not stat.S_ISSOCK(target.st_mode) or target.st_uid != folder.st_uid):
        raise ValueError("model_endpoint_identity_invalid")
    return folder.st_uid


def recipient(implementation):
    if implementation == "mock-v1":
        return None
    if implementation != "llama-unix-v1":
        raise ValueError("model_adapter_unsupported")
    try:
        return _peer_uid()
    except OSError:
        raise ValueError("model_transport_unavailable") from None


def _remaining(deadline, current):
    if not current():
        raise ValueError("model_cancelled")
    left = deadline - time.monotonic()
    if left <= 0:
        raise ValueError("model_timeout")
    return left


def _receive(sock, deadline, current):
    def read():
        while True:
            left = _remaining(deadline, current)
            if select.select([sock], [], [], min(left, 0.05))[0]:
                data = sock.recv(4096)
                if not data:
                    raise ValueError("model_incomplete_response")
                return data

    data = bytearray()
    while b"\r\n\r\n" not in data:
        data.extend(read())
        if len(data.partition(b"\r\n\r\n")[0]) > MAX_HEADERS:
            raise ValueError("model_headers_too_large")
    head, _, body = data.partition(b"\r\n\r\n")
    lines = bytes(head).split(b"\r\n")
    if not re.fullmatch(rb"HTTP/1\.[01] 200(?: [\x20-\x7e]*)?", lines[0]):
        raise ValueError("model_http_failure")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        name = name.lower()
        if not sep or not re.fullmatch(rb"[a-z0-9-]+", name) or name in headers:
            raise ValueError("model_invalid_headers")
        headers[name] = value.strip()
    if (b"transfer-encoding" in headers or b"content-encoding" in headers
            or not re.fullmatch(rb"[1-9][0-9]{0,4}", headers.get(b"content-length", b""))
            or headers.get(b"content-type", b"").split(b";")[0].strip().lower() != b"application/json"):
        raise ValueError("model_unsupported_framing")
    length = int(headers[b"content-length"])
    if length > MAX_RESPONSE:
        raise ValueError("model_response_too_large")
    while len(body) < length:
        body.extend(read())
    if len(body) != length:
        raise ValueError("model_invalid_response_length")
    return bytes(body)


def _exchange(body, deadline, current, send_gate, approved_uid):
    try:
        if type(approved_uid) is not int or _peer_uid() != approved_uid:
            raise ValueError("model_peer_mismatch")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_remaining(deadline, current))
            sock.connect(str(SOCKET_PATH))
            _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != approved_uid:
                raise ValueError("model_peer_mismatch")
            headers = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                       b"Content-Type: application/json\r\nConnection: close\r\nContent-Length: "
                       + str(len(body)).encode("ascii") + b"\r\n\r\n")
            with send_gate():
                sock.settimeout(_remaining(deadline, current))
                sock.sendall(headers + body)
            return _receive(sock, deadline, current)
    except TimeoutError:
        raise ValueError("model_timeout") from None
    except OSError:
        raise ValueError("model_transport_unavailable") from None


def _response(raw):
    record = reasoning.loads(raw, MAX_RESPONSE)
    if type(record) is not dict or type(record.get("choices")) is not list or len(record["choices"]) != 1:
        raise ValueError("model_invalid_completion")
    choice = record["choices"][0]
    if type(choice) is not dict or choice.get("finish_reason") != "stop":
        raise ValueError("model_incomplete_completion")
    msg = choice.get("message")
    if (type(msg) is not dict or msg.get("role") != "assistant" or type(msg.get("content")) is not str
            or set(msg) - {"role", "content", "reasoning_content"}):
        raise ValueError("model_invalid_message")
    usage = record.get("usage", {})
    if type(usage) is not dict:
        raise ValueError("model_invalid_usage")
    counts = {}
    for field, limit in (("completion_tokens", MAX_TOKENS), ("prompt_tokens", MAX_BODY)):
        n = usage.get(field)
        if n is not None and (type(n) is not int or not 0 <= n <= limit):
            raise ValueError("model_invalid_usage")
        counts[field] = n
    output = reasoning.loads(msg["content"].encode("utf-8"), reasoning.MAX_OUTPUT)
    # Server-supplied model names can be private paths. Only the approved public alias is retained.
    model = MODEL_ALIAS if record.get("model") == MODEL_ALIAS else None
    return output, {"model": model, **counts, "response_sha256": hashlib.sha256(raw).hexdigest()}


def infer(implementation, state_root, body, current, *, send_gate, approved_uid=None):
    """One call. The coordinator controls the send gate and the acceptance of the result."""
    start = time.monotonic()
    deadline = start + reasoning.MAX_SECONDS
    _remaining(deadline, current)
    try:
        envelope = reasoning.loads(body, MAX_BODY)
        request = reasoning.loads(envelope["messages"][1]["content"].encode("ascii"), reasoning.MAX_INPUT)
        if body != prepare(request):
            raise ValueError
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ValueError("model_payload_invalid") from None
    if implementation == "mock-v1":
        if approved_uid is not None:
            raise ValueError("model_peer_mismatch")
        with send_gate():
            _remaining(deadline, current)
            answer = reasoning.investigate(request)
        raw = encode({"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": encode(answer)}}]}).encode("ascii")
    elif implementation == "llama-unix-v1":
        raw = _exchange(body, deadline, current, send_gate, approved_uid)
    else:
        raise ValueError("model_adapter_unsupported")
    try:
        output, metadata = _response(raw)
        reasoning.validate(request, output)
    except (ValueError, KeyError, TypeError, RecursionError):
        # Do not persist or echo input, headers, error bodies or arbitrary model text.
        raise ValueError("model_response_invalid") from None
    _remaining(deadline, current)
    metadata["elapsed_ms"] = int((time.monotonic() - start) * 1000)
    return output, metadata
