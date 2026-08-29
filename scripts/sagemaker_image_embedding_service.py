#!/usr/bin/env python3

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from image_embedding_worker import (
    Settings,
    load_model,
    run_queue,
    summarize,
)


LOGGER = logging.getLogger("sagemaker-image-embedding")
MAX_REQUEST_BYTES = 16 * 1024
MODEL_PATH = "/opt/ml/model/open_clip_model.safetensors"
ALLOWED_FIELDS = {"limit", "batchSize", "refillSize"}


def bounded_integer(payload, field, default, minimum, maximum):
    value = payload.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def parse_invocation(payload):
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    unknown_fields = set(payload) - ALLOWED_FIELDS
    if unknown_fields:
        raise ValueError(
            f"unknown fields: {', '.join(sorted(unknown_fields))}"
        )
    return {
        "limit": bounded_integer(payload, "limit", 500, 1, 500),
        "batch_size": bounded_integer(
            payload,
            "batchSize",
            10,
            1,
            10,
        ),
        "refill_size": bounded_integer(
            payload,
            "refillSize",
            0,
            0,
            5000,
        ),
    }


@dataclass
class Runtime:
    settings: Settings
    model: object
    preprocess: object
    device: str
    idle_polls: int

    def __post_init__(self):
        self.invocation_lock = threading.Lock()

    def invoke(self, request):
        if not self.invocation_lock.acquire(blocking=False):
            raise RuntimeError("another invocation is already running")
        try:
            started_at = time.monotonic()
            results = run_queue(
                self.settings,
                self.model,
                self.preprocess,
                self.device,
                request["limit"],
                request["batch_size"],
                request["refill_size"],
                self.idle_polls,
            )
            duration_ms = round((time.monotonic() - started_at) * 1000)
            return summarize(results, duration_ms)
        finally:
            self.invocation_lock.release()


class SageMakerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, runtime):
        super().__init__(server_address, SageMakerHandler)
        self.runtime = runtime


class SageMakerHandler(BaseHTTPRequestHandler):
    server: SageMakerServer

    def send_json(self, status, payload):
        content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path != "/ping":
            self.send_json(404, {"error": "not found"})
            return
        self.send_json(
            200,
            {
                "status": "ok",
                "device": self.server.runtime.device,
            },
        )

    def do_POST(self):
        if self.path != "/invocations":
            self.send_json(404, {"error": "not found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_json(400, {"error": "invalid Content-Length"})
            return
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            self.send_json(
                400,
                {
                    "error": (
                        f"request body must be 1-{MAX_REQUEST_BYTES} bytes"
                    )
                },
            )
            return
        try:
            payload = json.loads(
                self.rfile.read(content_length).decode("utf-8")
            )
            request = parse_invocation(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self.send_json(400, {"error": str(error)})
            return

        try:
            summary = self.server.runtime.invoke(request)
        except RuntimeError as error:
            if str(error) == "another invocation is already running":
                self.send_json(409, {"error": str(error)})
                return
            LOGGER.exception("invocation failed")
            self.send_json(500, {"error": str(error)})
            return
        except Exception as error:
            LOGGER.exception("invocation failed")
            self.send_json(500, {"error": str(error)})
            return
        self.send_json(200, summary)

    def log_message(self, message_format, *args):
        LOGGER.info(
            "%s - %s",
            self.address_string(),
            message_format % args,
        )


def load_runtime():
    model_name = os.getenv("IMAGE_EMBEDDING_MODEL", "ViT-L-14")
    pretrained = (
        MODEL_PATH
        if os.path.isfile(MODEL_PATH)
        else os.getenv("IMAGE_EMBEDDING_PRETRAINED", "openai")
    )
    idle_polls = int(os.getenv("IMAGE_EMBEDDING_IDLE_POLLS", "1"))
    if idle_polls < 1:
        raise RuntimeError("IMAGE_EMBEDDING_IDLE_POLLS must be positive")

    model, preprocess, device = load_model(
        model_name,
        pretrained,
        "cuda",
    )
    LOGGER.info(
        "loaded model=%s pretrained=%s device=%s",
        model_name,
        pretrained,
        device,
    )
    return Runtime(
        settings=Settings.from_env(),
        model=model,
        preprocess=preprocess,
        device=device,
        idle_polls=idle_polls,
    )


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    port = int(os.getenv("PORT", "8080"))
    server = SageMakerServer(("0.0.0.0", port), load_runtime())
    LOGGER.info("listening on port %s", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
