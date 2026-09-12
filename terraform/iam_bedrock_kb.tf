# ---------------------------------------------------------------------------
# IAM role assumed by the Bedrock Knowledge Base
#
# Least-privilege: read the docs bucket, invoke the embedding + parsing models,
# use the KMS key, and read/write the specific S3 Vectors index.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "bedrock_kb" {
  name = "${local.name_prefix}-bedrock-kb-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike = {
          "aws:SourceArn" = "arn:${local.partition}:bedrock:${var.aws_region}:${local.account_id}:knowledge-base/*"
        }
      }
    }]
  })
}

# Read source documents.
resource "aws_iam_role_policy" "kb_s3_docs" {
  name = "s3-docs-read"
  role = aws_iam_role.bedrock_kb.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.docs.arn]
      },
      {
        Sid      = "GetObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.docs.arn}/*"]
        Condition = {
          StringEquals = { "aws:ResourceAccount" = local.account_id }
        }
      }
    ]
  })
}

# Invoke embedding + advanced-parsing models.
resource "aws_iam_role_policy" "kb_bedrock_models" {
  name = "bedrock-invoke-models"
  role = aws_iam_role.bedrock_kb.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "InvokeEmbeddingAndParsingModels"
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        # Required so the KB can resolve the inference profile it invokes.
        "bedrock:GetInferenceProfile"
      ]
      # Embedding (on-demand FM), the parsing model (inference profile ARN when
      # use_inference_profiles), and the underlying cross-region foundation
      # model the profile routes to — inference profiles need both granted.
      Resource = distinct(concat(
        [
          local.embedding_model_arn,
          local.parsing_model_arn,
        ],
        var.use_inference_profiles ? [local.parsing_fm_wildcard_arn] : [],
      ))
    }]
  })
}

# Use the S3 Vectors index (read + write).
resource "aws_iam_role_policy" "kb_s3vectors" {
  name = "s3vectors-access"
  role = aws_iam_role.bedrock_kb.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "S3VectorsReadWrite"
      Effect = "Allow"
      Action = [
        "s3vectors:GetVectorBucket",
        "s3vectors:GetIndex",
        "s3vectors:ListIndexes",
        "s3vectors:PutVectors",
        "s3vectors:GetVectors",
        "s3vectors:QueryVectors",
        "s3vectors:DeleteVectors",
        "s3vectors:ListVectors"
      ]
      Resource = [
        aws_s3vectors_vector_bucket.main.vector_bucket_arn,
        aws_s3vectors_index.main.index_arn
      ]
    }]
  })
}

# Use the KMS key for docs + vectors.
resource "aws_iam_role_policy" "kb_kms" {
  name = "kms-access"
  role = aws_iam_role.bedrock_kb.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "UseKmsKey"
      Effect = "Allow"
      Action = [
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:DescribeKey"
      ]
      Resource = [aws_kms_key.main.arn]
    }]
  })
}
