"""familIA retriever Lambda (RAG module — retrieval only, no generation).

This is the RAG layer, intentionally kept as a PURE RETRIEVAL engine: it calls
Bedrock `Retrieve` (NOT `RetrieveAndGenerate`) and returns the matching chunks
with their metadata and relevance score. It never invokes a generation model
and never writes prose. All natural-language generation lives in the separate
orchestrator module, so the two layers stay completely independent.

Contract
--------
Input (JSON body via API Gateway, or direct Lambda invoke):
    {
      "query": "text to search for",   # or "question" (alias)
      "owner": "owner_key",            # optional convenience filter
      "topic": "topic_key",            # optional convenience filter
      "filter": { ... },               # optional raw Bedrock filter (wins)
      "numberOfResults": 8             # optional override
    }

Output:
    {
      "chunks": [
        {
          "text": "...",               # chunk content
          "score": 0.83,               # relevance score (higher = closer)
          "uri": "s3://.../file.pdf",
          "topic": "topic_key",
          "owner": "owner_key",
          "source_path": "...",
          "captured_date": "...",
          "modified_date": "..."
        },
        ...
      ]
    }

No personal data lives in this code. Family context comes entirely from the
indexed documents (RAG), never from a hard-coded prompt.
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
REGION = os.environ.get("AWS_REGION", "eu-central-1")
NUM_RESULTS = int(os.environ.get("NUM_RESULTS", "8"))
MAX_RESULTS = int(os.environ.get("MAX_RESULTS", "25"))
MAX_QUERY_CHARS = int(os.environ.get("MAX_QUERY_CHARS", "1000"))

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


def _parse_payload(event):
    """Accept both an API Gateway proxy event and a direct Lambda invoke.

    A direct invoke (from the orchestrator) passes the payload as the event
    itself. An API Gateway event wraps it in a (possibly base64) `body`.
    """
    if "body" in event:
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        return json.loads(raw_body)
    return event


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


def _extract_chunks(result):
    """Flatten Bedrock Retrieve results into a compact chunk list."""
    chunks = []
    for item in result.get("retrievalResults", []):
        md = item.get("metadata", {}) or {}
        chunks.append(
            {
                "text": item.get("content", {}).get("text", ""),
                "score": item.get("score"),
                "uri": item.get("location", {}).get("s3Location", {}).get("uri"),
                "topic": md.get("topic"),
                "owner": md.get("owner"),
                "source_path": md.get("source_path"),
                # Dates help disambiguate duplicates (e.g. newest DNI).
                "captured_date": md.get("captured_date"),
                "modified_date": md.get("modified_date"),
            }
        )
    return chunks


def handler(event, context):
    if not KNOWLEDGE_BASE_ID:
        return _response(503, {"error": "Knowledge Base not enabled yet."})

    try:
        payload = _parse_payload(event)
    except (ValueError, TypeError):
        return _response(400, {"error": "Body must be valid JSON."})

    query = (payload.get("query") or payload.get("question") or "").strip()
    if not query:
        return _response(400, {"error": "Missing 'query' field."})
    if len(query) > MAX_QUERY_CHARS:
        return _response(400, {"error": "Query too long."})

    try:
        num_results = int(payload.get("numberOfResults", NUM_RESULTS))
    except (ValueError, TypeError):
        num_results = NUM_RESULTS
    num_results = max(1, min(num_results, MAX_RESULTS))

    vector_search = {"numberOfResults": num_results}
    retrieval_filter = _build_filter(payload)
    if retrieval_filter:
        vector_search["filter"] = retrieval_filter

    request = {
        "knowledgeBaseId": KNOWLEDGE_BASE_ID,
        "retrievalQuery": {"text": query},
        "retrievalConfiguration": {"vectorSearchConfiguration": vector_search},
    }

    try:
        result = _bedrock.retrieve(**request)
    except _bedrock.exceptions.ValidationException as exc:
        logger.warning("Validation error: %s", exc)
        return _response(400, {"error": "Invalid request (check filter/owner/topic)."})
    except Exception:
        logger.exception("retrieve failed")
        return _response(502, {"error": "Upstream retrieval error."})

    chunks = _extract_chunks(result)
    return _response(200, {"chunks": chunks})
