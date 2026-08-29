FROM public.ecr.aws/lambda/python:3.13

COPY scripts/image-ingestion-requirements.txt .
RUN pip install --no-cache-dir -r image-ingestion-requirements.txt

COPY scripts/image_ingestion_worker.py ${LAMBDA_TASK_ROOT}/image_ingestion_worker.py

CMD ["image_ingestion_worker.lambda_handler"]
