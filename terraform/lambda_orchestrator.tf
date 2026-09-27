# ---------------------------------------------------------------------------
# Orchestrator Lambda (module 1): routing + language layer in front of the RAG
#
# Independent module. It classifies intent, extracts owner/topic, optimizes the
# query, and for personal questions ALWAYS queries the RAG retriever first
# (invoking the retriever Lambda directly). It never touches the vector store.
#
# Gated on local.orchestrator_enabled (enable_orchestrator && enable_knowledge_base):
# it needs the retriever Lambda + KB to exist.
# ---------------------------------------------------------------------------
data "archive_file" "orchestrator" {
  count       = local.orchestrator_enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/../lambda/orchestrator"
  output_path = "${path.module}/build/orchestrator.zip"
}

resource "aws_iam_role" "orchestrator_lambda" {
  count = local.orchestrator_enabled ? 1 : 0
  name  = "${local.name_prefix}-orchestrator-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "orchestrator_lambda" {
  count = local.orchestrator_enabled ? 1 : 0
  name  = "orchestrator-permissions"
  role  = aws_iam_role.orchestrator_lambda[0].id
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
        Resource = "${aws_cloudwatch_log_group.orchestrator[0].arn}:*"
      },
      {
        # Invoke the orchestrator model (Bedrock Messages API). With inference
        # profiles, grant both the profile ARN and the underlying cross-region
        # foundation-model ARN it routes to.
        Sid    = "InvokeOrchestratorModel"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:GetInferenceProfile"
        ]
        Resource = distinct(concat(
          [local.orchestrator_model_arn],
          var.use_inference_profiles ? [local.orchestrator_fm_wildcard_arn] : [],
        ))
      },
      {
        # Call the RAG retriever module (separate Lambda) — the ONLY way the
        # orchestrator reaches the knowledge base. No direct Bedrock KB access.
        Sid      = "InvokeRetriever"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.query.arn
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "orchestrator" {
  count             = local.orchestrator_enabled ? 1 : 0
  name              = "/aws/lambda/${local.name_prefix}-orchestrator"
  retention_in_days = var.lambda_log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_lambda_function" "orchestrator" {
  count            = local.orchestrator_enabled ? 1 : 0
  function_name    = "${local.name_prefix}-orchestrator"
  role             = aws_iam_role.orchestrator_lambda[0].arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.orchestrator[0].output_path
  source_code_hash = data.archive_file.orchestrator[0].output_base64sha256
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      ORCHESTRATOR_MODEL_ARN  = local.orchestrator_model_arn
      RETRIEVER_FUNCTION_NAME = aws_lambda_function.query.function_name
      NUM_RESULTS             = tostring(var.orchestrator_num_results)
      MIN_SCORE               = tostring(var.orchestrator_min_score)
      # Owners/topics come from gitignored tfvars — never the repo.
      OWNERS    = join(",", var.orchestrator_known_owners)
      TOPICS    = join(",", var.orchestrator_known_topics)
      LOG_LEVEL = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.orchestrator]
}

# ---------------------------------------------------------------------------
# POST /ask route -> orchestrator (IAM/SigV4 auth), on the existing HTTP API.
# ---------------------------------------------------------------------------
resource "aws_apigatewayv2_integration" "orchestrator" {
  count                  = local.orchestrator_enabled ? 1 : 0
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.orchestrator[0].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "orchestrator" {
  count              = local.orchestrator_enabled ? 1 : 0
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "POST /ask"
  target             = "integrations/${aws_apigatewayv2_integration.orchestrator[0].id}"
  authorization_type = "AWS_IAM"
}

resource "aws_lambda_permission" "apigw_invoke_orchestrator" {
  count         = local.orchestrator_enabled ? 1 : 0
  statement_id  = "AllowAPIGatewayInvokeOrchestrator"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.orchestrator[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}
