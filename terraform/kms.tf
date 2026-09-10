# ---------------------------------------------------------------------------
# Customer-managed KMS key
#
# One CMK encrypts everything sensitive: the documents bucket, the S3 Vectors
# store, and CloudWatch Logs. A CMK (vs SSE-S3) gives you an auditable key
# policy and the ability to revoke access. Cost ~1 USD/month + tiny per-request.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "main" {
  description             = "${local.name_prefix} - encrypts family documents, vectors and logs"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRootAccountAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowBedrockUseOfKey"
        Effect    = "Allow"
        Principal = { Service = "bedrock.amazonaws.com" }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      },
      {
        # S3 Vectors performs asynchronous indexing under this service
        # principal (verified: KMS accepts "indexing.s3vectors.amazonaws.com",
        # not "s3vectors.amazonaws.com"). Required so the index can be created
        # and written with the customer-managed key.
        Sid       = "AllowS3VectorsIndexing"
        Effect    = "Allow"
        Principal = { Service = "indexing.s3vectors.amazonaws.com" }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
        }
      },
      {
        Sid       = "AllowCloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${var.aws_region}:${local.account_id}:log-group:*"
          }
        }
      }
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name_prefix}"
  target_key_id = aws_kms_key.main.key_id
}
