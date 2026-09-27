#!/usr/bin/env python3
"""Minimal SigV4 client for the familIA orchestrator API — for the Raspberry Pi.

Signs the POST /ask request with IAM credentials (SigV4) using botocore, so no
API keys or shared secrets are involved. The OpenVoice server on the Pi can
import `ask()` directly, or you can run this from the CLI.

The /ask endpoint is the ORCHESTRATOR: it classifies the question, decides
whether it is personal/family-related, auto-detects the person/topic, and
queries the private RAG first for personal questions (answering from general
knowledge only for non-personal questions). You do NOT need to pass owner/topic
— they are detected automatically. They remain here as optional overrides.

Dependencies (install on the Pi):
    pip install botocore requests

Credentials come from the standard AWS credential chain (env vars, ~/.aws, or
the pi-credentials.env produced by create_pi_credentials.sh).

Usage:
    export FAMILIA_API_URL="https://xxxx.execute-api.eu-central-1.amazonaws.com/prod/ask"
    export AWS_REGION="eu-central-1"
    python3 ask.py "¿Cuándo es el cumpleaños de papá?"
"""
import json
import os
import sys

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

SERVICE = "execute-api"


def ask(question: str, api_url: str | None = None, region: str | None = None,
        owner: str | None = None, topic: str | None = None) -> dict:
    api_url = api_url or os.environ["FAMILIA_API_URL"]
    region = region or os.environ.get("AWS_REGION", "eu-central-1")

    body = {"question": question}
    # Optional overrides — the orchestrator auto-detects these, so normally omit.
    if owner:
        body["owner"] = owner
    if topic:
        body["topic"] = topic
    payload = json.dumps(body)

    # Build and SigV4-sign the request.
    aws_request = AWSRequest(
        method="POST",
        url=api_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    credentials = Session().get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials found in the environment.")
    SigV4Auth(credentials, SERVICE, region).add_auth(aws_request)

    response = requests.post(
        api_url,
        data=payload,
        headers=dict(aws_request.headers),
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Ask familIA a question (SigV4-signed, via /ask orchestrator).")
    ap.add_argument("question", nargs="+", help="The question to ask")
    ap.add_argument("--owner", help="Override: force a document owner (folder key)")
    ap.add_argument("--topic", help="Override: force a topic (folder key)")
    args = ap.parse_args()

    result = ask(" ".join(args.question), owner=args.owner, topic=args.topic)

    mode = result.get("mode")
    print(result.get("answer", ""))
    if mode:
        detected = ", ".join(
            f"{k}={result[k]}" for k in ("owner", "topic") if result.get(k)
        )
        print(f"\n[mode: {mode}" + (f" | {detected}" if detected else "") + "]")

    sources = result.get("sources") or []
    if sources:
        print("\nSources:")
        for s in sources:
            if isinstance(s, dict):
                label = s.get("source_path") or s.get("uri", "")
                extra = ", ".join(
                    f"{k}={s[k]}" for k in ("topic", "owner") if s.get(k)
                )
                print(f"  - {label}" + (f"  ({extra})" if extra else ""))
            else:
                print(f"  - {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
