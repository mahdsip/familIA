"""familIA auto-sync Lambda.

Triggers a Bedrock Knowledge Base ingestion job so newly uploaded / changed
documents get (re)indexed. Invoked two ways:

  * S3 event notification when an object is created/removed in the docs bucket.
  * EventBridge weekly schedule (safety net / full refresh).

To avoid launching many overlapping jobs during a bulk sync, it first checks
whether an ingestion job is already IN_PROGRESS or STARTING and, if so, skips.
"""

import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
DATA_SOURCE_ID = os.environ["DATA_SOURCE_ID"]
REGION = os.environ.get("AWS_REGION", "eu-central-1")

_bedrock = boto3.client("bedrock-agent", region_name=REGION)

ACTIVE_STATES = {"STARTING", "IN_PROGRESS"}


def _has_active_job():
    try:
        resp = _bedrock.list_ingestion_jobs(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            maxResults=5,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        )
    except Exception:
        logger.exception("list_ingestion_jobs failed; proceeding to start a job")
        return False

    for job in resp.get("ingestionJobSummaries", []):
        if job.get("status") in ACTIVE_STATES:
            logger.info("Ingestion job %s already %s; skipping", job.get("ingestionJobId"), job.get("status"))
            return True
    return False


def handler(event, context):
    if _has_active_job():
        return {"started": False, "reason": "ingestion already running"}

    resp = _bedrock.start_ingestion_job(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        dataSourceId=DATA_SOURCE_ID,
        description="Auto-sync triggered ingestion",
    )
    job_id = resp.get("ingestionJob", {}).get("ingestionJobId")
    logger.info("Started ingestion job %s", job_id)
    return {"started": True, "ingestionJobId": job_id}
