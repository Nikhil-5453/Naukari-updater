#!/bin/bash
# deploy.sh — Build Docker image, push to ECR, update Lambda
# Usage: bash deploy.sh [aws-profile]
# Requires: AWS CLI v2, Docker, jq

set -euo pipefail

AWS_PROFILE="${1:-default}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

echo "=== Naukri Updater — Deploy ==="
echo "Region : $AWS_REGION"
echo "Profile: $AWS_PROFILE"

# ── Get ECR repo URL from Terraform output ────────────────────────────────────
ECR_URL=$(aws ecr describe-repositories \
  --repository-names naukri-updater \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE" \
  --query 'repositories[0].repositoryUri' \
  --output text)

echo "ECR URL: $ECR_URL"

# ── Authenticate Docker to ECR ────────────────────────────────────────────────
aws ecr get-login-password \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE" \
  | docker login --username AWS --password-stdin "$ECR_URL"

# ── Build & push ──────────────────────────────────────────────────────────────
IMAGE_TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"

docker build \
  --platform linux/amd64 \
  -t "$ECR_URL:$IMAGE_TAG" \
  -t "$ECR_URL:latest" \
  .

docker push "$ECR_URL:$IMAGE_TAG"
docker push "$ECR_URL:latest"

echo "Image pushed: $ECR_URL:$IMAGE_TAG"

# ── Update Lambda to use new image ────────────────────────────────────────────
aws lambda update-function-code \
  --function-name naukri-updater \
  --image-uri "$ECR_URL:latest" \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE" \
  --output json | jq '{FunctionName, CodeSize, LastUpdateStatus}'

# Wait for update to complete
echo "Waiting for Lambda update to complete…"
aws lambda wait function-updated \
  --function-name naukri-updater \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE"

echo "=== Deploy complete ==="
echo ""
echo "Test with:"
echo "  aws lambda invoke --function-name naukri-updater --payload '{}' /tmp/out.json --region $AWS_REGION && cat /tmp/out.json"
