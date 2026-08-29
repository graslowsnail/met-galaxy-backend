#!/bin/zsh

set -e
set -u
set -o pipefail

script_dir=${0:A:h}
repo_dir=${script_dir:h}
cd "$repo_dir"

export AWS_PROFILE=${AWS_PROFILE:-met-galaxy}
export AWS_REGION=${AWS_REGION:-us-east-1}

ingestion_queue_url="https://sqs.us-east-1.amazonaws.com/402114662680/met-galaxy-image-ingestion"
embedding_queue_url="https://sqs.us-east-1.amazonaws.com/402114662680/met-galaxy-image-embedding.fifo"
sagemaker_stack="met-galaxy-image-embedding-sagemaker"
sagemaker_endpoint="met-galaxy-image-embedding-gpu"
sagemaker_image="402114662680.dkr.ecr.us-east-1.amazonaws.com/met-galaxy-image-embedding:sagemaker-20260730-3"
sagemaker_template="$repo_dir/infra/image-embedding/sagemaker.yaml"
gpu_instance_count=${GPU_INSTANCE_COUNT:-4}
gpu_driver_pid=""
typeset -a ingestion_pids
typeset -a text_pids
ingestion_pids=()
text_pids=()
runtime_dir=$(mktemp -d "${TMPDIR:-/tmp}/met-galaxy-backfill.XXXXXX")
gpu_stack_marker="$runtime_dir/gpu-stack-active"

caffeinate -dimsu &
caffeinate_pid=$!

