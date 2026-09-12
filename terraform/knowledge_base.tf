# ---------------------------------------------------------------------------
# Bedrock Knowledge Base (managed RAG) backed by S3 Vectors
#
# Bedrock manages parsing -> chunking -> embeddings -> ingestion for us. We
# keep the storage in S3 Vectors (cheap) and embeddings in Titan Text v2.
# ---------------------------------------------------------------------------
resource "aws_bedrockagent_knowledge_base" "main" {
  count       = var.enable_knowledge_base ? 1 : 0
  name        = "${local.name_prefix}-kb"
  description = "Private family document knowledge base"
  role_arn    = aws_iam_role.bedrock_kb.arn

  knowledge_base_configuration {
    type = "VECTOR"

    vector_knowledge_base_configuration {
      embedding_model_arn = local.embedding_model_arn

      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = var.embedding_dimensions
          embedding_data_type = "FLOAT32"
        }
      }

      # MULTIMODAL parsing extracts images from documents; Bedrock needs an S3
      # location to store those extracted artifacts. Required whenever the data
      # source uses parsing_modality = MULTIMODAL.
      dynamic "supplemental_data_storage_configuration" {
        for_each = local.create_supplemental ? [1] : []
        content {
          storage_location {
            type = "S3"
            s3_location {
              uri = "s3://${aws_s3_bucket.supplemental[0].id}"
            }
          }
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.main.index_arn
    }
  }

  # The KB role's inline policies must exist before Bedrock validates access.
  depends_on = [
    aws_iam_role_policy.kb_s3_docs,
    aws_iam_role_policy.kb_bedrock_models,
    aws_iam_role_policy.kb_s3vectors,
    aws_iam_role_policy.kb_kms,
  ]
}

# ---------------------------------------------------------------------------
# Data source: the S3 documents bucket
#
# Indexing quality lives here:
#   - HIERARCHICAL chunking: retrieve precise child chunks but hand the model
#     the larger parent chunk for full context.
#   - Advanced parsing via a foundation model: understands tables, layout and
#     scanned PDFs far better than the basic text parser.
# ---------------------------------------------------------------------------
resource "aws_bedrockagent_data_source" "docs" {
  count             = var.enable_knowledge_base ? 1 : 0
  knowledge_base_id = aws_bedrockagent_knowledge_base.main[0].id
  name              = "${local.name_prefix}-docs"
  description       = "Family documents stored in S3"

  # Keep vectors if the data source is removed, so accidental churn doesn't
  # wipe an expensive index. Destroy is still possible via terraform destroy.
  data_deletion_policy = "RETAIN"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = aws_s3_bucket.docs.arn
      inclusion_prefixes = length(var.docs_inclusion_prefixes) > 0 ? var.docs_inclusion_prefixes : null
    }
  }

  vector_ingestion_configuration {
    dynamic "chunking_configuration" {
      for_each = var.chunking_strategy == "HIERARCHICAL" ? [1] : []
      content {
        chunking_strategy = "HIERARCHICAL"
        hierarchical_chunking_configuration {
          # Parent (context) layer — must be the first level_configuration.
          level_configuration {
            max_tokens = var.hierarchical_parent_max_tokens
          }
          # Child (retrieval) layer.
          level_configuration {
            max_tokens = var.hierarchical_child_max_tokens
          }
          overlap_tokens = var.hierarchical_overlap_tokens
        }
      }
    }

    dynamic "chunking_configuration" {
      for_each = var.chunking_strategy == "FIXED_SIZE" ? [1] : []
      content {
        chunking_strategy = "FIXED_SIZE"
        fixed_size_chunking_configuration {
          max_tokens         = var.hierarchical_child_max_tokens
          overlap_percentage = 20
        }
      }
    }

    dynamic "chunking_configuration" {
      for_each = var.chunking_strategy == "NONE" ? [1] : []
      content {
        chunking_strategy = "NONE"
      }
    }

    # Advanced document parsing with a foundation model (optional).
    dynamic "parsing_configuration" {
      for_each = var.enable_advanced_parsing ? [1] : []
      content {
        parsing_strategy = "BEDROCK_FOUNDATION_MODEL"
        bedrock_foundation_model_configuration {
          model_arn = local.parsing_model_arn
          # MULTIMODAL lets the parser read scanned images (JPG/PNG) — your
          # DNI cards, certificates and photographed reports — not just text
          # documents. Without it, images are rejected as "unsupported format".
          parsing_modality = var.parsing_modality
          parsing_prompt {
            parsing_prompt_string = <<-EOT
              Transcribe the document into clean, well-structured text.
              Preserve headings, lists and the row/column structure of any
              tables. Keep names, dates, identifiers, addresses and numeric
              values exactly as written. Do not summarise or omit content.
            EOT
          }
        }
      }
    }
  }
}
