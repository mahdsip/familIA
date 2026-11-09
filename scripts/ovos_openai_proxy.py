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
  * optionally (MCP_SERVER_COMMAND set) acts as the MCP client for live home
    data: it declares the familia-mcp tools to /ask and, when the orchestrator
    returns a tool request ("return of control"), runs it locally over MCP
    and sends the result back. Home services are never exposed to AWS.

No secrets are hard-coded. AWS credentials come from the standard chain
(env vars / ~/.aws / pi-credentials.env). Configure via env vars:

    FAMILIA_API_URL   full /ask URL (required), e.g.
                      https://xxxx.execute-api.eu-central-1.amazonaws.com/prod/ask
    AWS_REGION        default eu-central-1
    PROXY_API_KEY     optional; if set, OVOS must send this exact Bearer key
    PROXY_HOST        default 127.0.0.1  (keep it loopback-only)
    PROXY_PORT        default 8080
    MODEL_NAME        label returned in /v1/models (default "familia")
    MCP_SERVER_COMMAND  optional; command that starts the MCP server over
                      stdio, normally an `ssh ... familia-mcp@mcpserver.local` line
                      (see familia_mcp_bridge.py). Unset = no live tools.
    MAX_TOOL_ROUNDS   default 3; safety cap on tool rounds per question
    LOG_LEVEL         default INFO (DEBUG logs how many messages OVOS sends)

Dependencies (install on the Pi, in a venv):
    pip install -r requirements-proxy.txt   # botocore, requests (+ mcp for tools)

Run:
    export FAMILIA_API_URL="https://xxxx.execute-api.eu-central-1.amazonaws.com/prod/ask"
    export AWS_REGION="eu-central-1"
    export PROXY_API_KEY="familia-local"          # any string; used only locally
    python3 ovos_openai_proxy.py
"""
import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from datetime import datetime
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
MCP_SERVER_COMMAND = os.environ.get("MCP_SERVER_COMMAND", "").strip()
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "3"))
MAX_CALLS_PER_ROUND = 5
FALLBACK_ANSWER = "No he podido obtener una respuesta."

# Spoken confirmation of parked Home Assistant actions. The household speaks
# Spanish; override the word lists / phrases via env for another language.
CONFIRM_TTL_S = float(os.environ.get("CONFIRM_TTL_S", "60"))
CONFIRM_YES_WORDS = os.environ.get(
    "CONFIRM_YES_WORDS", "si,vale,confirmo,confirmado,adelante,hazlo,ok,okay,claro,correcto,venga,de acuerdo"
)
CONFIRM_NO_WORDS = os.environ.get(
    "CONFIRM_NO_WORDS", "no,cancela,cancelar,cancelalo,dejalo,olvidalo,espera,para"
)
CONFIRM_DONE_TEXT = os.environ.get("CONFIRM_DONE_TEXT", "Hecho.")
CONFIRM_CANCELLED_TEXT = os.environ.get("CONFIRM_CANCELLED_TEXT", "Vale, no hago nada.")
CONFIRM_FAILED_TEXT = os.environ.get("CONFIRM_FAILED_TEXT", "No he podido hacerlo.")
# A yes/no reply is short; anything longer is treated as a new question.
CONFIRM_MAX_WORDS = 4

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="familia-proxy %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("familia.proxy")

# Reuse one botocore session (credential lookup is cached).
_session = Session()

# MCP bridge (live home tools). Imported lazily so the proxy keeps working
# without the `mcp` package when tools are not configured.
_bridge = None


def _get_bridge():
    global _bridge
    if _bridge is None and MCP_SERVER_COMMAND:
        from familia_mcp_bridge import McpBridge

        _bridge = McpBridge(MCP_SERVER_COMMAND)
    return _bridge


def _sigv4_ask(body: dict) -> dict:
    """Sign and POST a request body to the familIA /ask orchestrator endpoint."""
    if not FAMILIA_API_URL:
        raise RuntimeError("FAMILIA_API_URL is not set.")
    payload = json.dumps(body, ensure_ascii=False)
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


# --------------------------------------------------------------------------
# Spoken confirmation (out of band: the model can never confirm by itself)
# --------------------------------------------------------------------------
_pending_lock = threading.Lock()
_pending: dict | None = None  # {"token", "tool", "expires"}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _word_set(csv: str) -> set[str]:
    return {_fold(w).strip() for w in csv.split(",") if w.strip()}


def _yes_no(text: str) -> str | None:
    """'yes', 'no' or None (not a short yes/no reply)."""
    words = [w for w in re.split(r"[^0-9a-z]+", _fold(text)) if w]
    if not words or len(words) > CONFIRM_MAX_WORDS:
        return None
    joined = " ".join(words)
    no_words, yes_words = _word_set(CONFIRM_NO_WORDS), _word_set(CONFIRM_YES_WORDS)
    if any(w in no_words for w in words):
        return "no"
    if words[0] in yes_words or any(" " in y and joined.startswith(y) for y in yes_words):
        return "yes"
    return None


def _strip_confirmation(tool_result: dict) -> dict:
    """Keep a parked action's token locally and hide it from the model."""
    global _pending
    content = tool_result.get("content")
    conf = content.get("confirmation") if isinstance(content, dict) else None
    if isinstance(conf, dict) and conf.get("confirmation_token") and conf.get("confirm_tool"):
        with _pending_lock:
            _pending = {
                "token": conf["confirmation_token"],
                "tool": conf["confirm_tool"],
                "expires": time.monotonic() + CONFIRM_TTL_S,
            }
        conf = {k: v for k, v in conf.items() if k not in ("confirmation_token", "confirm_tool")}
        tool_result = {**tool_result, "content": {**content, "confirmation": conf}}
    return tool_result


