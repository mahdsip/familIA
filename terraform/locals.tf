locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Auto-generate a non-personal, globally-unique bucket name when not provided.
  docs_bucket_name = var.docs_bucket_name != "" ? var.docs_bucket_name : "${var.project_name}-docs-${local.account_id}"

  vector_bucket_name = "${var.project_name}-vectors"
  vector_index_name  = "${var.project_name}-index"

  # Embeddings run on-demand (Titan v2), so a plain foundation-model ARN works.
  embedding_model_arn = "arn:${local.partition}:bedrock:${var.aws_region}::foundation-model/${var.embedding_model_id}"

  # Newer models (e.g. Claude Haiku 4.5) are NOT available on-demand and must be
  # invoked via a regional inference profile (e.g. "eu.anthropic.claude-...").
  # When use_inference_profiles = true we build inference-profile ARNs for the
  # parsing/generation models and also grant the underlying cross-region
  # foundation-model ARNs, which inference profiles require for invocation.
  inference_prefix = "${var.inference_profile_region_prefix}." # e.g. "eu."

  parsing_model_arn = var.use_inference_profiles ? (
    "arn:${local.partition}:bedrock:${var.aws_region}:${local.account_id}:inference-profile/${local.inference_prefix}${var.parsing_model_id}"
  ) : "arn:${local.partition}:bedrock:${var.aws_region}::foundation-model/${var.parsing_model_id}"

  generation_model_arn = var.use_inference_profiles ? (
    "arn:${local.partition}:bedrock:${var.aws_region}:${local.account_id}:inference-profile/${local.inference_prefix}${var.generation_model_id}"
  ) : "arn:${local.partition}:bedrock:${var.aws_region}::foundation-model/${var.generation_model_id}"

  # Underlying foundation-model ARNs an inference profile can route to (all
  # regions covered by the profile). Needed in IAM alongside the profile ARN.
  # Wildcard region because e.g. an "eu." profile may route to several EU regions.
  parsing_fm_wildcard_arn    = "arn:${local.partition}:bedrock:*::foundation-model/${var.parsing_model_id}"
  generation_fm_wildcard_arn = "arn:${local.partition}:bedrock:*::foundation-model/${var.generation_model_id}"

  name_prefix = var.project_name

  # Auto-sync requires the Knowledge Base to exist, so it only turns on in
  # phase 2 (enable_knowledge_base = true) and when auto-sync is requested.
  auto_sync_enabled = var.enable_auto_sync && var.enable_knowledge_base
}
