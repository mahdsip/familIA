locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Auto-generate a non-personal, globally-unique bucket name when not provided.
  docs_bucket_name = var.docs_bucket_name != "" ? var.docs_bucket_name : "${var.project_name}-docs-${local.account_id}"

  vector_bucket_name = "${var.project_name}-vectors"
  vector_index_name  = "${var.project_name}-index"

  embedding_model_arn = "arn:${local.partition}:bedrock:${var.aws_region}::foundation-model/${var.embedding_model_id}"
  parsing_model_arn   = "arn:${local.partition}:bedrock:${var.aws_region}::foundation-model/${var.parsing_model_id}"

  # RetrieveAndGenerate needs a model ARN or an inference-profile ARN. A plain
  # foundation-model ARN works for on-demand models like Haiku.
  generation_model_arn = "arn:${local.partition}:bedrock:${var.aws_region}::foundation-model/${var.generation_model_id}"

  name_prefix = var.project_name

  # Auto-sync requires the Knowledge Base to exist, so it only turns on in
  # phase 2 (enable_knowledge_base = true) and when auto-sync is requested.
  auto_sync_enabled = var.enable_auto_sync && var.enable_knowledge_base
}
