FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install \
      --index-url https://download.pytorch.org/whl/cpu \
      torch==2.7.1 \
    && pip install \
      open-clip-torch==3.3.0 \
      python-dotenv==1.2.2

COPY scripts/openclip_text_service.py .

EXPOSE 8090

CMD ["python", "/app/openclip_text_service.py", "--host", "::", "--device", "cpu"]
