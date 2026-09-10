# ---------------------------------------------------------------------------
# Auto-sync Lambda: (re)indexes the Knowledge Base on document changes
#
# Triggers:
#   * S3 object created/removed in the docs bucket (near-real-time).
#   * Weekly EventBridge schedule (safety-net full refresh).
# Both are gated so overlapping ingestion jobs are skipped.
# ---------------------------------------------------------------------------
data "archive_file" "auto_sync" {
  count       = local.auto_sync_enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/../lambda/auto_sync"
  output_path = "${path.module}/build/auto_sync.zip"
}

resource "aws_iam_role" "auto_sync" {
  count = local.auto_sync_enabled ? 1 : 0
  name  = "${local.name_prefix}-auto-sync-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "auto_sync" {
  count = local.auto_sync_enabled ? 1 : 0
  name  = "auto-sync-permissions"
  role  = aws_iam_role.auto_sync[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Logs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.auto_sync[0].arn}:*"
      },
      {
        Sid    = "Ingestion"
        Effect = "Allow"
        Action = [
          "bedrock:StartIngestionJob",
          "bedrock:ListIngestionJobs",
          "bedrock:GetIngestionJob"
        ]
        Resource = aws_bedrockagent_knowledge_base.main[0].arn
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "auto_sync" {
  count             = local.auto_sync_enabled ? 1 : 0
  name              = "/aws/lambda/${local.name_prefix}-auto-sync"
  retention_in_days = var.lambda_log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_lambda_function" "auto_sync" {
  count            = local.auto_sync_enabled ? 1 : 0
  function_name    = "${local.name_prefix}-auto-sync"
  role             = aws_iam_role.auto_sync[0].arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.auto_sync[0].output_path
  source_code_hash = data.archive_file.auto_sync[0].output_base64sha256
  timeout          = 60
  memory_size      = 128

  environment {
    variables = {
      KNOWLEDGE_BASE_ID = aws_bedrockagent_knowledge_base.main[0].id
      DATA_SOURCE_ID    = aws_bedrockagent_data_source.docs[0].data_source_id
      LOG_LEVEL         = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.auto_sync]
}

# ----- Trigger 1: S3 object change notifications -------------------------
resource "aws_lambda_permission" "s3_invoke" {
  count         = local.auto_sync_enabled ? 1 : 0
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auto_sync[0].function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.docs.arn
}

resource "aws_s3_bucket_notification" "docs" {
  count  = local.auto_sync_enabled ? 1 : 0
  bucket = aws_s3_bucket.docs.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.auto_sync[0].arn
    events              = ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]
  }

  depends_on = [aws_lambda_permission.s3_invoke]
}

# ----- Trigger 2: weekly safety-net schedule -----------------------------
resource "aws_scheduler_schedule" "weekly_sync" {
  count = local.auto_sync_enabled ? 1 : 0
  name  = "${local.name_prefix}-weekly-sync"

  flexible_time_window {
    mode = "OFF"
  }

  # Sundays at 03:00 UTC.
  schedule_expression          = "cron(0 3 ? * SUN *)"
  schedule_expression_timezone = "Europe/Madrid"

  target {
    arn      = aws_lambda_function.auto_sync[0].arn
    role_arn = aws_iam_role.scheduler[0].arn
  }
}

resource "aws_iam_role" "scheduler" {
  count = local.auto_sync_enabled ? 1 : 0
  name  = "${local.name_prefix}-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  count = local.auto_sync_enabled ? 1 : 0
  name  = "invoke-auto-sync"
  role  = aws_iam_role.scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.auto_sync[0].arn
    }]
  })
}
