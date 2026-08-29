# Canonical OpenCLIP batch worker

The worker reads canonical graph thumbnails from S3, generates normalized
768-dimensional OpenCLIP `ViT-L-14` embeddings in GPU batches, and updates the
canonical asset plus every linked artwork in one database transaction.
Database leases and SQS visibility changes make retries idempotent.

The Batch stack uses up to two `g5.xlarge` instances. The FIFO queue shards
assets across 64 message groups so two jobs can consume concurrently without
weakening per-asset idempotency.

Build and push the AMD64 CUDA image:

```bash
docker build \
  --platform linux/amd64 \
  --provenance=false \
  -f infra/image-embedding/Dockerfile \
  -t IMAGE_URI .
docker push IMAGE_URI
```

Submit two workers after pending outbox rows have been dispatched:

```bash
aws batch submit-job \
  --job-name met-galaxy-image-embedding \
  --job-queue met-galaxy-image-embedding \
  --job-definition met-galaxy-image-embedding \
  --array-properties size=2 \
  --profile met-galaxy \
  --region us-east-1
```

The same worker runs on Apple silicon for representative validation:

```bash
AWS_PROFILE=met-galaxy \
IMAGE_EMBEDDING_QUEUE_URL=QUEUE_URL \
venv-embeddings/bin/python scripts/image_embedding_worker.py \
  work --limit 1000 --batch-size 10 --device mps
```

## SageMaker real-time endpoint

The SageMaker image layers the HTTP service over the existing pushed GPU
worker image:

```bash
docker build \
  --platform linux/amd64 \
  --provenance=false \
  -f infra/image-embedding/SageMaker.Dockerfile \
  -t SAGEMAKER_IMAGE_URI .
docker push SAGEMAKER_IMAGE_URI
```

The container loads `ViT-L-14` once on CUDA. It uses
`/opt/ml/model/open_clip_model.safetensors` when the SageMaker model archive
provides it, otherwise `IMAGE_EMBEDDING_PRETRAINED` defaults to `openai`.
SageMaker probes `GET /ping`; callers submit serialized queue work to
`POST /invocations`:

```json
{"limit":500,"batchSize":10,"refillSize":5000}
```

`limit`, `batchSize`, and `refillSize` are capped at 500, 10, and 5,000.
Concurrent work requests receive `409` while the GPU is busy.

Deploy up to sixteen `ml.g5.xlarge` endpoint instances:

```bash
aws cloudformation deploy \
  --stack-name met-galaxy-image-embedding-sagemaker \
  --template-file infra/image-embedding/sagemaker.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    WorkerImageUri=SAGEMAKER_IMAGE_URI \
    DatabaseSecretArn=DATABASE_SECRET_ARN \
    EmbeddingQueueArn=EMBEDDING_QUEUE_ARN \
    EmbeddingQueueUrl=EMBEDDING_QUEUE_URL \
    ModelDataUrl=s3://BUCKET/KEY \
    GPUInstanceCount=16 \
  --profile met-galaxy \
  --region us-east-1
```

Check deployment status and retrieve the deterministic endpoint name and ARN:

```bash
aws sagemaker describe-endpoint \
  --endpoint-name met-galaxy-image-embedding-gpu \
  --profile met-galaxy \
  --region us-east-1

aws cloudformation describe-stacks \
  --stack-name met-galaxy-image-embedding-sagemaker \
  --query 'Stacks[0].Outputs' \
  --profile met-galaxy \
  --region us-east-1
```

The real-time endpoint is billed while it is provisioned. Remove it when it is
not needed:

```bash
aws cloudformation delete-stack \
  --stack-name met-galaxy-image-embedding-sagemaker \
  --profile met-galaxy \
  --region us-east-1
```
