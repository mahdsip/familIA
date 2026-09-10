# ---------------------------------------------------------------------------
# HTTP API (API Gateway v2) with IAM authorization (SigV4)
#
# The Raspberry Pi client signs requests with IAM credentials. No API keys,
# no secrets in the repo, strong auth at zero extra cost.
# ---------------------------------------------------------------------------
resource "aws_apigatewayv2_api" "main" {
  name          = "${local.name_prefix}-api"
  protocol_type = "HTTP"
  description   = "familIA private query API (IAM/SigV4 auth)"
}

resource "aws_apigatewayv2_integration" "query" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.query.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "query" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /query"
  target    = "integrations/${aws_apigatewayv2_integration.query.id}"

  # IAM authorization: caller must present valid SigV4-signed credentials that
  # are allowed to execute-api:Invoke this route.
  authorization_type = "AWS_IAM"
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/apigateway/${local.name_prefix}"
  retention_in_days = var.lambda_log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "prod"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = var.api_throttle_burst_limit
    throttling_rate_limit  = var.api_throttle_rate_limit
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      requestTime    = "$context.requestTime"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      integrationErr = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_lambda_permission" "apigw_invoke_query" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.query.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# ---------------------------------------------------------------------------
# IAM policy + user for the Raspberry Pi client
#
# Creates a dedicated, least-privilege IAM user that may ONLY invoke the query
# route. You generate its access key out-of-band (see README) — Terraform does
# not create or store the secret, so nothing sensitive lands in state or repo.
# ---------------------------------------------------------------------------
resource "aws_iam_user" "pi_client" {
  name = "${local.name_prefix}-pi-client"
  path = "/familia/"
}

resource "aws_iam_user_policy" "pi_client_invoke" {
  name = "invoke-query-api"
  user = aws_iam_user.pi_client.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeQueryRoute"
      Effect   = "Allow"
      Action   = "execute-api:Invoke"
      Resource = "${aws_apigatewayv2_api.main.execution_arn}/${aws_apigatewayv2_stage.prod.name}/POST/query"
    }]
  })
}
