#!/bin/bash
# setup-aws-oidc.sh
# Script to set up AWS OIDC for GitHub Actions deployment

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    print_error "AWS CLI is not installed. Please install it first."
    exit 1
fi

# Get configuration from user
echo "=== AWS OIDC Setup for GitHub Actions ==="
echo ""

read -p "Enter your AWS Account ID: " AWS_ACCOUNT_ID
read -p "Enter your GitHub repository (username/repo-name): " GITHUB_REPO
read -p "Enter AWS region [us-east-1]: " AWS_REGION
AWS_REGION=${AWS_REGION:-us-east-1}

# Validate inputs
if [[ ! $AWS_ACCOUNT_ID =~ ^[0-9]{12}$ ]]; then
    print_error "Invalid AWS Account ID. Must be 12 digits."
    exit 1
fi

if [[ ! $GITHUB_REPO =~ ^[^/]+/[^/]+$ ]]; then
    print_error "Invalid GitHub repository format. Should be username/repo-name"
    exit 1
fi

print_info "Configuration:"
print_info "  AWS Account ID: $AWS_ACCOUNT_ID"
print_info "  GitHub Repo: $GITHUB_REPO"
print_info "  AWS Region: $AWS_REGION"
echo ""

read -p "Continue? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    print_info "Setup cancelled."
    exit 0
fi

print_info "Setting up AWS OIDC for GitHub Actions..."

# 1. Create OIDC provider
print_info "Creating OIDC identity provider..."
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com" >/dev/null 2>&1; then
    print_warning "OIDC provider already exists. Skipping creation."
else
    aws iam create-open-id-connect-provider \
      --url https://token.actions.githubusercontent.com \
      --client-id-list sts.amazonaws.com \
      --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
      --thumbprint-list 1c58a3a8518e8759bf075b76b750d4f2df264fcd
    print_info "OIDC provider created successfully."
fi

# 2. Create trust policy
print_info "Creating trust policy..."
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
print_info "Creating GitHub Actions IAM role..."
if aws iam get-role --role-name GitHubActions-LambdaDeploy >/dev/null 2>&1; then
    print_warning "Role GitHubActions-LambdaDeploy already exists."
    read -p "Update the trust policy? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        aws iam update-assume-role-policy \
          --role-name GitHubActions-LambdaDeploy \
          --policy-document file:///tmp/github-actions-trust-policy.json
        print_info "Trust policy updated."
    fi
else
    aws iam create-role \
      --role-name GitHubActions-LambdaDeploy \
      --assume-role-policy-document file:///tmp/github-actions-trust-policy.json
    print_info "IAM role created successfully."
fi

# 4. Create deployment policy
print_info "Creating deployment policy..."
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
print_info "Creating and attaching deployment policy..."
POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:policy/LambdaDeployPolicy"

if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
    print_warning "Policy LambdaDeployPolicy already exists."
else
    aws iam create-policy \
      --policy-name LambdaDeployPolicy \
      --policy-document file:///tmp/lambda-deploy-policy.json
    print_info "Deployment policy created."
fi

# Attach policy to role
aws iam attach-role-policy \
  --role-name GitHubActions-LambdaDeploy \
  --policy-arn "$POLICY_ARN" 2>/dev/null || true

print_info "Policy attached to role."

# Clean up temporary files
rm -f /tmp/github-actions-trust-policy.json
rm -f /tmp/lambda-deploy-policy.json

print_info "Setup complete!"
echo ""
print_info "Next steps:"
print_info "1. Add this secret to your GitHub repository:"
echo "   AWS_ROLE_ARN: arn:aws:iam::${AWS_ACCOUNT_ID}:role/GitHubActions-LambdaDeploy"
echo ""
print_info "2. Create your Lambda function (if it doesn't exist yet):"
echo "   See DEPLOYMENT_GUIDE.md for detailed instructions"
echo ""
print_info "3. Push to your main/master branch to trigger deployment"
print_warning "Make sure to update the AWS_REGION in .github/workflows/deploy.yml if needed" 