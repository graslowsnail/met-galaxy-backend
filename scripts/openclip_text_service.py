#!/usr/bin/env python3

import argparse
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import open_clip
import torch
from dotenv import load_dotenv

load_dotenv()

MAX_REQUEST_BYTES = 16 * 1024
MAX_TEXT_LENGTH = 500
MAX_BATCH_SIZE = 32


def resolve_device(configured):
    if configured != "auto":
        return configured
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class TextEncoder:
    def __init__(self, model_name, pretrained, device):
        self.model_name = model_name
        self.pretrained = pretrained
        self.device = resolve_device(device)
        self.model = open_clip.create_model(
            model_name,
            pretrained=pretrained,
            device=self.device,
        )
        self.model.eval()
        self.tokenizer = open_clip.get_tokenizer(model_name)
        self.lock = threading.Lock()

    def encode(self, texts):
        tokens = self.tokenizer(texts).to(self.device)
        with self.lock, torch.inference_mode():
            embeddings = self.model.encode_text(tokens)
            embeddings = embeddings / embeddings.norm(
                dim=-1,
                keepdim=True,
            ).clamp_min(1e-12)
        return embeddings.float().cpu().tolist()


def handler_class(encoder, auth_token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenCLIPText/1.0"

        def send_json(self, status, payload):
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if not auth_token:
                return True
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {auth_token}"
            return hmac.compare_digest(supplied, expected)

        def do_GET(self):
            if self.path != "/health":
                self.send_json(404, {"error": "not_found"})
                return
            self.send_json(
                200,
                {
                    "status": "healthy",
                    "model": encoder.model_name,
                    "pretrained": encoder.pretrained,
                    "device": encoder.device,
                },
            )

        def do_POST(self):
            if self.path != "/embed":
                self.send_json(404, {"error": "not_found"})
                return
            if not self.authorized():
                self.send_json(401, {"error": "unauthorized"})
                return

            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json(400, {"error": "invalid_content_length"})
                return
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                self.send_json(413, {"error": "invalid_request_size"})
                return

            try:
                payload = json.loads(self.rfile.read(content_length))
                raw_texts = payload.get("texts")
                if raw_texts is None:
                    raw_texts = [payload.get("text")]
                if (
                    not isinstance(raw_texts, list)
                    or not 1 <= len(raw_texts) <= MAX_BATCH_SIZE
                    or any(
                        not isinstance(text, str)
                        or not text.strip()
                        or len(text) > MAX_TEXT_LENGTH
                        for text in raw_texts
                    )
                ):
                    raise ValueError("invalid text input")
                texts = [text.strip() for text in raw_texts]
                embeddings = encoder.encode(texts)
            except (json.JSONDecodeError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
                return
            except Exception:
                self.send_json(500, {"error": "embedding_failed"})
                return

            response = {
                "model": encoder.model_name,
                "pretrained": encoder.pretrained,
                "dimensions": len(embeddings[0]),
                "embeddings": embeddings,
            }
            if len(embeddings) == 1:
                response["embedding"] = embeddings[0]
            self.send_json(200, response)

        def log_message(self, message, *args):
            print(
                f'{self.address_string()} - [{self.log_date_time_string()}] '
                f'{message % args}',
                flush=True,
            )

    return Handler


def main():
    parser = argparse.ArgumentParser(
        description="HTTP service for matching OpenCLIP text embeddings"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("OPENCLIP_TEXT_PORT", "8090")),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENCLIP_MODEL", "ViT-L-14"),
    )
    parser.add_argument(
        "--pretrained",
        default=os.getenv("OPENCLIP_PRETRAINED", "openai"),
    )
    parser.add_argument(
        "--device",
        default=os.getenv("OPENCLIP_DEVICE", "auto"),
    )
    args = parser.parse_args()

    torch.set_num_threads(
        max(1, int(os.getenv("OPENCLIP_TORCH_THREADS", "4")))
    )
    encoder = TextEncoder(args.model, args.pretrained, args.device)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        handler_class(
            encoder,
            os.getenv("OPENCLIP_TEXT_AUTH_TOKEN", ""),
        ),
    )
    print(
        json.dumps(
            {
                "status": "ready",
                "host": args.host,
                "port": args.port,
                "model": encoder.model_name,
                "pretrained": encoder.pretrained,
                "device": encoder.device,
            }
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
