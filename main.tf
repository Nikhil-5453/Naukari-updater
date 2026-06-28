# terraform/main.tf
# Provisions:
#   - ECR repository (holds the Docker image)
#   - IAM role + policies for Lambda
#   - Lambda function (container image, 1024 MB, 5 min timeout)
#   - EventBridge Scheduler rule (every 1 hour)
#   - SSM Parameters for secrets (email / password)

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ── Variables ─────────────────────────────────────────────────────────────────

variable "aws_region"      { default = "ap-south-1" }
variable "s3_bucket"       { description = "S3 bucket holding resume.pdf and headline.txt" }
variable "s3_resume_key"   { default = "resume.pdf" }
variable "s3_headline_key" { default = "headline.txt" }
variable "naukri_email"    { 
  description = "Naukri login email"
  sensitive = true 
  }
variable "naukri_password" { 
  description = "Naukri login password" 
  sensitive = true 
}
variable "schedule_expression" {
  default     = "rate(1 hour)"
  description = "EventBridge schedule (rate or cron expression)"
}

locals {
  name = "naukri-updater"

  # Bootstrap fix: Lambda container image must exist in ECR before the function
  # can be created — but ECR is empty on first `terraform apply`.
  #
  # Solution: create Lambda with this public AWS placeholder image first.
  # deploy.sh then pushes the real image and calls `aws lambda update-function-code`.
  # lifecycle.ignore_changes = [image_uri] prevents Terraform from ever
  # reverting back to this placeholder on subsequent applies.
  # bootstrap_image = "public.ecr.aws/lambda/python:3.12"
}

# ── ECR Repository ────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "this" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration { scan_on_push = true }

  lifecycle { prevent_destroy = false }
}

# ── SSM Parameter Store (secrets) ────────────────────────────────────────────

resource "aws_ssm_parameter" "email" {
  name  = "/${local.name}/NAUKRI_EMAIL"
  type  = "SecureString"
  value = var.naukri_email
}

resource "aws_ssm_parameter" "password" {
  name  = "/${local.name}/NAUKRI_PASSWORD"
  type  = "SecureString"
  value = var.naukri_password
}

# ── IAM Role for Lambda ───────────────────────────────────────────────────────

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "permissions" {
  # S3 — read resume + headline, write debug screenshots
  statement {
    actions   = ["s3:GetObject", "s3:ListBucket", "s3:PutObject"]
    resources = [
      "arn:aws:s3:::${var.s3_bucket}",
      "arn:aws:s3:::${var.s3_bucket}/*",
    ]
  }
  # SSM — read credentials at cold start
  statement {
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      aws_ssm_parameter.email.arn,
      aws_ssm_parameter.password.arn,
    ]
  }
  # ECR — pull image (needed for Lambda container)
  statement {
    actions = [
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:GetAuthorizationToken",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "permissions" {
  name   = "${local.name}-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.permissions.json
}

# ── Lambda Function ───────────────────────────────────────────────────────────

resource "aws_lambda_function" "this" {
  function_name = local.name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"

  # Uses public placeholder on first apply so Terraform doesn't fail with
  # "source image does not exist". deploy.sh replaces this with the real image.
  image_uri = "012874738371.dkr.ecr.ap-south-1.amazonaws.com/naukri-updater:latest"

  timeout     = 300   # 5 minutes — enough for browser automation
  memory_size = 1024  # Chromium needs headroom

  environment {
    variables = {
      S3_BUCKET          = var.s3_bucket
      S3_RESUME_KEY      = var.s3_resume_key
      S3_HEADLINE_KEY    = var.s3_headline_key
      AWS_REGION_NAME    = var.aws_region
      SSM_EMAIL_PARAM    = aws_ssm_parameter.email.name
      SSM_PASSWORD_PARAM = aws_ssm_parameter.password.name
    }
  }

  # Terraform must NOT revert image_uri back to the placeholder after
  # deploy.sh has pushed the real image. All image updates go through
  # `aws lambda update-function-code` in deploy.sh — not through Terraform.
  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_iam_role_policy_attachment.basic]
}

# ── EventBridge Scheduler (hourly trigger) ────────────────────────────────────

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.this.arn
    }]
  })
}

resource "aws_scheduler_schedule" "hourly" {
  name       = "${local.name}-hourly"
  group_name = "default"

  flexible_time_window { mode = "OFF" }

  schedule_expression = var.schedule_expression   # "rate(1 hour)"

  target {
    arn      = aws_lambda_function.this.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "eventbridge-scheduler" })
  }
}

# ── CloudWatch Log Group ──────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 14
}

# Outputs are defined in outputs.tf
