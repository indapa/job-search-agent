#!/usr/bin/env bash
# Deploy the job-search CloudFormation stacks with the AWS CLI.
#
#   infra/deploy.sh bootstrap                 # ECR repo + GitHub OIDC deploy role (once)
#   infra/deploy.sh secrets                   # copy GOOGLE_API_KEY and TAVILY_API_KEY from .env to SSM
#   infra/deploy.sh app [ImageUri] [Email]    # bucket, Lambda, nightly schedule, emails, alarm
#   infra/deploy.sh resume                    # sync resume/ to the bucket (after "app")
#
# For "app", ImageUri defaults to the image the function currently runs (so a
# redeploy never rolls back an image published by GitHub Actions), or, on the
# first deploy, to the newest image in ECR.
# Uses the default AWS profile unless AWS_PROFILE is set; region from AWS_REGION (default us-west-1).
set -euo pipefail

REGION="${AWS_REGION:-us-west-1}"
FUNCTION_NAME="job-search-nightly"
PARAM_PREFIX="/job-search"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
aws_() { aws --region "$REGION" "$@"; }

case "${1:-}" in
  bootstrap)
    create_oidc=true
    if aws iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[].Arn' --output text \
        | grep -q 'token.actions.githubusercontent.com'; then
      # Only reuse a provider this stack doesn't own.
      owner=$(aws_ cloudformation describe-stack-resources --stack-name job-search-bootstrap \
                --logical-resource-id GitHubOidcProvider --query 'StackResources[0].PhysicalResourceId' \
                --output text 2>/dev/null || true)
      [[ -z "$owner" || "$owner" == "None" ]] && create_oidc=false
    fi
    aws_ cloudformation deploy --stack-name job-search-bootstrap \
      --template-file "$HERE/bootstrap.yaml" --capabilities CAPABILITY_NAMED_IAM \
      --parameter-overrides "CreateOidcProvider=$create_oidc"
    aws_ cloudformation describe-stacks --stack-name job-search-bootstrap \
      --query 'Stacks[0].Outputs' --output table
    ;;
  secrets)
    env_file="$ROOT/.env"
    [[ -f "$env_file" ]] || { echo "error: $env_file not found (copy .env.example and fill it in)" >&2; exit 1; }
    for key in GOOGLE_API_KEY TAVILY_API_KEY; do
      value=$(grep -E "^${key}=" "$env_file" | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')
      [[ -n "$value" ]] || { echo "error: $key is empty in $env_file" >&2; exit 1; }
      aws_ ssm put-parameter --name "$PARAM_PREFIX/$key" --type SecureString --overwrite \
        --value "$value" > /dev/null
      echo "Stored $PARAM_PREFIX/$key"
    done
    ;;
  app)
    image="${2:-}"
    if [[ -z "$image" ]]; then
      image=$(aws_ lambda get-function --function-name "$FUNCTION_NAME" \
                --query 'Code.ImageUri' --output text 2>/dev/null || true)
    fi
    if [[ -z "$image" || "$image" == "None" ]]; then
      repo=$(aws_ ecr describe-repositories --repository-names job-search \
               --query 'repositories[0].repositoryUri' --output text)
      tag=$(aws_ ecr describe-images --repository-name job-search \
              --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageTags[?@!=`latest`] | [0]' --output text)
      [[ -z "$tag" || "$tag" == "None" ]] && { echo "error: no image in ECR yet; run the deploy workflow first" >&2; exit 1; }
      image="$repo:$tag"
    fi
    echo "ImageUri: $image"
    params=("ImageUri=$image")
    [[ -n "${3:-}" ]] && params+=("AlertEmail=$3")
    aws_ cloudformation deploy --stack-name job-search-app \
      --template-file "$HERE/app.yaml" --capabilities CAPABILITY_IAM \
      --parameter-overrides "${params[@]}"
    aws_ cloudformation describe-stacks --stack-name job-search-app \
      --query 'Stacks[0].Outputs' --output table
    ;;
  resume)
    bucket=$(aws_ cloudformation describe-stacks --stack-name job-search-app \
               --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' --output text)
    aws_ s3 sync "$ROOT/resume/" "s3://$bucket/resume/" --delete --exclude '.*'
    aws_ s3 ls "s3://$bucket/resume/"
    ;;
  *)
    sed -n '2,12p' "$0"; exit 2 ;;
esac
