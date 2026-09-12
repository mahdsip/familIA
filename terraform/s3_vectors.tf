# ---------------------------------------------------------------------------
# S3 Vectors store (the cheap vector database)
#
# S3 Vectors is the low-cost vector backend for the Bedrock Knowledge Base:
# no OpenSearch Serverless OCU charges, pay-per-use. The index attributes
# (dimension, distance_metric, name) are IMMUTABLE — changing them forces a
# full re-index — which is why they are pinned deliberately via variables.
# ---------------------------------------------------------------------------
resource "aws_s3vectors_vector_bucket" "main" {
  vector_bucket_name = local.vector_bucket_name

  encryption_configuration {
    sse_type    = "aws:kms"
    kms_key_arn = aws_kms_key.main.arn
  }

  # Allow `terraform destroy` to remove the bucket even if it still holds
  # vectors/indexes. Safe here because vectors are derived data (re-created by
  # re-ingesting the source documents).
  force_destroy = true

  tags = {
    Name = local.vector_bucket_name
  }
}

resource "aws_s3vectors_index" "main" {
  index_name         = local.vector_index_name
  vector_bucket_name = aws_s3vectors_vector_bucket.main.vector_bucket_name

  data_type       = "float32"
  dimension       = var.embedding_dimensions
  distance_metric = var.distance_metric

  # Metadata strategy (drives retrieval precision AND stays under the S3 Vectors
  # 2KB filterable-metadata budget):
  #   * Keep ONLY the small, high-value keys filterable: topic, owner, doc_type.
  #     These are what the query Lambda filters on.
  #   * Everything large or long — the raw chunk text, Bedrock's source-URI
  #     metadata, and our long path-like attrs (source_path, file_name, subpath,
  #     owner_name) — is NON-filterable. It's still RETURNED with results (so
  #     citations keep the source path), just not usable as a filter. This
  #     prevents deeply-nested paths from blowing the 2KB filterable limit.
  #   * non_filterable_metadata_keys is IMMUTABLE; changing it recreates the
  #     index (and requires re-ingestion) — acceptable as vectors are derived.
  metadata_configuration {
    non_filterable_metadata_keys = concat(
      [
        "AMAZON_BEDROCK_TEXT",         # the chunk text (large)
        "AMAZON_BEDROCK_METADATA",     # Bedrock internal metadata blob
        "x-amz-bedrock-kb-source-uri", # full S3 source URI (long)
        "source_path",                 # our relative path (can be long)
        "file_name",
        "subpath",
        "owner_name",
      ],
      var.extra_non_filterable_metadata_keys,
    )
  }

  encryption_configuration {
    sse_type    = "aws:kms"
    kms_key_arn = aws_kms_key.main.arn
  }

  tags = {
    Name = local.vector_index_name
  }
}
