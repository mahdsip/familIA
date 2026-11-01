"""familIA orchestrator Lambda (routing + language layer).

This is the ONLY module that speaks natural language to the user. It sits in
front of the RAG retriever and stays completely independent from it: it never
touches the vector store directly, only invoking the retriever Lambda through
`lambda:InvokeFunction`.

Flow
----
1. INTENT + EXTRACT (one cheap LLM call): classify whether the question is
   personal/family-related, and — restricted to the known owners/topics passed
   via env — extract an `owner`/`topic` and an `optimized_query` reformulated
   for vector search.
2. ROUTE:
     * personal  -> ALWAYS query the RAG retriever FIRST, with the extracted
                    owner/topic filter and optimized query. If the retriever
                    returns relevant chunks, generate a grounded answer from
                    them (with source citations). If it returns NOTHING, STOP
                    and say it was not found in the documents (no fallback to
                    general knowledge — privacy/accuracy first).
     * general   -> answer from the model's own general knowledge; the RAG is
                    not consulted.
     * live      -> (only when the client declares `tools`) live household
                    data: device health, home automation, media, photos.
                    See "Return of control" below.

Return of control (live tools)
------------------------------
The household services (Prometheus, Home Assistant, Plex, PhotoPrism) live on
the home LAN and are never exposed to AWS. The Pi-side proxy declares the
tools it can run (from its MCP server) in the request; this Lambda runs the
Bedrock Converse tool loop but, instead of executing a tool, RETURNS the
model's tool request to the proxy:

    Pi  -> {question, tools}                         -> Lambda
    Pi  <- {mode: "tool_use", tool_calls, messages}  <- Lambda
    Pi runs the tools locally via MCP
    Pi  -> {question, tools, messages, tool_results} -> Lambda
    Pi  <- {mode: "live", answer}                    <- Lambda

The Lambda stays stateless: the Converse transcript (`messages`) travels with
the client between rounds. Requests without `tools` behave exactly as before.

Privacy
-------
No personal data is hard-coded here. The list of valid owners/topics is
injected at deploy time from a gitignored variable (env: OWNERS, TOPICS), so
the public repo never contains family names. The model may only map to values
from that injected list.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

REGION = os.environ.get("AWS_REGION", "eu-central-1")
ORCHESTRATOR_MODEL_ARN = os.environ["ORCHESTRATOR_MODEL_ARN"]
RETRIEVER_FUNCTION_NAME = os.environ["RETRIEVER_FUNCTION_NAME"]
NUM_RESULTS = int(os.environ.get("NUM_RESULTS", "8"))
MAX_QUESTION_CHARS = int(os.environ.get("MAX_QUESTION_CHARS", "1000"))
# Minimum retrieval score for a chunk to count as "relevant". Below this the
# retriever is treated as having found nothing (personal question -> stop).
MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.0"))

# Valid owners/topics come from a gitignored deploy variable — never the repo.
# Format: comma-separated keys, e.g. "owner_a,owner_b,owner_c".
KNOWN_OWNERS = [o.strip() for o in os.environ.get("OWNERS", "").split(",") if o.strip()]
KNOWN_TOPICS = [t.strip() for t in os.environ.get("TOPICS", "").split(",") if t.strip()]

# Live tools (return of control). Limits bound what an (authenticated) client
# can make the Lambda send to Bedrock.
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "3"))
MAX_TOOLS = int(os.environ.get("MAX_TOOLS", "40"))
MAX_TOOLS_JSON_CHARS = int(os.environ.get("MAX_TOOLS_JSON_CHARS", "40000"))
MAX_MESSAGES_JSON_CHARS = int(os.environ.get("MAX_MESSAGES_JSON_CHARS", "60000"))
MAX_TOOL_RESULT_CHARS = int(os.environ.get("MAX_TOOL_RESULT_CHARS", "8000"))
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=REGION,
    config=Config(retries={"max_attempts": 3, "mode": "adaptive"}),
)
_lambda = boto3.client(
    "lambda",
    region_name=REGION,
    config=Config(retries={"max_attempts": 2, "mode": "adaptive"}),
)


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


# --------------------------------------------------------------------------
# Step 1: intent classification + entity extraction + query optimization
# --------------------------------------------------------------------------
_INTENT_SYSTEM = (
    "Eres un enrutador de consultas para un asistente familiar privado. "
    "Recibes una pregunta del usuario y devuelves EXCLUSIVAMENTE un objeto JSON "
    "válido (sin texto adicional, sin markdown) con esta forma exacta:\n"
    '{"is_personal": true|false, "is_live": true|false, "owner": <clave|null>, '
    '"topic": <clave|null>, "optimized_query": "<texto>"}\n\n'
    "Reglas:\n"
    "- is_live = true SOLO si se te indica una lista de herramientas en vivo y la "
    "pregunta necesita datos actuales o históricos que esas herramientas "
    "proporcionan (estado de dispositivos o servicios de la casa, domótica, "
    "multimedia, fotos). Si no hay herramientas en vivo, is_live = false.\n"
    "- is_personal = true SOLO si la pregunta trata sobre la familia del usuario, "
    "sus personas, documentos, salud, finanzas, papeles o vida personal. "
    "Preguntas de conocimiento general (historia, ciencia, definiciones, cómo "
    "hacer algo genérico) => is_personal = false.\n"
    "- owner: si la pregunta menciona o implica a una persona concreta, mapéala "
    "a UNA clave EXACTA de la lista de owners válidos. Si no hay persona clara o "
    "no está en la lista, owner = null. NUNCA inventes una clave fuera de la lista.\n"
    "- topic: igual, mapea a UNA clave EXACTA de la lista de topics válidos, o null.\n"
    "- optimized_query: reformula la pregunta como una consulta de búsqueda "
    "semántica en español para una base de datos vectorial. CONSERVA los "
    "términos originales de la pregunta y AÑADE sinónimos y términos médicos/"
    "administrativos relacionados (p. ej. si preguntan por 'romperse la mano', "
    "incluye: fractura, muñeca, brazo, traumatología, urgencias, informe de "
    "alta, radiografía). Mantén nombres propios y datos tal cual.\n"
)


def _intent_prompt(question, tools=None):
    owners = ", ".join(KNOWN_OWNERS) if KNOWN_OWNERS else "(ninguna configurada)"
    topics = ", ".join(KNOWN_TOPICS) if KNOWN_TOPICS else "(ninguno configurado)"
    if tools:
        live = "\n".join(f"- {t['name']}: {_first_sentence(t['description'])}" for t in tools)
    else:
        live = "(ninguna)"
    return (
        f"Owners válidos: [{owners}]\n"
        f"Topics válidos: [{topics}]\n"
        f"Herramientas en vivo disponibles:\n{live}\n\n"
        f"Pregunta del usuario: {question}\n\n"
        "Devuelve solo el JSON."
    )


def _first_sentence(text, limit=160):
    text = " ".join((text or "").split())
    cut = text.find(". ")
    if cut != -1:
        text = text[: cut + 1]
    return text[:limit]


def _invoke_model(system, user_text, max_tokens=512, temperature=0.0):
    """Call the orchestrator model via the Bedrock Messages API (Anthropic)."""
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
    }
    resp = _bedrock.invoke_model(
        modelId=ORCHESTRATOR_MODEL_ARN,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(resp["body"].read())
    parts = payload.get("content", [])
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()


def _classify(question, tools=None):
    """Return (route, owner, topic, optimized_query) with safe fallbacks.

    route is "live" (only possible when tools were declared), "personal" or
    "general".
    """
    try:
        raw = _invoke_model(_INTENT_SYSTEM, _intent_prompt(question, tools), max_tokens=300)
        data = json.loads(_strip_json(raw))
    except Exception:
        logger.exception("intent classification failed; defaulting to personal")
        # Fail safe: treat as personal so we consult the documents rather than
        # answering from general knowledge about the family.
        return "personal", None, None, question

    is_live = bool(tools) and data.get("is_live") is True
    is_personal = bool(data.get("is_personal", True))
    owner = data.get("owner")
    topic = data.get("topic")
    optimized = (data.get("optimized_query") or "").strip() or question

    # Guardrail: the model may only use keys from the injected lists.
    if owner not in KNOWN_OWNERS:
        owner = None
    if topic not in KNOWN_TOPICS:
        topic = None
    route = "live" if is_live else ("personal" if is_personal else "general")
    return route, owner, topic, optimized


def _strip_json(text):
    """Best-effort extraction of a JSON object from a model reply."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


