ARG BASE_IMAGE=402114662680.dkr.ecr.us-east-1.amazonaws.com/met-galaxy-image-embedding:p4-20260730-3
FROM ${BASE_IMAGE}

WORKDIR /app

COPY scripts/image_embedding_worker.py .
COPY scripts/sagemaker_image_embedding_service.py .

EXPOSE 8080

ENTRYPOINT ["python", "/app/sagemaker_image_embedding_service.py"]
