# Image ingestion queues

`queues.yaml` defines the database-idempotent ingestion queue and a FIFO
OpenCLIP dispatch queue. Both have server-side encryption, 15-minute
visibility timeouts, five-receive dead-letter policies, and dedicated
dead-letter queues. When given a worker image and database secret, the same
stack deploys an ARM64 Lambda with event-source concurrency capped at sixteen.

`repository.yaml` defines the immutable, scan-on-push ECR repository. The
worker image uses only the ingestion dependencies rather than the much larger
OpenCLIP environment.

Deploy or update the stack with:

```bash
aws cloudformation deploy \
  --stack-name met-galaxy-image-ingestion \
  --template-file infra/image-ingestion/queues.yaml \
  --parameter-overrides \
    WorkerImageUri=ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/met-galaxy-image-ingestion:TAG \
    DatabaseSecretArn=SECRET_ARN \
    WorkerMaximumConcurrency=16 \
  --capabilities CAPABILITY_IAM \
  --profile met-galaxy \
  --region us-east-1
```

Read the queue URLs with:

```bash
aws cloudformation describe-stacks \
  --stack-name met-galaxy-image-ingestion \
  --profile met-galaxy \
  --region us-east-1 \
  --query 'Stacks[0].Outputs'
```

Build the Lambda-compatible single-architecture image with:

```bash
docker build \
  --platform linux/arm64 \
  --provenance=false \
  -f infra/image-ingestion/Dockerfile \
  -t IMAGE_URI .
```
