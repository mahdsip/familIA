"""Orchestrator: /ask contract with live tools (return of control)."""

import io
import json

import pytest

from _support import load_orchestrator

h = load_orchestrator()

TOOLS = [
    {
        "name": "prom_devices_overview",
        "description": "Health of ALL devices monitored by Prometheus. More text.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "prom_device_status",
        "description": "Status of one device.",
        "input_schema": {
            "type": "object",
            "properties": {"device": {"type": "string"}},
            "required": ["device"],
        },
    },
]


class FakeBedrock:
    """invoke_model -> classifier JSON; converse -> scripted responses."""

    def __init__(self, intent, converse_replies=()):
        self.intent = intent
        self.converse_replies = list(converse_replies)
        self.invoke_calls = []
        self.converse_calls = []

    def invoke_model(self, **kw):
        self.invoke_calls.append(json.loads(kw["body"]))
        if isinstance(self.intent, list):  # sequence of raw model texts
            text = self.intent[len(self.invoke_calls) - 1]
        else:
            text = json.dumps(self.intent)
        return {"body": io.BytesIO(json.dumps({"content": [{"type": "text", "text": text}]}).encode())}

    def converse(self, **kw):
        self.converse_calls.append(kw)
        return self.converse_replies.pop(0)


def tool_use_reply(*uses):
    content = [{"text": "Voy a mirarlo."}] + [
        {"toolUse": {"toolUseId": uid, "name": name, "input": inp}} for uid, name, inp in uses
    ]
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": "tool_use"}


def text_reply(text):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
    }


LIVE_INTENT = {"is_personal": False, "is_live": True, "owner": None, "topic": None, "optimized_query": "x"}


