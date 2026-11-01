"""Shared helpers for the orchestrator and proxy tests.

Run from the repo root (uses the familia-mcp env, which already has mcp/httpx):

    uv run --project mcp_server --group dev \
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
