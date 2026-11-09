"""OVOS proxy: return-of-control loop and the MCP bridge (real stdio server)."""

import json
import shlex
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import _support  # noqa: F401  (puts scripts/ on sys.path)
import familia_mcp_bridge as bridge_mod
import ovos_openai_proxy as proxy


# --------------------------------------------------------------- proxy loop
class FakeBridge:
    def __init__(self, tools=("prom_devices_overview",)):
        self.tools = [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in tools]
        self.calls = []

    def tool_specs(self):
        return self.tools

    def run_tool_call(self, call):
        self.calls.append(call)
        return {"tool_use_id": call["id"], "status": "success", "content": {"ok": True}}


@pytest.fixture
def upstream(monkeypatch):
    sent = []
    replies = []

    def fake_ask(body):
        sent.append(body)
        return replies.pop(0)

    monkeypatch.setattr(proxy, "_sigv4_ask", fake_ask)
    return sent, replies


def test_no_bridge_single_call_without_tools(upstream):
    sent, replies = upstream
    replies.append({"answer": "Hola", "mode": "general"})
    assert proxy._answer("hola", None) == "Hola"
    assert len(sent) == 1 and "tools" not in sent[0]
    assert sent[0]["client_time"][4] == "-" and sent[0]["client_time"][-6] in "+-"


def test_tool_loop_runs_tools_and_resends_transcript(upstream):
    sent, replies = upstream
    msgs = [{"role": "user", "content": [{"text": "q"}]}, {"role": "assistant", "content": []}]
    replies += [
        {"mode": "tool_use", "tool_calls": [{"id": "t1", "name": "prom_devices_overview", "input": {}}], "messages": msgs},
        {"answer": "Todo bien", "mode": "live"},
    ]
    fb = FakeBridge()
    assert proxy._answer("¿cómo están mis dispositivos?", fb) == "Todo bien"
    assert sent[0]["tools"] == fb.tools
    assert sent[1]["messages"] == msgs
    assert sent[1]["tool_results"] == [{"tool_use_id": "t1", "status": "success", "content": {"ok": True}}]
    assert sent[1]["question"] == sent[0]["question"] and sent[1]["tools"] == fb.tools


def test_tool_loop_is_capped(upstream, monkeypatch):
    sent, replies = upstream
    monkeypatch.setattr(proxy, "MAX_TOOL_ROUNDS", 2)
    loop = {"mode": "tool_use", "tool_calls": [{"id": "t", "name": "prom_devices_overview"}], "messages": []}
    replies += [loop, loop, loop, loop]
    assert proxy._answer("q", FakeBridge()) == proxy.FALLBACK_ANSWER
    assert len(sent) == 3  # initial + 2 rounds, then stop


def test_tool_use_without_bridge_falls_back(upstream):
    _, replies = upstream
    replies.append({"mode": "tool_use", "tool_calls": [{"id": "t", "name": "x"}], "messages": []})
    assert proxy._answer("q", None) == proxy.FALLBACK_ANSWER


# ------------------------------------- bridge <-> real familia-mcp over stdio
class _FakePromHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/v1/targets"):
            data = {
                "activeTargets": [
                    {"labels": {"instance": "pihole.local:9100", "job": "node"}, "health": "up", "lastError": ""}
                ]
            }
        elif self.path.startswith("/api/v1/query"):
            data = {"resultType": "vector", "result": [{"metric": {"instance": "pihole.local:9100"}, "value": [0, "42"]}]}
        else:
            data = {"alerts": []}
        body = json.dumps({"status": "success", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_prom_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakePromHandler)  # loopback only
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_bridge_end_to_end_with_real_server(fake_prom_url, tmp_path):
    # Same shape as production (a command line), minus ssh.
    command = " ".join(
        shlex.quote(a)
        for a in (
            "env",
            f"PROMETHEUS_URL={fake_prom_url}",
            f"FAMILIA_MCP_ENV_FILE={tmp_path / 'none.env'}",
            sys.executable,
            "-m",
            "familia_mcp",
        )
    )
    b = bridge_mod.McpBridge(command, connect_timeout_s=30)
    try:
        specs = b.tool_specs()
        names = {s["name"] for s in specs}
        assert "prom_device_status" in names
        status = next(s for s in specs if s["name"] == "prom_device_status")
        assert status["input_schema"]["required"] == ["device"]

        ok = b.run_tool_call({"id": "t1", "name": "prom_device_status", "input": {"device": "pihole"}})
        assert ok["status"] == "success" and ok["tool_use_id"] == "t1"
        assert isinstance(ok["content"], dict) and ok["content"]["name"] == "pihole"
        assert ok["content"]["cpu_percent"] == 42.0

        bad = b.run_tool_call({"id": "t2", "name": "prom_device_status", "input": {"device": "nevera"}})
        assert bad["status"] == "error" and "Unknown device" in json.dumps(bad["content"])

        # Tools not listed by the server are refused locally, before MCP.
        refused = b.run_tool_call({"id": "t3", "name": "ha_unlock_door", "input": {}})
        assert refused == {"tool_use_id": "t3", "status": "error", "content": "Tool 'ha_unlock_door' is not available."}
    finally:
        b.close()


def test_bridge_unreachable_server_reports_no_tools():
    b = bridge_mod.McpBridge(f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(1)'", connect_timeout_s=10)
    try:
        assert b.tool_specs() == []
        # Retry is rate-limited: an immediate second ask does not respawn.
        assert b.ensure_connected() is False
    finally:
        b.close()


def test_result_content_truncates():
    class R:
        structured_content = {"a": "x" * 50}
        content = []

    out = bridge_mod.result_content(R(), 20)
    assert isinstance(out, str) and len(out) == 20


# ----------------------------------------------------- spoken confirmation
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Sí", "yes"),
        ("sí, apágalo", "yes"),
        ("vale", "yes"),
        ("de acuerdo", "yes"),
        ("No", "no"),
        ("no, déjalo", "no"),
        ("sí... no, espera", "no"),  # any no wins (safe side)
        ("¿qué tiempo hace mañana en Madrid?", None),  # long -> new question
        ("enciende la luz", None),
        ("", None),
    ],
)
def test_yes_no(text, expected):
    assert proxy._yes_no(text) == expected