# --------------------------------------------------------------------------
# Step 2: call the RAG retriever (separate module) via Lambda invoke
# --------------------------------------------------------------------------
def _retrieve(query, owner, topic):
    payload = {"query": query, "numberOfResults": NUM_RESULTS}
    if owner:
        payload["owner"] = owner
    if topic:
        payload["topic"] = topic

    resp = _lambda.invoke(
        FunctionName=RETRIEVER_FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    raw = resp["Payload"].read().decode("utf-8")
    outer = json.loads(raw)
    # The retriever returns an API-Gateway-style envelope; unwrap its body.
    body = outer.get("body")
    if isinstance(body, str):
        body = json.loads(body)
    elif body is None:
        body = outer
    return body.get("chunks", []) if isinstance(body, dict) else []


# --------------------------------------------------------------------------
# Step 3: generate the final answer from retrieved chunks
# --------------------------------------------------------------------------
# Answers are spoken aloud by the voice assistant, so both answer prompts share
# the same spoken style and a short token budget.
_VOICE_STYLE = (
    "Tu respuesta se leerá en voz alta en un asistente de voz: responde en el "
    "mismo idioma que la pregunta, con un estilo hablado y natural, normalmente "
    "en una o dos frases cortas. Empieza directamente con la respuesta. Sin "
    "emojis, sin markdown, sin viñetas, sin aclaraciones entre paréntesis y sin "
    "preguntas de cierre."
)
ANSWER_MAX_TOKENS = 300

_ANSWER_SYSTEM = (
    "Eres un asistente familiar privado. Responde a la pregunta del usuario "
    "usando ÚNICAMENTE la información de los fragmentos de documentos que se te "
    "proporcionan. No inventes datos. Si los fragmentos no contienen la "
    "respuesta, dilo claramente. Incluye los datos concretos que respondan a la "
    "pregunta (fechas, nombres, valores) tal como aparecen. " + _VOICE_STYLE
)


def _generate_answer(question, chunks):
    context_blocks = []
    for i, c in enumerate(chunks, 1):
        src = c.get("source_path") or c.get("uri") or "documento"
        context_blocks.append(f"[Fragmento {i}] (fuente: {src})\n{c.get('text', '')}")
    context = "\n\n".join(context_blocks)
    user_text = (
        f"Fragmentos de documentos:\n{context}\n\n"
        f"Pregunta: {question}\n\nRespuesta:"
    )
    return _invoke_model(_ANSWER_SYSTEM, user_text, max_tokens=ANSWER_MAX_TOKENS, temperature=0.0)


def _general_answer(question):
    system = (
        "Eres un asistente útil. Responde a la pregunta con tu conocimiento "
        "general. " + _VOICE_STYLE
    )
    return _invoke_model(system, question, max_tokens=ANSWER_MAX_TOKENS, temperature=0.2)


def _sources_from_chunks(chunks):
    sources, seen = [], set()
    for c in chunks:
        uri = c.get("uri")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        sources.append(
            {
                "uri": uri,
                "topic": c.get("topic"),
                "owner": c.get("owner"),
                "source_path": c.get("source_path"),
                "captured_date": c.get("captured_date"),
                "modified_date": c.get("modified_date"),
            }
        )
    return sources


# --------------------------------------------------------------------------
# Live tools: Bedrock Converse with return of control
# --------------------------------------------------------------------------
LIVE_MAX_TOKENS = 600
_LIVE_GAVE_UP = "No he podido completar la consulta con los datos de casa."
_LIVE_EMPTY = "No he obtenido datos para responder a eso."

_LIVE_SYSTEM = (
    "Eres el asistente de voz de una casa. Dispones de herramientas que consultan "
    "en tiempo real los dispositivos y servicios del hogar. Usa las herramientas "
    "necesarias y responde SOLO con los datos que devuelvan; no inventes valores. "
    "Si una herramienta falla o no devuelve datos, dilo brevemente. Si el nombre "
    "de un dispositivo es ambiguo, menciona las opciones. Nunca ejecutes una "
    "acción que el usuario no haya pedido explícitamente. Redondea las cifras al "
    "decirlas. "
)


class _BadRequest(ValueError):
    """Client sent a malformed live-tools request (-> HTTP 400)."""


def _validate_tools(raw):
    """Normalize the client's tool declarations, or [] when absent."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _BadRequest("'tools' must be a list.")
    if len(raw) > MAX_TOOLS:
        raise _BadRequest("Too many tools.")
    if len(json.dumps(raw)) > MAX_TOOLS_JSON_CHARS:
        raise _BadRequest("Tool declarations too large.")
    tools, seen = [], set()
    for t in raw:
        if not isinstance(t, dict):
            raise _BadRequest("Each tool must be an object.")
        name = t.get("name")
        desc = t.get("description") or name
        schema = t.get("input_schema") or {"type": "object", "properties": {}}
        if not isinstance(name, str) or not _TOOL_NAME_RE.match(name) or name in seen:
            raise _BadRequest("Invalid or duplicated tool name.")
        if not isinstance(desc, str) or not isinstance(schema, dict):
            raise _BadRequest(f"Invalid declaration for tool {name}.")
        seen.add(name)
        tools.append({"name": name, "description": desc[:1024], "input_schema": schema})
    return tools


def _tool_config(tools):
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": {"json": t["input_schema"]},
                }
            }
            for t in tools
        ]
    }


def _validate_messages(raw):
    """The Converse transcript echoed back by the client between rounds."""
    if not isinstance(raw, list) or not raw:
        raise _BadRequest("'messages' must be a non-empty list.")
    if len(json.dumps(raw)) > MAX_MESSAGES_JSON_CHARS:
        raise _BadRequest("Conversation too large.")
    for i, m in enumerate(raw):
        expected_role = "user" if i % 2 == 0 else "assistant"
        if not isinstance(m, dict) or m.get("role") != expected_role or not isinstance(m.get("content"), list):
            raise _BadRequest("Malformed 'messages'.")
    if raw[-1]["role"] != "assistant":
        raise _BadRequest("'messages' must end with the assistant tool request.")
    return raw


def _tool_uses(message):
    return [
        b["toolUse"]
        for b in message.get("content", [])
        if isinstance(b, dict) and isinstance(b.get("toolUse"), dict)
    ]


def _tool_rounds(messages):
    return sum(1 for m in messages if m.get("role") == "assistant" and _tool_uses(m))


def _tool_result_block(result, expected_ids):
    if not isinstance(result, dict):
        raise _BadRequest("Each tool result must be an object.")
    tool_use_id = result.get("tool_use_id")
    if tool_use_id not in expected_ids:
        raise _BadRequest("Tool result for an unknown tool_use_id.")
    status = "error" if result.get("status") == "error" else "success"
    content = result.get("content")
    if isinstance(content, dict):
        text = json.dumps(content, ensure_ascii=False)
        block = {"json": content} if len(text) <= MAX_TOOL_RESULT_CHARS else {"text": text[:MAX_TOOL_RESULT_CHARS]}
    else:
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        block = {"text": text[:MAX_TOOL_RESULT_CHARS] or "(sin datos)"}
    return {"toolResult": {"toolUseId": tool_use_id, "content": [block], "status": status}}


def _client_now(raw):
    """User's local time. The client (Pi) sends it with its UTC offset, so the
    household timezone comes from the device, not from this code."""
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is not None:
                return parsed.isoformat(timespec="minutes")
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat(timespec="minutes")


def _converse(messages, tools, now):
    system = (
        _LIVE_SYSTEM
        + f"Fecha y hora actuales del usuario: {now}. Interpreta 'hoy', 'ayer' o "
        "'esta semana' respecto a esa fecha. "
        + _VOICE_STYLE
    )
    resp = _bedrock.converse(
        modelId=ORCHESTRATOR_MODEL_ARN,
        system=[{"text": system}],
        messages=messages,
        toolConfig=_tool_config(tools),
        inferenceConfig={"maxTokens": LIVE_MAX_TOKENS, "temperature": 0.0},
    )
    return resp["output"]["message"], resp.get("stopReason")


def _live_answer(answer):
    return {"answer": answer, "mode": "live", "owner": None, "topic": None, "sources": []}


def _live_turn(messages, tools, now):
    """One Converse round. Returns either a tool request or the final answer."""
    message, stop_reason = _converse(messages, tools, now)
    messages = messages + [message]
    uses = _tool_uses(message)
    if stop_reason == "tool_use" and uses:
        declared = {t["name"] for t in tools}
        if any(u.get("name") not in declared for u in uses):
            logger.warning("model requested an undeclared tool")
            return _live_answer(_LIVE_GAVE_UP)
        if _tool_rounds(messages) > MAX_TOOL_ROUNDS:
            logger.info("tool round limit reached")
            return _live_answer(_LIVE_GAVE_UP)
        return {
            "mode": "tool_use",
            "tool_calls": [
                {"id": u["toolUseId"], "name": u["name"], "input": u.get("input") or {}} for u in uses
            ],
            "messages": messages,
        }
    text = "".join(
        b.get("text", "") for b in message.get("content", []) if isinstance(b, dict)
    ).strip()
    return _live_answer(text or _LIVE_EMPTY)


def _continue_live(payload, tools, now):
    """Second+ round: append the client's tool results and ask the model again."""
    if not tools:
        raise _BadRequest("'tools' must be resent with tool results.")
    messages = _validate_messages(payload.get("messages"))
    expected = {u.get("toolUseId") for u in _tool_uses(messages[-1])}
    results = payload.get("tool_results")
    if not expected or not isinstance(results, list):
        raise _BadRequest("'tool_results' must answer the pending tool request.")
    blocks = [_tool_result_block(r, expected) for r in results]
    if {b["toolResult"]["toolUseId"] for b in blocks} != expected or len(blocks) != len(expected):
        raise _BadRequest("Every pending tool request needs exactly one result.")
    return _live_turn(messages + [{"role": "user", "content": blocks}], tools, now)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def handler(event, context):
    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw_body = base64.b64decode(raw_body).decode("utf-8")
    try:
        payload = json.loads(raw_body)
    except (ValueError, TypeError):
        return _response(400, {"error": "Body must be valid JSON."})

    question = (payload.get("question") or payload.get("query") or "").strip()
    if not question:
        return _response(400, {"error": "Missing 'question' field."})
    if len(question) > MAX_QUESTION_CHARS:
        return _response(400, {"error": "Question too long."})

    try:
        tools = _validate_tools(payload.get("tools"))
        now = _client_now(payload.get("client_time"))
        if payload.get("messages") is not None or payload.get("tool_results") is not None:
            # Return-of-control continuation: skip classification.
            return _response(200, _continue_live(payload, tools, now))
    except _BadRequest as exc:
        return _response(400, {"error": str(exc)})
    except Exception:
        logger.exception("live tool round failed")
        return _response(502, {"error": "Live tools layer error."})

    route, owner, topic, optimized = _classify(question, tools)

    if route == "live":
        try:
            first = [{"role": "user", "content": [{"text": question}]}]
            return _response(200, _live_turn(first, tools, now))
        except Exception:
            logger.exception("live tool round failed")
            return _response(502, {"error": "Live tools layer error."})

    if route == "general":
        # General knowledge — the RAG is never consulted.
        answer = _general_answer(question)
        return _response(
            200,
            {"answer": answer, "mode": "general", "owner": None, "topic": None, "sources": []},
        )

    # Personal question: ALWAYS query the RAG first.
    try:
        chunks = _retrieve(optimized, owner, topic)
    except Exception:
        logger.exception("retriever invoke failed")
        return _response(502, {"error": "Retrieval layer error."})

    if MIN_SCORE > 0:
        chunks = [c for c in chunks if (c.get("score") or 0) >= MIN_SCORE]

    if not chunks:
        # Fallback policy: STOP. Do not answer from general knowledge.
        return _response(
            200,
            {
                "answer": "No he encontrado esa información en los documentos familiares.",
                "mode": "personal_not_found",
                "owner": owner,
                "topic": topic,
                "sources": [],
            },
        )

    answer = _generate_answer(question, chunks)
    return _response(
        200,
        {
            "answer": answer,
            "mode": "personal",
            "owner": owner,
            "topic": topic,
            "sources": _sources_from_chunks(chunks),
        },
    )
