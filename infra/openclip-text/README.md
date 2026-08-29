# OpenCLIP text embedding service

This service loads the same OpenCLIP `ViT-L-14` / `openai` checkpoint as the
canonical image worker and exposes normalized 768-dimensional text vectors.
The API uses it for the visual-semantic branch of reciprocal-rank fusion.
The backend validates the service's model name, pretrained checkpoint, and
dimension before using a query vector, so a mismatched encoder cannot silently
rank the stored image embeddings.

Run it locally with the existing embedding environment:

```bash
npm run serve-openclip-text
```

Configure the backend with:

```bash
OPENCLIP_TEXT_EMBEDDING_URL=http://localhost:8090
OPENCLIP_TEXT_AUTH_TOKEN=replace-with-a-shared-secret
```

The token is optional for loopback-only development and required whenever the
service is network-accessible. Build a CPU image with:

```bash
docker build \
  -f infra/openclip-text/Dockerfile \
  -t met-galaxy-openclip-text .
```

The service provides `GET /health` and authenticated `POST /embed`. Model
weights are cached by OpenCLIP under the container user's cache directory; use
a persistent cache volume in production to avoid downloading them on every
restart.

Run the complete search stack with the backend configured to use this matching
encoder:

```bash
docker compose --env-file .env -f compose.search.yaml up --build
```

Set `OPENCLIP_TEXT_AUTH_TOKEN` in `.env` before starting the stack. The text
encoder is only reachable by the backend on the internal Compose network.
