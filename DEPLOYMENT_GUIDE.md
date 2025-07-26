# PDF Processor Lambda Deployment Guide

## Recommended Approach: Docker Container with OIDC Authentication

### Why Docker + OIDC?
- **Reliable builds**: PyMuPDF has native dependencies that work better with containers
- **Larger size limit**: 10GB vs 250MB for zip files
- **Consistent environment**: Same runtime locally and in production
- **Secure authentication**: No long-lived AWS credentials stored in GitHub
- **Easier dependency management**: No architecture-specific compilation issues

## Setup Instructions

### 1. AWS OIDC Provider Setup (One-time setup)

First, create an OIDC identity provider in AWS:

```bash
# Create the OIDC identity provider
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
  --thumbprint-list 1c58a3a8518e8759bf075b76b750d4f2df264fcd
```

### 2. Create IAM Role for GitHub Actions

Create a trust policy file:

```bash
cat > github-actions-trust-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::YOUR_ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:YOUR_GITHUB_USERNAME/YOUR_REPO_NAME:*"
        }
      }
    }
  ]
}
EOF
```

Create the IAM role:

```bash
# Replace YOUR_ACCOUNT_ID with your AWS account ID
# Replace YOUR_GITHUB_USERNAME/YOUR_REPO_NAME with your repository
aws iam create-role \
  --role-name GitHubActions-LambdaDeploy \
  --assume-role-policy-document file://github-actions-trust-policy.json
```

### 3. Create IAM Policy for Lambda Deployment

```bash
cat > lambda-deploy-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:DescribeRepositories",
        "ecr:CreateRepository",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "lambda:UpdateFunctionCode",
        "lambda:GetFunction",
        "lambda:CreateFunction",
        "lambda:UpdateFunctionConfiguration"
      ],
      "Resource": "arn:aws:lambda:*:*:function:pdf-processor-lambda"
    }
  ]
}
EOF

# Create and attach the policy
aws iam create-policy \
  --policy-name LambdaDeployPolicy \
  --policy-document file://lambda-deploy-policy.json

aws iam attach-role-policy \
  --role-name GitHubActions-LambdaDeploy \
  --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/LambdaDeployPolicy
```

### 4. GitHub Repository Setup

Add this secret to your GitHub repository:
- `AWS_ROLE_ARN`: `arn:aws:iam::YOUR_ACCOUNT_ID:role/GitHubActions-LambdaDeploy`

**Note**: You no longer need `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` secrets!

### 5. Create Lambda Function (First time only)

```bash
# Create the Lambda execution role first
cat > lambda-execution-trust-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

aws iam create-role \
  --role-name lambda-execution-role \
  --assume-role-policy-document file://lambda-execution-trust-policy.json

# Attach basic execution policy
aws iam attach-role-policy \
  --role-name lambda-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Create policy for S3 access
cat > lambda-s3-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::your-bucket/*"
    }
  ]
}
EOF

aws iam create-policy \
  --policy-name LambdaS3ReadPolicy \
  --policy-document file://lambda-s3-policy.json

aws iam attach-role-policy \
  --role-name lambda-execution-role \
  --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/LambdaS3ReadPolicy

# Create the Lambda function
aws lambda create-function \
  --function-name pdf-processor-lambda \
  --role arn:aws:iam::YOUR_ACCOUNT_ID:role/lambda-execution-role \
  --code ImageUri=YOUR_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/pdf-processor-lambda:latest \
  --package-type Image \
  --timeout 900 \
  --memory-size 1024
```

## Quick Setup Script

Create and run this setup script:

