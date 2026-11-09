"""Shared helpers for the orchestrator and proxy tests.

Run from the repo root with home-mcp checked out next to it
(https://github.com/mahdsip/home-mcp). Its env already has mcp/httpx and
provides the real familia-mcp server the bridge tests start over stdio:

    uv run --project ../home-mcp --group dev \
        --with boto3==1.43.123 --with requests==2.34.2 pytest tests
"""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_orchestrator():
    os.environ.setdefault("AWS_REGION", "eu-central-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")
    os.environ["ORCHESTRATOR_MODEL_ARN"] = "arn:aws:bedrock:eu-central-1:123456789012:inference-profile/test"
    os.environ["RETRIEVER_FUNCTION_NAME"] = "familia-query"
    os.environ["OWNERS"] = "owner_a,owner_b"
    os.environ["TOPICS"] = "salud"
    return load_module("orchestrator_handler", ROOT / "lambda" / "orchestrator" / "handler.py")