def call(body):
    resp = h.handler({"body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


@pytest.fixture
def bedrock(monkeypatch):
    def install(intent, replies=()):
        fake = FakeBedrock(intent, replies)
        monkeypatch.setattr(h, "_bedrock", fake)
        return fake

    return install


# ------------------------------------------------------------ backward compat
def test_without_tools_behaves_as_before(bedrock):
    # Even if the classifier claims "live", no tools -> never the live route.
    fake = bedrock([json.dumps(LIVE_INTENT), "París."])
    status, body = call({"question": "¿Capital de Francia?"})
    assert status == 200 and body["mode"] == "general" and body["answer"] == "París."
    assert fake.converse_calls == []


def test_classifier_prompt_lists_tools_only_when_declared(bedrock):
    assert "(ninguna)" in h._intent_prompt("q")
    prompt = h._intent_prompt("q", h._validate_tools(TOOLS))
    assert "- prom_devices_overview: Health of ALL devices monitored by Prometheus." in prompt
    assert "More text" not in prompt


# ------------------------------------------------------------- live round trip
def test_live_first_round_returns_tool_request(bedrock):
    fake = bedrock(LIVE_INTENT, [tool_use_reply(("t1", "prom_device_status", {"device": "pihole"}))])
    status, body = call(
        {"question": "¿Cómo está el pihole?", "tools": TOOLS, "client_time": "2026-10-31T18:05:00+01:00"}
    )
    assert status == 200
    assert body["mode"] == "tool_use"
    assert body["tool_calls"] == [{"id": "t1", "name": "prom_device_status", "input": {"device": "pihole"}}]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    sent = fake.converse_calls[0]
    assert sent["modelId"] == h.ORCHESTRATOR_MODEL_ARN
    assert "2026-10-31T18:05+01:00" in sent["system"][0]["text"]
    specs = sent["toolConfig"]["tools"]
    assert specs[1]["toolSpec"]["inputSchema"]["json"]["required"] == ["device"]


def test_live_continuation_returns_answer(bedrock):
    fake = bedrock(LIVE_INTENT, [tool_use_reply(("t1", "prom_device_status", {"device": "pihole"}))])
    _, first = call({"question": "¿Cómo está el pihole?", "tools": TOOLS})
    fake.converse_replies.append(text_reply("El pihole está bien, al 30 por ciento de disco."))
    status, body = call(
        {
            "question": "¿Cómo está el pihole?",
            "tools": TOOLS,
            "messages": first["messages"],
            "tool_results": [{"tool_use_id": "t1", "status": "success", "content": {"disk": 30}}],
        }
    )
    assert status == 200
    assert body == {
        "answer": "El pihole está bien, al 30 por ciento de disco.",
        "mode": "live",
        "owner": None,
        "topic": None,
        "sources": [],
    }
    # Classifier ran only on the first round.
    assert len(fake.invoke_calls) == 1
    last_user = fake.converse_calls[1]["messages"][-1]
    assert last_user == {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": "t1", "content": [{"json": {"disk": 30}}], "status": "success"}}],
    }


def test_tool_error_and_large_text_results(bedrock):
    fake = bedrock(LIVE_INTENT, [tool_use_reply(("a", "prom_device_status", {}), ("b", "prom_devices_overview", {}))])
    _, first = call({"question": "q", "tools": TOOLS})
    fake.converse_replies.append(text_reply("ok"))
    big = {"x": "y" * (h.MAX_TOOL_RESULT_CHARS + 10)}
    call(
        {
            "question": "q",
            "tools": TOOLS,
            "messages": first["messages"],
            "tool_results": [
                {"tool_use_id": "a", "status": "error", "content": "Unknown device"},
                {"tool_use_id": "b", "content": big},
            ],
        }
    )
    blocks = fake.converse_calls[1]["messages"][-1]["content"]
    assert blocks[0]["toolResult"]["status"] == "error"
    assert blocks[0]["toolResult"]["content"] == [{"text": "Unknown device"}]
    assert len(blocks[1]["toolResult"]["content"][0]["text"]) == h.MAX_TOOL_RESULT_CHARS


def test_round_limit_gives_up(bedrock):
    replies = [tool_use_reply((f"t{i}", "prom_devices_overview", {})) for i in range(h.MAX_TOOL_ROUNDS + 1)]
    fake = bedrock(LIVE_INTENT, replies)
    status, body = call({"question": "q", "tools": TOOLS})
    for i in range(h.MAX_TOOL_ROUNDS):
        assert body["mode"] == "tool_use"
        status, body = call(
            {
                "question": "q",
                "tools": TOOLS,
                "messages": body["messages"],
                "tool_results": [{"tool_use_id": f"t{i}", "content": {}}],
            }
        )
    assert status == 200 and body["mode"] == "live" and body["answer"] == h._LIVE_GAVE_UP
    assert len(fake.converse_calls) == h.MAX_TOOL_ROUNDS + 1


def test_model_requesting_undeclared_tool_gives_up(bedrock):
    bedrock(LIVE_INTENT, [tool_use_reply(("t1", "ha_unlock_door", {}))])
    _, body = call({"question": "q", "tools": TOOLS})
    assert body["mode"] == "live" and body["answer"] == h._LIVE_GAVE_UP


def test_classifier_not_live_with_tools_uses_rag(bedrock, monkeypatch):
    bedrock({"is_personal": True, "is_live": False, "owner": "owner_a", "topic": None, "optimized_query": "q"})
    monkeypatch.setattr(h, "_retrieve", lambda q, o, t: [])
    _, body = call({"question": "¿Cuándo vacunaron a A?", "tools": TOOLS})
    assert body["mode"] == "personal_not_found" and body["owner"] == "owner_a"


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize(
    "tools",
    [
        "nope",
        [{"name": "bad name!", "description": "d"}],
        [{"name": "a", "description": "d"}, {"name": "a", "description": "d"}],
        [{"name": "a", "input_schema": "notadict"}],
        [{"name": f"t{i}"} for i in range(h.MAX_TOOLS + 1)],
    ],
)
def test_invalid_tools_are_rejected(bedrock, tools):
    bedrock(LIVE_INTENT)
    status, body = call({"question": "q", "tools": tools})
    assert status == 400


def test_continuation_validation(bedrock):
    fake = bedrock(LIVE_INTENT, [tool_use_reply(("t1", "prom_devices_overview", {}))])
    _, first = call({"question": "q", "tools": TOOLS})
    base = {"question": "q", "tools": TOOLS, "messages": first["messages"]}

    assert call({**base, "tool_results": [{"tool_use_id": "zzz", "content": {}}]})[0] == 400
    assert call({**base, "tool_results": []})[0] == 400
    assert call({**base, "tools": None, "tool_results": [{"tool_use_id": "t1", "content": {}}]})[0] == 400
    swapped = list(reversed(first["messages"]))
    assert call({**base, "messages": swapped, "tool_results": [{"tool_use_id": "t1"}]})[0] == 400
    assert len(fake.converse_calls) == 1  # nothing invalid reached Bedrock


def test_bedrock_failure_is_502(bedrock):
    fake = bedrock(LIVE_INTENT)

    def boom(**kw):
        raise RuntimeError("throttled")

    fake.converse = boom
    status, body = call({"question": "q", "tools": TOOLS})
    assert status == 502 and body["error"] == "Live tools layer error."


def test_client_now():
    assert h._client_now("2026-10-31T18:05:33+01:00") == "2026-10-31T18:05+01:00"
    assert h._client_now("2026-10-31T18:05:33").endswith("+00:00")  # naive -> UTC now
    assert h._client_now(None).endswith("+00:00")
