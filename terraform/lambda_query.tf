# ---------------------------------------------------------------------------
# Query Lambda: answers questions via Bedrock RetrieveAndGenerate
# ---------------------------------------------------------------------------
data "archive_file" "query" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/query"
  output_path = "${path.module}/build/query.zip"
}

resource "aws_iam_role" "query_lambda" {
  name = "${local.name_prefix}-query-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "query_lambda" {
  name = "query-permissions"
  role = aws_iam_role.query_lambda.id
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
        Resource = "${aws_cloudwatch_log_group.query.arn}:*"
      },
      {
        Sid    = "RetrieveAndGenerate"
        Effect = "Allow"
        Action = [
          "bedrock:RetrieveAndGenerate",
          "bedrock:Retrieve"
        ]
        # Scope to this account's knowledge bases. The specific KB may not exist
        # yet in phase 1 (enable_knowledge_base=false), so we scope by account.
        Resource = "arn:${local.partition}:bedrock:${var.aws_region}:${local.account_id}:knowledge-base/*"
      },
      {
        # RetrieveAndGenerate invokes the generation model under the hood.
        Sid      = "InvokeGenerationModel"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = local.generation_model_arn
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "query" {
  name              = "/aws/lambda/${local.name_prefix}-query"
  retention_in_days = var.lambda_log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_lambda_function" "query" {
  function_name    = "${local.name_prefix}-query"
  role             = aws_iam_role.query_lambda.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.query.output_path
  source_code_hash = data.archive_file.query.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      # Empty in phase 1 (KB not created yet); set in phase 2. The Lambda only
      # answers usefully once the KB exists.
      KNOWLEDGE_BASE_ID    = var.enable_knowledge_base ? aws_bedrockagent_knowledge_base.main[0].id : ""
      GENERATION_MODEL_ARN = local.generation_model_arn
      LOG_LEVEL            = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.query]
}