def _take_pending() -> dict | None:
    """Pending confirmation, if still valid. Single chance: always consumed."""
    global _pending
    with _pending_lock:
        pending, _pending = _pending, None
    if pending and pending["expires"] >= time.monotonic():
        return pending
    return None


def _resolve_confirmation(question: str, bridge) -> str | None:
    """Answer a yes/no to a parked action, or None to treat as a new question."""
    pending = _take_pending()
    if not pending or not bridge:
        return None
    verdict = _yes_no(question)
    if verdict == "no":
        logger.info("parked action cancelled by the user")
        return CONFIRM_CANCELLED_TEXT
    if verdict != "yes":
        return None  # a different question: the parked action is dropped
    result = bridge.run_tool_call(
        {"id": "confirm", "name": pending["tool"], "input": {"confirmation_token": pending["token"]}},
        internal=True,
    )
    content = result.get("content")
    results = content.get("results") if isinstance(content, dict) else None
    ok = result.get("status") == "success" and results and all(r.get("status") == "done" for r in results)
    logger.info("parked action confirmed: %s", "done" if ok else "failed")
    return CONFIRM_DONE_TEXT if ok else CONFIRM_FAILED_TEXT


def _handle_question(question: str, bridge=None) -> str:
    confirmed = _resolve_confirmation(question, bridge)
    if confirmed is not None:
        return confirmed
    return _answer(question, bridge)


def _answer(question: str, bridge=None) -> str:
    """Ask the orchestrator, running any requested home tools locally.

    Without a bridge (or with the MCP Pi down) this is a single /ask call, as
    before. With tools, the orchestrator may answer {mode: "tool_use"}; the
    tool calls run here over MCP and the results go back in the next /ask.
    """
    request = {
        "question": question,
        # Local time with UTC offset: lets the model resolve "hoy"/"ayer" in
        # the household's timezone without hard-coding one anywhere.
        "client_time": datetime.now().astimezone().isoformat(timespec="minutes"),
    }
    tools = bridge.tool_specs() if bridge else []
    if tools:
        request["tools"] = tools

    result = _sigv4_ask(request)
    rounds = 0
    while result.get("mode") == "tool_use":
        rounds += 1
        calls = result.get("tool_calls") or []
        if not bridge or rounds > MAX_TOOL_ROUNDS or not calls or len(calls) > MAX_CALLS_PER_ROUND:
            logger.warning("stopping tool loop (round %d, %d calls)", rounds, len(calls))
            return FALLBACK_ANSWER
        logger.info("tool round %d: %s", rounds, ", ".join(str(c.get("name")) for c in calls))
        tool_results = [_strip_confirmation(bridge.run_tool_call(c)) for c in calls]
        result = _sigv4_ask(
            {**request, "messages": result.get("messages"), "tool_results": tool_results}
        )
    return result.get("answer", "") or FALLBACK_ANSWER


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


def _openai_stream_chunks(answer: str):
    """OpenAI-style SSE chunks: role, content, flush, finish.

    The OVOS solver only speaks text once a chunk ends in sentence punctuation,
    skips chunks ending in "<digit>." and never flushes leftovers at the end.
    A trailing newline chunk forces it to speak answers like "...de 2025."
    """
    base = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
    }
    deltas = [
        ({"role": "assistant"}, None),
        ({"content": answer}, None),
        ({"content": "\n"}, None),
        ({}, "stop"),
    ]
    for delta, finish in deltas:
        yield {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class Handler(BaseHTTPRequestHandler):
    server_version = "familia-ovos-proxy/1.0"

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, answer):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in _openai_stream_chunks(answer):
            line = "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

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
        # Diagnostic only: tells us whether OVOS sends conversation history.
        logger.debug("OVOS sent %d message(s)", len(body.get("messages") or []))

        try:
            answer = _handle_question(question, _get_bridge())
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 502
            return self._send(502, {"error": {"message": f"Upstream error ({code})"}})
        except Exception as exc:  # noqa: BLE001 - surface a clean error to OVOS
            logger.exception("proxy error")
            return self._send(502, {"error": {"message": f"Proxy error: {exc}"}})

        if body.get("stream"):
            return self._send_stream(answer)
        return self._send(200, _openai_chat_response(answer))


def main():
    if not FAMILIA_API_URL:
        raise SystemExit("Set FAMILIA_API_URL (the /ask endpoint) before starting.")
    bridge = _get_bridge()
    if bridge:
        # Warm up the MCP session so the first spoken question is not slower.
        logger.info("live tools: %d available", len(bridge.tool_specs()))
    httpd = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), Handler)
    print(f"familIA OVOS proxy on http://{PROXY_HOST}:{PROXY_PORT}/v1 -> {FAMILIA_API_URL}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
