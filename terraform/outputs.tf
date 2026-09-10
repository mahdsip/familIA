output "query_api_url" {
  description = "Full HTTPS endpoint for questions (POST). Requires SigV4/IAM auth."
  value       = "${aws_apigatewayv2_stage.prod.invoke_url}/query"
}

output "api_id" {
  description = "API Gateway HTTP API id."
  value       = aws_apigatewayv2_api.main.id
}

output "docs_bucket" {
  description = "S3 bucket where you sync your family documents."
  value       = aws_s3_bucket.docs.bucket
}

output "knowledge_base_id" {
  description = "Bedrock Knowledge Base id (null in phase 1 until enable_knowledge_base = true)."
  value       = var.enable_knowledge_base ? aws_bedrockagent_knowledge_base.main[0].id : null
}

output "data_source_id" {
  description = "Bedrock Knowledge Base data source id (null in phase 1)."
  value       = var.enable_knowledge_base ? aws_bedrockagent_data_source.docs[0].data_source_id : null
}

output "vector_index_arn" {
  description = "S3 Vectors index ARN (immutable attributes)."
  value       = aws_s3vectors_index.main.index_arn
}

output "kms_key_arn" {
  description = "Customer-managed KMS key ARN."
  value       = aws_kms_key.main.arn
}

output "pi_client_user" {
  description = "IAM user for the Raspberry Pi client. Create its access key out-of-band (see README)."
  value       = aws_iam_user.pi_client.name
}

output "aws_region" {
  description = "Region the stack is deployed in (needed for SigV4 signing)."
  value       = var.aws_region
}
