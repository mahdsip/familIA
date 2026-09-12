# ---------------------------------------------------------------------------
# Input variables
#
# IMPORTANT (public repo): no real values live here. Defaults are generic and
# contain NO personal data. Real values go in terraform.tfvars (gitignored) —
# see terraform.tfvars.example for the template.
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region to deploy into. EU region recommended for GDPR data residency."
  type        = string
  default     = "eu-central-1"
}

variable "project_name" {
  description = "Short name used as a prefix for all resources. Keep it generic (no personal info)."
  type        = string
  default     = "familia"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project_name))
    error_message = "project_name must be lowercase alphanumeric/hyphen, 2-21 chars, starting with a letter."
  }
}

variable "environment" {
  description = "Deployment environment (e.g. prod, dev)."
  type        = string
  default     = "prod"
}

# ----- Document storage --------------------------------------------------

variable "docs_bucket_name" {
  description = <<-EOT
    Globally-unique name for the S3 bucket that stores the source family
    documents. Must be unique across all of AWS. Leave empty to auto-generate
    a name based on project_name + account id (recommended for privacy: it does
    not leak a personal-looking name into the public repo).
  EOT
  type        = string
  default     = ""
}

variable "docs_inclusion_prefixes" {
  description = "Optional list of S3 key prefixes to restrict which objects are ingested (e.g. [\"documents/\"]). Empty = whole bucket."
  type        = list(string)
  default     = []
}

variable "version_retention_days" {
  description = "How many days to keep noncurrent object versions before expiring them."
  type        = number
  default     = 90
}

# ----- Embeddings / vector index (INDEXING QUALITY — see README) ---------

variable "embedding_model_id" {
  description = "Bedrock embedding model id. Titan Text v2 supports 256/512/1024 dims."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "embedding_dimensions" {
  description = <<-EOT
    Vector dimensions. IMMUTABLE once the index is created (changing it forces
    a full re-index). 1024 = best quality for Titan v2; 512/256 = cheaper
    storage/query at some recall cost. We default to 1024 to "index once, well".
  EOT
  type        = number
  default     = 1024

  validation {
    condition     = contains([256, 512, 1024], var.embedding_dimensions)
    error_message = "Titan Text v2 supports 256, 512 or 1024 dimensions."
  }
}

variable "distance_metric" {
  description = "Similarity metric for the vector index. IMMUTABLE. 'cosine' is standard for text embeddings."
  type        = string
  default     = "cosine"

  validation {
    condition     = contains(["cosine", "euclidean"], var.distance_metric)
    error_message = "distance_metric must be 'cosine' or 'euclidean'."
  }
}

variable "extra_non_filterable_metadata_keys" {
  description = <<-EOT
    Additional metadata keys to store but NOT expose as query filters. The
    folder-derived keys (topic, owner, subpath, doc_type, file_name,
    source_path) stay FILTERABLE by default and should not be listed here.
    IMMUTABLE once the index is created.
  EOT
  type        = list(string)
  default     = []
}

# ----- Chunking (INDEXING QUALITY) ---------------------------------------

variable "chunking_strategy" {
  description = "How documents are split. HIERARCHICAL (parent/child) gives the best context/precision balance for mixed documents."
  type        = string
  default     = "HIERARCHICAL"

  validation {
    condition     = contains(["HIERARCHICAL", "SEMANTIC", "FIXED_SIZE", "NONE"], var.chunking_strategy)
    error_message = "chunking_strategy must be HIERARCHICAL, SEMANTIC, FIXED_SIZE or NONE."
  }
}

variable "hierarchical_parent_max_tokens" {
  description = "Max tokens for parent (context) chunks when using HIERARCHICAL chunking."
  type        = number
  default     = 1500
}

variable "hierarchical_child_max_tokens" {
  description = "Max tokens for child (retrieval) chunks when using HIERARCHICAL chunking."
  type        = number
  default     = 300
}

variable "hierarchical_overlap_tokens" {
  description = "Tokens repeated across chunks in the same layer (HIERARCHICAL)."
  type        = number
  default     = 60
}

# ----- Advanced parsing (INDEXING QUALITY) -------------------------------

variable "enable_advanced_parsing" {
  description = <<-EOT
    Use a Bedrock foundation model to parse documents (understands tables,
    layout, scanned PDFs) instead of the basic text parser. Costs more at
    ingestion time but materially improves retrieval quality — aligned with
    "index once, well". Disable to minimise ingestion cost.
  EOT
  type        = bool
  default     = true
}

variable "parsing_model_id" {
  description = "Bedrock model id used for advanced document parsing (only when enable_advanced_parsing = true). Use a current (non-legacy) multimodal model."
  type        = string
  default     = "anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "use_inference_profiles" {
  description = <<-EOT
    Newer Bedrock models (e.g. Claude Haiku 4.5) are not available on-demand and
    must be invoked through a regional inference profile. When true, the parsing
    and generation model ids are wrapped as inference-profile ARNs using
    inference_profile_region_prefix. Set false only if you switch both models
    back to ones that support on-demand throughput.
  EOT
  type        = bool
  default     = true
}

variable "inference_profile_region_prefix" {
  description = "Region prefix for Bedrock inference profiles (e.g. 'eu', 'us', 'apac'). 'eu' keeps inference within the EU for data residency."
  type        = string
  default     = "eu"
}

# ----- Generation model (query time) -------------------------------------

variable "generation_model_id" {
  description = <<-EOT
    Bedrock model id used to generate answers from retrieved chunks. Query-time
    cost is tiny for a home workload, so this favours quality but stays cheap.
    Configurable so you can swap models without touching architecture (e.g.
    amazon.nova-lite-v1:0 for an even cheaper option).
  EOT
  type        = string
  default     = "anthropic.claude-haiku-4-5-20251001-v1:0"
}

# ----- Query API / access -------------------------------------------------

variable "api_throttle_burst_limit" {
  description = "API Gateway burst limit (requests). Keeps costs and abuse bounded for a single-client home setup."
  type        = number
  default     = 5
}

variable "api_throttle_rate_limit" {
  description = "API Gateway steady-state rate limit (requests/second)."
  type        = number
  default     = 2
}

variable "lambda_log_retention_days" {
  description = "CloudWatch Logs retention for Lambda functions."
  type        = number
  default     = 30
}

# ----- Auto-sync ----------------------------------------------------------

variable "enable_auto_sync" {
  description = "If true, an S3 upload event triggers a Knowledge Base ingestion job automatically (debounced). You still keep the weekly cron as a safety net."
  type        = bool
  default     = true
}

variable "enable_knowledge_base" {
  description = <<-EOT
    Two-phase deploy switch.
      false (phase 1): create only the base infra (empty docs bucket, S3
             Vectors store, IAM, API, query Lambda). Sync your documents to the
             bucket from your data machine.
      true  (phase 2): additionally create the Knowledge Base, its S3 data
             source, and the auto-sync Lambda, then ingest the documents that
             now exist in the bucket.
    Deferring KB creation avoids an empty first ingestion and lets you load
    data before indexing.
  EOT
  type        = bool
  default     = false
}
