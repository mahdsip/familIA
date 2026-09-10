#!/usr/bin/env python3
"""Minimal SigV4 client for the familIA query API — reference for the Raspberry Pi.

Signs the POST /query request with IAM credentials (SigV4) using botocore, so
no API keys or shared secrets are involved. The OpenVoice server on the Pi can
import `ask()` directly, or you can run this from the CLI.

Dependencies (install on the Pi):
    pip install botocore requests

Credentials come from the standard AWS credential chain (env vars, ~/.aws, or
the pi-credentials.env produced by create_pi_credentials.sh).

Usage:
    export FAMILIA_API_URL="https://xxxx.execute-api.eu-central-1.amazonaws.com/prod/query"
    export AWS_REGION="eu-central-1"
    python3 ask.py "When is Dad's birthday?"
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
        session_id: str | None = None, owner: str | None = None,
        topic: str | None = None) -> dict:
    api_url = api_url or os.environ["FAMILIA_API_URL"]
    region = region or os.environ.get("AWS_REGION", "eu-central-1")

    body = {"question": question}
    if session_id:
        body["sessionId"] = session_id
    # Optional metadata filters — sharpen retrieval by person/subject.
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
        timeout=40,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Ask familIA a question (SigV4-signed).")
    ap.add_argument("question", nargs="+", help="The question to ask")
    ap.add_argument("--owner", help="Filter retrieval to a document owner (folder)")
    ap.add_argument("--topic", help="Filter retrieval to a topic (folder)")
    ap.add_argument("--session-id", help="Continue a previous conversation")
    args = ap.parse_args()

    result = ask(
        " ".join(args.question),
        session_id=args.session_id,
        owner=args.owner,
        topic=args.topic,
    )
    print(result.get("answer", ""))
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
