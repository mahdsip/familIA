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

  # Metadata strategy (drives retrieval precision):
  #   * All metadata keys are FILTERABLE by default in S3 Vectors. Our folder-
  #     derived attributes (topic, owner, subpath, doc_type, file_name,
  #     source_path) therefore become filterable automatically, so the query
  #     Lambda can narrow retrieval by owner/topic and always return the source.
  #   * The raw chunk text Bedrock stores under AMAZON_BEDROCK_TEXT is large and
  #     must be NON-filterable, otherwise it blows the S3 Vectors filterable-
  #     metadata size budget (~2KB/vector) and ingestion fails.
  #   * non_filterable_metadata_keys is IMMUTABLE after creation — pinned here.
  metadata_configuration {
    non_filterable_metadata_keys = concat(
      ["AMAZON_BEDROCK_TEXT"],
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
