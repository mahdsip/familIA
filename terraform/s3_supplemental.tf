# ---------------------------------------------------------------------------
# Supplemental data storage bucket (multimodal parsing artifacts)
#
# When the data source uses MULTIMODAL parsing, Bedrock extracts images from
# documents and stores them here. The KB requires a bucket ROOT URI for this
# (sub-folders are not allowed), so it gets its own dedicated bucket. Same
# lockdown as the docs bucket: KMS-encrypted, versioned, no public access.
# Only created when multimodal parsing is in use.
# ---------------------------------------------------------------------------
locals {
  create_supplemental = var.enable_knowledge_base && var.enable_advanced_parsing && var.parsing_modality == "MULTIMODAL"
  supplemental_bucket = "${var.project_name}-supplemental-${local.account_id}"
}

resource "aws_s3_bucket" "supplemental" {
  count  = local.create_supplemental ? 1 : 0
  bucket = local.supplemental_bucket

  tags = {
    Name        = local.supplemental_bucket
    DataClass   = "sensitive-personal"
    Description = "Extracted multimodal artifacts for the knowledge base"
  }
}

resource "aws_s3_bucket_public_access_block" "supplemental" {
  count                   = local.create_supplemental ? 1 : 0
  bucket                  = aws_s3_bucket.supplemental[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "supplemental" {
  count  = local.create_supplemental ? 1 : 0
  bucket = aws_s3_bucket.supplemental[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "supplemental" {
  count  = local.create_supplemental ? 1 : 0
  bucket = aws_s3_bucket.supplemental[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "supplemental" {
  count  = local.create_supplemental ? 1 : 0
  bucket = aws_s3_bucket.supplemental[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_policy" "supplemental" {
  count  = local.create_supplemental ? 1 : 0
  bucket = aws_s3_bucket.supplemental[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.supplemental[0].arn,
        "${aws_s3_bucket.supplemental[0].arn}/*"
      ]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.supplemental]
}
