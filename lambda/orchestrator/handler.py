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
    '{"is_personal": true|false, "owner": <clave|null>, '
    '"topic": <clave|null>, "optimized_query": "<texto>"}\n\n'
    "Reglas:\n"
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


def _intent_prompt(question):
    owners = ", ".join(KNOWN_OWNERS) if KNOWN_OWNERS else "(ninguna configurada)"
    topics = ", ".join(KNOWN_TOPICS) if KNOWN_TOPICS else "(ninguno configurado)"
    return (
        f"Owners válidos: [{owners}]\n"
        f"Topics válidos: [{topics}]\n\n"
        f"Pregunta del usuario: {question}\n\n"
        "Devuelve solo el JSON."
    )


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


def _classify(question):
    """Return (is_personal, owner, topic, optimized_query) with safe fallbacks."""
    try:
        raw = _invoke_model(_INTENT_SYSTEM, _intent_prompt(question), max_tokens=300)
        data = json.loads(_strip_json(raw))
    except Exception:
        logger.exception("intent classification failed; defaulting to personal")
        # Fail safe: treat as personal so we consult the documents rather than
        # answering from general knowledge about the family.
        return True, None, None, question

    is_personal = bool(data.get("is_personal", True))
    owner = data.get("owner")
    topic = data.get("topic")
    optimized = (data.get("optimized_query") or "").strip() or question

    # Guardrail: the model may only use keys from the injected lists.
    if owner not in KNOWN_OWNERS:
        owner = None
    if topic not in KNOWN_TOPICS:
        topic = None
    return is_personal, owner, topic, optimized


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
_ANSWER_SYSTEM = (
    "Eres un asistente familiar privado. Responde a la pregunta del usuario "
    "usando ÚNICAMENTE la información de los fragmentos de documentos que se te "
    "proporcionan. No inventes datos. Si los fragmentos no contienen la "
    "respuesta, dilo claramente. Sé conciso y factual, y cita datos concretos "
    "(fechas, nombres, valores) tal como aparecen. Responde en español."
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
    return _invoke_model(_ANSWER_SYSTEM, user_text, max_tokens=800, temperature=0.0)


def _general_answer(question):
    system = (
        "Eres un asistente útil y conciso. Responde a la pregunta con tu "
        "conocimiento general. Responde en español."
    )
    return _invoke_model(system, question, max_tokens=800, temperature=0.2)


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

    is_personal, owner, topic, optimized = _classify(question)

    if not is_personal:
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
