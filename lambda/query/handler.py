"""familIA query Lambda.

Answers a question using the Bedrock Knowledge Base via RetrieveAndGenerate
(managed RAG: retrieve relevant chunks + generate a grounded answer).

Precision & provenance features:
  * Folder-derived metadata (topic, owner, subpath, doc_type, ...) is indexed
    as filterable attributes. Callers may pass `owner`/`topic` or a raw
    `filter` to narrow retrieval, which sharply improves precision when a
    question is about a specific person or subject.
  * Every answer returns the SOURCE of each retrieved chunk: the S3 URI plus
    its topic/owner/source_path metadata — so you always know which file the
    information came from.

No personal data lives in this code. The family context comes entirely from
the indexed documents (RAG), never from a hard-coded prompt.
"""

import base64
import json
import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
MODEL_ARN = os.environ["GENERATION_MODEL_ARN"]
REGION = os.environ.get("AWS_REGION", "eu-central-1")
NUM_RESULTS = int(os.environ.get("NUM_RESULTS", "8"))
MAX_QUESTION_CHARS = int(os.environ.get("MAX_QUESTION_CHARS", "1000"))

DEFAULT_PROMPT = (
    "You are a private family assistant. Answer the question using ONLY the "
    "information in the search results below. If the answer is not present, "
    "say you could not find it in the documents. Be concise and factual, and "
    "cite concrete details (dates, names, values) exactly as written. "
    "Mention which document the information comes from.\n\n"
    "Search results:\n$search_results$\n\nQuestion: $query$\n\nAnswer:"
)
# IMPORTANT: A custom promptTemplate SUPPRESSES RetrieveAndGenerate citations,
# and returning the source file is a hard requirement. So we DO NOT send a
# custom template by default (empty => Bedrock's default, which keeps citations
# and still produces grounded answers). Set PROMPT_TEMPLATE explicitly only if
# you accept losing source citations. DEFAULT_PROMPT is kept for reference.
PROMPT_TEMPLATE = os.environ.get("PROMPT_TEMPLATE", "").strip()

_bedrock = boto3.client(
    "bedrock-agent-runtime",
    region_name=REGION,
    config=Config(retries={"max_attempts": 3, "mode": "adaptive"}),
)


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _build_filter(payload):
    """Build a Bedrock retrieval filter from convenience fields or a raw filter.

    Priority:
      1. An explicit `filter` object (passed straight to Bedrock) wins.
      2. Otherwise build an equals/andAll filter from `owner` and/or `topic`.
    Returns None when no filtering is requested.
    """
    if isinstance(payload.get("filter"), dict):
        return payload["filter"]

    clauses = []
    for key in ("owner", "topic"):
        value = payload.get(key)
        if value:
            clauses.append({"equals": {"key": key, "value": str(value)}})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"andAll": clauses}


def handler(event, context):
    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
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

    session_id = payload.get("sessionId")

    vector_search = {"numberOfResults": NUM_RESULTS}
    retrieval_filter = _build_filter(payload)
    if retrieval_filter:
        vector_search["filter"] = retrieval_filter

    kb_config = {
        "knowledgeBaseId": KNOWLEDGE_BASE_ID,
        "modelArn": MODEL_ARN,
        "retrievalConfiguration": {"vectorSearchConfiguration": vector_search},
    }
    # Only send a custom prompt template if explicitly configured — otherwise
    # Bedrock's default is used, which preserves source citations.
    if PROMPT_TEMPLATE:
        kb_config["generationConfiguration"] = {
            "promptTemplate": {"textPromptTemplate": PROMPT_TEMPLATE}
        }

    request = {
        "input": {"text": question},
        "retrieveAndGenerateConfiguration": {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": kb_config,
        },
    }
    if session_id:
        request["sessionId"] = session_id

    try:
        result = _bedrock.retrieve_and_generate(**request)
    except _bedrock.exceptions.ValidationException as exc:
        # Most commonly a malformed filter.
        logger.warning("Validation error: %s", exc)
        return _response(400, {"error": "Invalid request (check filter/owner/topic)."})
    except Exception:
        logger.exception("retrieve_and_generate failed")
        return _response(502, {"error": "Upstream model error."})

    answer = result.get("output", {}).get("text", "")

    # Build rich source citations: URI + folder metadata, deduped by URI.
    sources = []
    seen = set()
    for citation in result.get("citations", []):
        for ref in citation.get("retrievedReferences", []):
            uri = ref.get("location", {}).get("s3Location", {}).get("uri")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            md = ref.get("metadata", {}) or {}
            sources.append(
                {
                    "uri": uri,
                    "topic": md.get("topic"),
                    "owner": md.get("owner"),
                    "source_path": md.get("source_path"),
                }
            )

    return _response(
        200,
        {
            "answer": answer,
            "sessionId": result.get("sessionId"),
            "sources": sources,
        },
    )
