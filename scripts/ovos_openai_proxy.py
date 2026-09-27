#!/usr/bin/env python3
"""OpenAI-compatible proxy for OVOS -> familIA orchestrator (SigV4/IAM).

OpenVoice OS (and most tools) speak the OpenAI Chat Completions API: a base
URL ending in /v1, a POST /v1/chat/completions endpoint, and a Bearer API key.
The familIA API instead uses API Gateway with IAM/SigV4 auth and a simple
{question} -> {answer} contract.

This tiny proxy bridges the two. Run it on the Raspberry Pi, next to OVOS:

    OVOS  ──OpenAI(HTTP, localhost)──►  this proxy  ──SigV4──►  API Gateway /ask

The proxy:
  * exposes  http://127.0.0.1:8080/v1/chat/completions  (OpenAI shape)
  * accepts a LOCAL, made-up API key (OVOS requires *some* key). It never
    leaves the Pi and is unrelated to AWS auth. Set PROXY_API_KEY to require it.
  * takes the last user message as the question, calls /ask (SigV4-signed with
    the pi-client IAM credentials), and wraps the answer back in OpenAI format.
  * also implements GET /v1/models so clients that probe it don't error.

No secrets are hard-coded. AWS credentials come from the standard chain
(env vars / ~/.aws / pi-credentials.env). Configure via env vars:

    FAMILIA_API_URL   full /ask URL (required), e.g.
                      https://xxxx.execute-api.eu-central-1.amazonaws.com/prod/ask
    AWS_REGION        default eu-central-1
    PROXY_API_KEY     optional; if set, OVOS must send this exact Bearer key
    PROXY_HOST        default 127.0.0.1  (keep it loopback-only)
    PROXY_PORT        default 8080
    MODEL_NAME        label returned in /v1/models (default "familia")

Dependencies (install on the Pi):
    pip install botocore requests

Run:
    export FAMILIA_API_URL="https://xxxx.execute-api.eu-central-1.amazonaws.com/prod/ask"
    export AWS_REGION="eu-central-1"
    export PROXY_API_KEY="familia-local"          # any string; used only locally
    python3 ovos_openai_proxy.py
"""
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

SERVICE = "execute-api"

FAMILIA_API_URL = os.environ.get("FAMILIA_API_URL", "")
REGION = os.environ.get("AWS_REGION", "eu-central-1")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")  # optional local gate
PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8080"))
MODEL_NAME = os.environ.get("MODEL_NAME", "familia")
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "60"))

# Reuse one botocore session (credential lookup is cached).
_session = Session()


def _sigv4_ask(question: str) -> dict:
    """Sign and POST {question} to the familIA /ask orchestrator endpoint."""
    if not FAMILIA_API_URL:
        raise RuntimeError("FAMILIA_API_URL is not set.")
    payload = json.dumps({"question": question})
    aws_request = AWSRequest(
        method="POST",
        url=FAMILIA_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    creds = _session.get_credentials()
    if creds is None:
        raise RuntimeError("No AWS credentials found in the environment.")
    SigV4Auth(creds, SERVICE, REGION).add_auth(aws_request)
    resp = requests.post(
        FAMILIA_API_URL,
        data=payload,
        headers=dict(aws_request.headers),
        timeout=UPSTREAM_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_question(body: dict) -> str:
    """Pull the latest user message from an OpenAI chat request."""
    messages = body.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):  # OpenAI "parts" form
                return " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                ).strip()
            return (content or "").strip()
    # Fallback: some clients send a bare "prompt".
    return (body.get("prompt") or "").strip()


def _openai_chat_response(answer: str) -> dict:
    now = int(time.time())
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": now,
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "familia-ovos-proxy/1.0"

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if not PROXY_API_KEY:
            return True  # no local gate configured
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {PROXY_API_KEY}"

    def log_message(self, fmt, *args):  # keep logs quiet/clean
        pass

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            if not self._authorized():
                return self._send(401, {"error": {"message": "Invalid API key"}})
            return self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_NAME,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "familia",
                        }
                    ],
                },
            )
        if self.path.rstrip("/") in ("/health", "/healthz"):
            return self._send(200, {"status": "ok"})
        return self._send(404, {"error": {"message": "Not found"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self._send(404, {"error": {"message": "Not found"}})
        if not self._authorized():
            return self._send(401, {"error": {"message": "Invalid API key"}})

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": {"message": "Invalid JSON body"}})

        question = _extract_question(body)
        if not question:
            return self._send(400, {"error": {"message": "No user message found"}})

        try:
            result = _sigv4_ask(question)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 502
            return self._send(502, {"error": {"message": f"Upstream error ({code})"}})
        except Exception as exc:  # noqa: BLE001 - surface a clean error to OVOS
            return self._send(502, {"error": {"message": f"Proxy error: {exc}"}})

        answer = result.get("answer", "") or "No he podido obtener una respuesta."
        return self._send(200, _openai_chat_response(answer))


def main():
    if not FAMILIA_API_URL:
        raise SystemExit("Set FAMILIA_API_URL (the /ask endpoint) before starting.")
    httpd = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), Handler)
    print(f"familIA OVOS proxy on http://{PROXY_HOST}:{PROXY_PORT}/v1 -> {FAMILIA_API_URL}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
