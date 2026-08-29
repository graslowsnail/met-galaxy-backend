FROM pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

WORKDIR /app

COPY scripts/image-embedding-requirements.txt .
RUN pip install -r image-embedding-requirements.txt

COPY scripts/image_embedding_worker.py .

ENTRYPOINT ["python", "/app/image_embedding_worker.py"]