```bash
#!/bin/bash
# setup-aws-oidc.sh

# Set these variables
AWS_ACCOUNT_ID="123456789012"  # Replace with your AWS account ID
GITHUB_REPO="username/repo-name"  # Replace with your GitHub repo
AWS_REGION="us-east-1"

echo "Setting up AWS OIDC for GitHub Actions..."

# 1. Create OIDC provider
echo "Creating OIDC provider..."
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
  --thumbprint-list 1c58a3a8518e8759bf075b76b750d4f2df264fcd

# 2. Create trust policy
cat > /tmp/github-actions-trust-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:${GITHUB_REPO}:*"
        }
      }
    }
  ]
}
EOF

# 3. Create GitHub Actions role
echo "Creating GitHub Actions IAM role..."
aws iam create-role \
  --role-name GitHubActions-LambdaDeploy \
  --assume-role-policy-document file:///tmp/github-actions-trust-policy.json

# 4. Create deployment policy
cat > /tmp/lambda-deploy-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:DescribeRepositories",
        "ecr:CreateRepository",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "lambda:UpdateFunctionCode",
        "lambda:GetFunction",
        "lambda:CreateFunction",
        "lambda:UpdateFunctionConfiguration"
      ],
      "Resource": "arn:aws:lambda:*:*:function:pdf-processor-lambda"
    }
  ]
}
EOF

# 5. Create and attach policy
echo "Creating deployment policy..."
aws iam create-policy \
  --policy-name LambdaDeployPolicy \
  --policy-document file:///tmp/lambda-deploy-policy.json

aws iam attach-role-policy \
  --role-name GitHubActions-LambdaDeploy \
  --policy-arn arn:aws:iam::${AWS_ACCOUNT_ID}:policy/LambdaDeployPolicy

echo "Setup complete!"
echo ""
echo "Add this secret to your GitHub repository:"
echo "AWS_ROLE_ARN: arn:aws:iam::${AWS_ACCOUNT_ID}:role/GitHubActions-LambdaDeploy"

# Clean up temporary files
rm /tmp/github-actions-trust-policy.json
rm /tmp/lambda-deploy-policy.json
```

## Alternative Approach: Zip Deployment

If you prefer zip deployment, here's the GitHub Actions workflow:

### Zip Deployment Workflow (with OIDC)
```yaml
name: Deploy PDF Processor Lambda (Zip)

on:
  push:
    branches: [main, master]

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v4
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
    
    - name: Build deployment package
      run: |
        chmod +x build.sh
        ./build.sh
    
    - name: Configure AWS credentials
      uses: aws-actions/configure-aws-credentials@v4
      with:
        role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
        role-session-name: GitHubActions-${{ github.run_id }}
        aws-region: us-east-1
    
    - name: Deploy to Lambda
      run: |
        aws lambda update-function-code \
          --function-name pdf-processor-lambda \
          --zip-file fileb://pdf-processor-lambda.zip
```

## Performance Considerations

### Container Image Optimizations
1. **Multi-stage builds**: Consider using multi-stage builds to reduce final image size
2. **Layer caching**: Order Dockerfile commands to maximize layer reuse
3. **Remove unnecessary files**: Use `.dockerignore` to exclude development files

### Lambda Configuration
```bash
# Recommended Lambda settings for PDF processing
aws lambda update-function-configuration \
  --function-name pdf-processor-lambda \
  --memory-size 1024 \
  --timeout 900 \
  --environment Variables='{
    "PYTHONPATH": "/var/task:/opt/python",
    "LOG_LEVEL": "INFO"
  }'
```

## Testing Locally

### With Docker
```bash
# Build the image
docker build -t pdf-processor-local .

# Test locally
docker run --rm -p 9000:8080 pdf-processor-local

# In another terminal, test the function
curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -d '{"s3_path": "s3://your-bucket/test.pdf"}'
```

### With SAM (Optional)
```yaml
# template.yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31

Resources:
  PdfProcessorFunction:
    Type: AWS::Serverless::Function
    Properties:
      PackageType: Image
      ImageUri: pdf-processor-lambda:latest
      MemorySize: 1024
      Timeout: 900
```

## Security Benefits of OIDC

1. **No long-lived credentials**: Temporary tokens expire automatically
2. **Fine-grained access**: Role can only be assumed by your specific repository
3. **Audit trail**: All actions are logged with session names
4. **Rotation-free**: No need to rotate access keys
5. **Breach protection**: If GitHub is compromised, attackers can't access your AWS resources long-term

## Monitoring and Debugging

### CloudWatch Logs
The function automatically logs to CloudWatch. Monitor:
- Memory usage patterns
- Processing time per PDF
- Error rates

### X-Ray Tracing (Optional)
Add to your Lambda environment variables:
```
_X_AMZN_TRACE_ID=Auto
```

## Cost Optimization

1. **Right-size memory**: Start with 1024MB, adjust based on actual usage
2. **Use Provisioned Concurrency** only if you have consistent traffic
3. **Monitor duration** to optimize timeout settings
4. **Consider S3 event triggers** instead of API Gateway for file processing workflows 