@pytest.fixture(autouse=True)
def _no_pending():
    proxy._pending = None
    yield
    proxy._pending = None


class _FakeHAHandler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.headers["Authorization"] == "Bearer t0k"
        self._json([
            {"entity_id": "switch.enchufe_nas", "state": "on", "attributes": {"friendly_name": "Enchufe NAS"}},
            {"entity_id": "light.cocina", "state": "off", "attributes": {"friendly_name": "Luz Cocina"}},
        ])

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append((self.path, body))
        self._json([{"entity_id": body["entity_id"], "state": "off"}])


@pytest.fixture
def ha_bridge(tmp_path):
    _FakeHAHandler.calls = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHAHandler)  # loopback only
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    allow = tmp_path / "ha_allowlist.json"
    allow.write_text(json.dumps({"allow": [
        {"entity_id": "light.*"},
        {"entity_id": "switch.enchufe_nas", "confirm": True},
    ]}), encoding="utf-8")
    command = " ".join(shlex.quote(a) for a in (
        "env",
        f"HA_URL=http://127.0.0.1:{httpd.server_address[1]}",
        "HA_TOKEN=t0k",
        f"HA_ALLOWLIST_FILE={allow}",
        f"FAMILIA_MCP_ENV_FILE={tmp_path / 'none.env'}",
        sys.executable, "-m", "familia_mcp",
    ))
    b = bridge_mod.McpBridge(command, connect_timeout_s=30)
    yield b
    b.close()
    httpd.shutdown()


def test_internal_tool_hidden_and_refused(ha_bridge):
    names = {s["name"] for s in ha_bridge.tool_specs()}
    assert "ha_control" in names and "ha_confirm_action" not in names
    refused = ha_bridge.run_tool_call({"id": "x", "name": "ha_confirm_action", "input": {"confirmation_token": "t"}})
    assert refused["status"] == "error" and "not available" in refused["content"]


def test_confirmation_end_to_end(ha_bridge, upstream):
    sent, replies = upstream
    msgs = [{"role": "user", "content": [{"text": "q"}]}, {"role": "assistant", "content": []}]
    replies += [
        {"mode": "tool_use", "messages": msgs, "tool_calls": [
            {"id": "t1", "name": "ha_control", "input": {"devices": ["nas"], "action": "turn_off"}}]},
        {"mode": "live", "answer": "¿Confirmas que apague el enchufe del NAS?"},
    ]
    assert proxy._handle_question("apaga el nas", ha_bridge) == "¿Confirmas que apague el enchufe del NAS?"
    assert _FakeHAHandler.calls == []  # parked, not executed

    # The token never leaves the Pi: the model only sees the instructions.
    conf = sent[1]["tool_results"][0]["content"]["confirmation"]
    assert "confirmation_token" not in conf and "confirm_tool" not in conf
    assert conf["devices"] == ["Enchufe NAS"]

    # User says yes -> the proxy confirms locally, no /ask round trip.
    assert proxy._handle_question("sí", ha_bridge) == proxy.CONFIRM_DONE_TEXT
    assert _FakeHAHandler.calls == [("/api/services/switch/turn_off", {"entity_id": "switch.enchufe_nas"})]
    assert len(sent) == 2

    # A second "sí" has nothing pending: it goes to /ask as a normal question.
    replies.append({"mode": "general", "answer": "¿Sí a qué?"})
    assert proxy._handle_question("sí", ha_bridge) == "¿Sí a qué?"
    assert len(_FakeHAHandler.calls) == 1


def test_confirmation_cancel_and_expiry(ha_bridge, upstream, monkeypatch):
    _, replies = upstream
    tool_round = {"mode": "tool_use", "messages": [{"role": "user", "content": []}, {"role": "assistant", "content": []}],
                  "tool_calls": [{"id": "t1", "name": "ha_control", "input": {"devices": ["nas"], "action": "turn_off"}}]}
    replies += [tool_round, {"mode": "live", "answer": "¿Confirmas?"}]
    proxy._handle_question("apaga el nas", ha_bridge)
    assert proxy._handle_question("no", ha_bridge) == proxy.CONFIRM_CANCELLED_TEXT

    monkeypatch.setattr(proxy, "CONFIRM_TTL_S", -1)
    replies += [tool_round, {"mode": "live", "answer": "¿Confirmas?"}, {"mode": "general", "answer": "otra"}]
    proxy._handle_question("apaga el nas", ha_bridge)
    assert proxy._handle_question("sí", ha_bridge) == "otra"  # expired -> normal question
    assert _FakeHAHandler.calls == []