cleanup() {
  for worker_pid in "${ingestion_pids[@]}"; do
    kill "$worker_pid" >/dev/null 2>&1 || true
  done
  for worker_pid in "${text_pids[@]}"; do
    kill "$worker_pid" >/dev/null 2>&1 || true
  done
  if [[ -n "$gpu_driver_pid" ]]; then
    kill "$gpu_driver_pid" >/dev/null 2>&1 || true
  fi
  if [[ -f "$gpu_stack_marker" ]]; then
    AWS_PROFILE="$AWS_PROFILE" aws cloudformation delete-stack \
      --stack-name "$sagemaker_stack" \
      --region "$AWS_REGION" >/dev/null 2>&1 || true
  fi
  kill "$caffeinate_pid" >/dev/null 2>&1 || true
  rm -f "$gpu_stack_marker"
  rmdir "$runtime_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM HUP

for worker in {1..4}; do
  AWS_PROFILE="$AWS_PROFILE" \
    AWS_REGION="$AWS_REGION" \
    LOG_LEVEL=WARNING \
    IMAGE_INGESTION_QUEUE_URL="$ingestion_queue_url" \
    IMAGE_INGESTION_DB_POOL_SIZE=8 \
    venv/bin/python -u scripts/image_ingestion_worker.py \
      work --from-sqs --limit 400000 --concurrency 8 &
  ingestion_pids+=($!)
done

(
  for setup_attempt in {1..3}; do
    stack_status=$(
      AWS_PROFILE="$AWS_PROFILE" aws cloudformation describe-stacks \
        --stack-name "$sagemaker_stack" \
        --region "$AWS_REGION" \
        --query 'Stacks[0].StackStatus' \
        --output text 2>/dev/null || true
    )
    current_image=$(
      AWS_PROFILE="$AWS_PROFILE" aws cloudformation describe-stacks \
        --stack-name "$sagemaker_stack" \
        --region "$AWS_REGION" \
        --query 'Stacks[0].Parameters[?ParameterKey==`WorkerImageUri`].ParameterValue | [0]' \
        --output text 2>/dev/null || true
    )

    if [[ (
      "$stack_status" == "CREATE_IN_PROGRESS" ||
      "$stack_status" == "CREATE_COMPLETE"
    ) && "$current_image" == "$sagemaker_image" ]]; then
      touch "$gpu_stack_marker"
      break
    fi

    if [[ -n "$stack_status" ]]; then
      AWS_PROFILE="$AWS_PROFILE" aws cloudformation delete-stack \
        --stack-name "$sagemaker_stack" \
        --region "$AWS_REGION"
      AWS_PROFILE="$AWS_PROFILE" aws cloudformation wait \
        stack-delete-complete \
        --stack-name "$sagemaker_stack" \
        --region "$AWS_REGION"
    fi

    create_exit=0
    create_output=$(
      AWS_PROFILE="$AWS_PROFILE" aws cloudformation create-stack \
        --stack-name "$sagemaker_stack" \
        --template-body "file://$sagemaker_template" \
        --capabilities CAPABILITY_IAM \
        --region "$AWS_REGION" \
        --parameters \
          "ParameterKey=WorkerImageUri,ParameterValue=$sagemaker_image" \
          "ParameterKey=DatabaseSecretArn,ParameterValue=arn:aws:secretsmanager:us-east-1:402114662680:secret:met-galaxy/database-url-FIaJ9M" \
          "ParameterKey=EmbeddingQueueArn,ParameterValue=arn:aws:sqs:us-east-1:402114662680:met-galaxy-image-embedding.fifo" \
          "ParameterKey=EmbeddingQueueUrl,ParameterValue=$embedding_queue_url" \
          "ParameterKey=ModelDataUrl,ParameterValue=s3://met-artworks-images/model-artifacts/openclip-vit-l-14-openai/model.tar.gz" \
          "ParameterKey=GPUInstanceCount,ParameterValue=$gpu_instance_count" \
        2>&1
    ) || create_exit=$?
    if (( create_exit == 0 )); then
      touch "$gpu_stack_marker"
      break
    fi
    if [[ "$create_output" != *"AlreadyExistsException"* ]]; then
      print -u2 "$create_output"
      exit "$create_exit"
    fi
    sleep 2
  done

  if [[ ! -f "$gpu_stack_marker" ]]; then
    print -u2 "Unable to create or adopt the SageMaker stack."
    exit 1
  fi

  if ! AWS_PROFILE="$AWS_PROFILE" aws cloudformation wait \
    stack-create-complete \
    --stack-name "$sagemaker_stack" \
    --region "$AWS_REGION"; then
    print -u2 "SageMaker stack did not finish successfully."
    exit 1
  fi
  AWS_PROFILE="$AWS_PROFILE" \
    AWS_REGION="$AWS_REGION" \
    venv/bin/python -u scripts/invoke_sagemaker_embedding_workers.py \
      --endpoint "$sagemaker_endpoint" \
      --workers "$gpu_instance_count" \
      --limit 500 \
      --batch-size 10 \
      --refill-size 5000 \
      --idle-rounds 2
) &
gpu_driver_pid=$!

for worker_pid in "${ingestion_pids[@]}"; do
  wait "$worker_pid" || true
done
if ! wait "$gpu_driver_pid"; then
  print -u2 "The first GPU embedding pass failed."
  exit 1
fi

endpoint_status=$(
  AWS_PROFILE="$AWS_PROFILE" aws sagemaker describe-endpoint \
    --endpoint-name "$sagemaker_endpoint" \
    --region "$AWS_REGION" \
    --query EndpointStatus \
    --output text
)
if [[ "$endpoint_status" != "InService" ]]; then
  print -u2 "SageMaker endpoint is $endpoint_status, not InService."
  exit 1
fi

AWS_PROFILE="$AWS_PROFILE" \
  AWS_REGION="$AWS_REGION" \
  venv/bin/python -u scripts/invoke_sagemaker_embedding_workers.py \
  --endpoint "$sagemaker_endpoint" \
  --workers "$gpu_instance_count" \
    --limit 500 \
    --batch-size 10 \
    --refill-size 5000 \
    --idle-rounds 2

AWS_PROFILE="$AWS_PROFILE" aws cloudformation delete-stack \
  --stack-name "$sagemaker_stack" \
  --region "$AWS_REGION"
AWS_PROFILE="$AWS_PROFILE" aws cloudformation wait \
  stack-delete-complete \
  --stack-name "$sagemaker_stack" \
  --region "$AWS_REGION"
rm -f "$gpu_stack_marker"

for round in {1..5}; do
  text_pids=()
  for worker in {1..4}; do
    venv-embeddings/bin/python -u \
      scripts/generate-text-embeddings.py \
      work --limit 400000 --batch-size 100 &
    text_pids+=($!)
  done
  for worker_pid in "${text_pids[@]}"; do
    wait "$worker_pid" || true
  done

  text_pending=$(
    venv-embeddings/bin/python \
      scripts/generate-text-embeddings.py stats |
      jq -r '.pending'
  )
  if [[ "$text_pending" = "0" ]]; then
    break
  fi
  sleep 30
done

AWS_PROFILE="$AWS_PROFILE" \
  AWS_REGION="$AWS_REGION" \
  venv/bin/python scripts/image_ingestion_worker.py stats
AWS_PROFILE="$AWS_PROFILE" \
  AWS_REGION="$AWS_REGION" \
  IMAGE_EMBEDDING_QUEUE_URL="$embedding_queue_url" \
  venv-embeddings/bin/python scripts/image_embedding_worker.py stats
venv-embeddings/bin/python scripts/generate-text-embeddings.py stats

kill "$caffeinate_pid" >/dev/null 2>&1 || true
rm -f "$gpu_stack_marker"
rmdir "$runtime_dir" >/dev/null 2>&1 || true
trap - EXIT INT TERM HUP
print "Full backfill finished; GPU endpoint removed."
