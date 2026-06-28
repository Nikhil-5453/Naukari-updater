# Naukri Profile Auto-Updater — Lambda

Serverless replacement for the EC2 version.  
**EventBridge Scheduler** triggers the Lambda every hour — no server to manage.

```
EventBridge Scheduler (every 1 hour)
        │
        ▼
  Lambda Function  ──► S3 (download resume.pdf + headline.txt)
  (Playwright +    ──► Naukri.com (headless Chromium)
   Chromium)       ──► S3 debug/ (error screenshots on failure)
        │
   CloudWatch Logs
```

---

## Project Layout

```
naukri_lambda/
├── src/
│   └── handler.py          # Lambda handler (Playwright automation)
├── terraform/
│   ├── main.tf             # ECR, Lambda, IAM, EventBridge, CloudWatch
│   └── terraform.tfvars.example
├── scripts/
│   └── deploy.sh           # Build → push ECR → update Lambda
├── Dockerfile              # Lambda container (Python 3.12 + Playwright Chromium)
├── requirements.txt
└── README.md
```

---

## Prerequisites

| Tool | Version |
|---|---|
| AWS CLI | v2 |
| Docker (with BuildKit) | 24+ |
| Terraform | 1.5+ |
| Python | 3.12 (local testing only) |

**S3 bucket** — upload your assets once; the Lambda always pulls the latest:
```bash
aws s3 cp resume.pdf   s3://my-naukri-assets/resume.pdf
aws s3 cp headline.txt s3://my-naukri-assets/headline.txt
```
`headline.txt` is a single line of plain text, e.g.:
```
DevOps Engineer | AWS | Kubernetes | Terraform | 5 YOE
```

---

## Deploy (first time)

### 1. Provision infrastructure with Terraform
```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars          # fill in bucket, email, password

terraform init
terraform apply
```
This creates: ECR repo, Lambda function, IAM roles, EventBridge hourly schedule, CloudWatch log group, and SSM SecureString parameters for credentials.

### 2. Build & push Docker image, update Lambda
```bash
cd ..                          # back to project root
bash scripts/deploy.sh
```
This builds the container image (with Playwright + Chromium baked in), pushes to ECR, and updates the Lambda to use the new image.

### 3. Test immediately
```bash
aws lambda invoke \
  --function-name naukri-updater \
  --payload '{}' \
  /tmp/out.json \
  --region ap-south-1 \
  --log-type Tail \
  --query 'LogResult' \
  --output text | base64 -d

cat /tmp/out.json
```

---

## Day-to-day operations

### Update resume or headline
Just re-upload to S3 — Lambda fetches fresh copies every invocation:
```bash
aws s3 cp new_resume.pdf   s3://my-naukri-assets/resume.pdf
aws s3 cp new_headline.txt s3://my-naukri-assets/headline.txt
```

### Watch live logs
```bash
aws logs tail /aws/lambda/naukri-updater --follow --region ap-south-1
```

### Change schedule
Edit `terraform.tfvars`:
```hcl
schedule_expression = "cron(30 3 * * ? *)"   # 9 AM IST daily
```
Then `terraform apply`.

### Force a manual run
```bash
aws lambda invoke \
  --function-name naukri-updater \
  --payload '{}' /tmp/out.json \
  --region ap-south-1 && cat /tmp/out.json
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Login fails / OTP screen | Naukri flagged the Lambda IP. Invoke once from your browser on the same network, or wait — Lambda IPs rotate. Error screenshots saved to `s3://your-bucket/debug/`. |
| `Task timed out after 300s` | Increase `timeout` in `terraform/main.tf` (max 900s). |
| `Cannot find Chromium` | Rebuild Docker image: `bash scripts/deploy.sh`. |
| `AccessDenied` on S3 | Verify Lambda IAM role has `s3:GetObject` on your bucket. |
| Cold start slow | First invocation after deploy can take ~30s for Chromium. Subsequent runs are faster. Lambda keeps the container warm between hourly runs. |

---

## Cost estimate (ap-south-1)

| Component | Monthly cost |
|---|---|
| Lambda (1024 MB × 5 min × 24 invocations/day) | ~$0.25 |
| ECR storage (~500 MB image) | ~$0.05 |
| EventBridge Scheduler | Free tier covers this |
| CloudWatch Logs | ~$0.01 |
| **Total** | **< $0.50/month** |

vs EC2 t3.small running 24/7 ≈ **$15–18/month**.
