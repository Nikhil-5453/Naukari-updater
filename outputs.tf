# terraform/outputs.tf
# All outputs for the Naukri Profile Auto-Updater stack.
# Run `terraform output` after apply to see these values.

# ── ECR ───────────────────────────────────────────────────────────────────────

output "ecr_repository_name" {
  description = "Name of the ECR repository"
  value       = aws_ecr_repository.this.name
}

output "ecr_repository_url" {
  description = "Full ECR repository URL — used in deploy.sh and docker push"
  value       = aws_ecr_repository.this.repository_url
}

output "ecr_repository_arn" {
  description = "ARN of the ECR repository"
  value       = aws_ecr_repository.this.arn
}

output "ecr_registry_id" {
  description = "AWS account ID that owns the ECR registry"
  value       = aws_ecr_repository.this.registry_id
}

# ── Lambda ────────────────────────────────────────────────────────────────────

output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.this.function_name
}

output "lambda_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.this.arn
}

output "lambda_invoke_arn" {
  description = "Invoke ARN of the Lambda function (used by API Gateway or SDK)"
  value       = aws_lambda_function.this.invoke_arn
}

output "lambda_role_arn" {
  description = "ARN of the IAM execution role attached to the Lambda"
  value       = aws_iam_role.lambda.arn
}

output "lambda_role_name" {
  description = "Name of the IAM execution role attached to the Lambda"
  value       = aws_iam_role.lambda.name
}

output "lambda_memory_size" {
  description = "Memory allocated to the Lambda function (MB)"
  value       = aws_lambda_function.this.memory_size
}

output "lambda_timeout" {
  description = "Lambda function timeout (seconds)"
  value       = aws_lambda_function.this.timeout
}

output "lambda_image_uri" {
  description = "Container image URI currently deployed to Lambda"
  value       = aws_lambda_function.this.image_uri
}

# ── EventBridge Scheduler ─────────────────────────────────────────────────────

output "scheduler_name" {
  description = "Name of the EventBridge Scheduler schedule"
  value       = aws_scheduler_schedule.hourly.name
}

output "scheduler_arn" {
  description = "ARN of the EventBridge Scheduler schedule"
  value       = aws_scheduler_schedule.hourly.arn
}

output "scheduler_expression" {
  description = "Schedule expression controlling invocation frequency"
  value       = aws_scheduler_schedule.hourly.schedule_expression
}

output "scheduler_role_arn" {
  description = "ARN of the IAM role used by EventBridge Scheduler to invoke Lambda"
  value       = aws_iam_role.scheduler.arn
}

# ── CloudWatch Logs ───────────────────────────────────────────────────────────

output "cloudwatch_log_group_name" {
  description = "CloudWatch Log Group name for Lambda logs"
  value       = aws_cloudwatch_log_group.this.name
}

output "cloudwatch_log_group_arn" {
  description = "CloudWatch Log Group ARN"
  value       = aws_cloudwatch_log_group.this.arn
}

output "cloudwatch_log_retention_days" {
  description = "Log retention period in days"
  value       = aws_cloudwatch_log_group.this.retention_in_days
}

# ── SSM Parameters ────────────────────────────────────────────────────────────

output "ssm_email_parameter_name" {
  description = "SSM Parameter Store key holding the Naukri email (SecureString)"
  value       = aws_ssm_parameter.email.name
}

output "ssm_email_parameter_arn" {
  description = "ARN of the SSM parameter for Naukri email"
  value       = aws_ssm_parameter.email.arn
}

output "ssm_password_parameter_name" {
  description = "SSM Parameter Store key holding the Naukri password (SecureString)"
  value       = aws_ssm_parameter.password.name
}

output "ssm_password_parameter_arn" {
  description = "ARN of the SSM parameter for Naukri password"
  value       = aws_ssm_parameter.password.arn
}

# ── S3 ────────────────────────────────────────────────────────────────────────

output "s3_bucket_name" {
  description = "S3 bucket used for resume and headline assets"
  value       = var.s3_bucket
}

output "s3_resume_s3_uri" {
  description = "Full S3 URI of the resume PDF"
  value       = "s3://${var.s3_bucket}/${var.s3_resume_key}"
}

output "s3_headline_s3_uri" {
  description = "Full S3 URI of the headline text file"
  value       = "s3://${var.s3_bucket}/${var.s3_headline_key}"
}

output "s3_debug_screenshots_prefix" {
  description = "S3 prefix where error screenshots are uploaded on failure"
  value       = "s3://${var.s3_bucket}/debug/"
}

# ── Handy CLI commands ────────────────────────────────────────────────────────

output "cmd_invoke_lambda" {
  description = "AWS CLI command to manually trigger a profile update"
  value       = "aws lambda invoke --function-name ${aws_lambda_function.this.function_name} --payload '{}' /tmp/out.json --region ${var.aws_region} && cat /tmp/out.json"
}

output "cmd_tail_logs" {
  description = "AWS CLI command to stream Lambda logs live"
  value       = "aws logs tail ${aws_cloudwatch_log_group.this.name} --follow --region ${var.aws_region}"
}

output "cmd_docker_push" {
  description = "Docker login + push command for deploying a new image"
  value       = "aws ecr get-login-password --region ${var.aws_region} | docker login --username AWS --password-stdin ${aws_ecr_repository.this.repository_url} && docker build --platform linux/amd64 -t ${aws_ecr_repository.this.repository_url}:latest . && docker push ${aws_ecr_repository.this.repository_url}:latest"
}
