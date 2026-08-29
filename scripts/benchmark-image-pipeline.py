#!/usr/bin/env python3

import argparse
import hashlib
import io
import json
import math
import os
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import imagehash
import numpy as np
import psycopg2
import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageOps, features
from skimage.metrics import peak_signal_noise_ratio, structural_similarity


USER_AGENT = "Met-Galaxy-Image-Benchmark/1.0 (Educational Project)"
FULL_QUALITIES = (82, 85, 88)
THUMBNAIL_SIZES = (512, 768)
THUMBNAIL_QUALITY = 85
PHASH_THRESHOLDS = (4, 6, 8, 10, 12, 16)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--analyze-only", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark-data/image-ingestion-v1"),
    )
    return parser.parse_args()


def database_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg2.connect(database_url)


def fetch_candidates(connection, sample_size, seed):
    selected = []
    selected_ids = set()

    categories = [
        (
            "photograph",
            max(1, round(sample_size * 0.17)),
            """(
                department = 'Photographs'
                OR classification ILIKE '%%photo%%'
            )""",
        ),
        (
            "painting",
            max(1, round(sample_size * 0.13)),
            """(
                department = 'European Paintings'
                OR classification ILIKE '%%painting%%'
            )""",
        ),
        (
            "line_art",
            max(1, round(sample_size * 0.17)),
            """(
                classification ILIKE '%%print%%'
                OR classification ILIKE '%%drawing%%'
            )""",
        ),
        (
            "text",
            max(1, round(sample_size * 0.10)),
            """(
                classification ILIKE '%%manuscript%%'
                OR classification ILIKE '%%book%%'
                OR classification ILIKE '%%poster%%'
                OR classification ILIKE '%%ephemera%%'
                OR medium ILIKE '%%calligraph%%'
                OR title ILIKE '%%manuscript%%'
            )""",
        ),
    ]

    def select_rows(where_sql, limit, label, params=()):
        if limit <= 0:
            return

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    id,
                    "objectId",
                    title,
                    department,
                    classification,
                    medium,
                    "primaryImage"
                FROM "met-galaxy_artwork"
                WHERE "primaryImage" IS NOT NULL
                  AND "primaryImage" <> ''
                  AND ({where_sql})
                  AND NOT (id = ANY(%s))
                ORDER BY md5(id::text || %s)
                LIMIT %s
                """,
                (*params, list(selected_ids) or [-1], str(seed), limit),
            )

            for row in cursor.fetchall():
                item = {
                    "id": row[0],
                    "object_id": row[1],
                    "title": row[2],
                    "department": row[3],
                    "classification": row[4],
                    "medium": row[5],
                    "source_url": row[6],
                    "sample_stratum": label,
                }
                selected.append(item)
                selected_ids.add(item["id"])

    for label, quota, where_sql in categories:
        select_rows(where_sql, quota, label)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT department
            FROM "met-galaxy_artwork"
            WHERE "primaryImage" IS NOT NULL
              AND "primaryImage" <> ''
              AND department IS NOT NULL
            ORDER BY department
            """
        )
        departments = [row[0] for row in cursor.fetchall()]

    remaining_for_departments = max(0, round(sample_size * 0.38))
    per_department = max(1, remaining_for_departments // len(departments))
    for department in departments:
        select_rows(
            "department = %s",
            per_department,
            f"department:{department}",
            (department,),
        )

    reserve_count = max(100, round(sample_size * 0.1))
    select_rows(
        "TRUE",
        sample_size + reserve_count - len(selected),
        "diverse_top_up",
    )
    return selected


def download_source(item, originals_dir):
    output_path = originals_dir / f"{item['id']}.source"
    if output_path.exists() and output_path.stat().st_size > 0:
        source_bytes = output_path.read_bytes()
        return {
            **item,
            "source_path": str(output_path),
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "source_byte_size": len(source_bytes),
            "download_attempts": 0,
        }

    last_error = None
    for attempt in range(1, 4):
        try:
            with requests.get(
                item["source_url"],
                headers={"User-Agent": USER_AGENT},
                timeout=(10, 45),
                stream=True,
            ) as response:
                if response.status_code == 429:
                    raise requests.HTTPError("HTTP 429", response=response)
                response.raise_for_status()

                digest = hashlib.sha256()
                source = io.BytesIO()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        digest.update(chunk)
                        source.write(chunk)

                source_bytes = source.getvalue()
                output_path.write_bytes(source_bytes)
                time.sleep(0.15)
                return {
                    **item,
                    "source_path": str(output_path),
                    "source_sha256": digest.hexdigest(),
                    "source_byte_size": len(source_bytes),
                    "download_attempts": attempt,
                    "source_content_type": response.headers.get("Content-Type"),
                }
        except Exception as error:
            last_error = str(error)
            time.sleep(min(2**attempt, 8))

    return {**item, "download_error": last_error}


def alpha_present(image):
    if image.mode not in ("RGBA", "LA") and "transparency" not in image.info:
        return False
    return image.convert("RGBA").getchannel("A").getextrema()[0] < 255


def composite_rgb(image):
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    return Image.alpha_composite(background, rgba).convert("RGB")


def normalized_pixel_digest(image):
    rgba = image.convert("RGBA")
    digest = hashlib.sha256()
    digest.update(f"{rgba.width}x{rgba.height}:RGBA:".encode())
    digest.update(rgba.tobytes())
    return digest.hexdigest()


def encode_webp(image, quality):
    output = io.BytesIO()
    started_at = time.perf_counter()
    image.save(
        output,
        format="WEBP",
        quality=quality,
        method=4,
        exact=True,
    )
    duration_ms = (time.perf_counter() - started_at) * 1000
    return output.getvalue(), duration_ms


def metric_image(image, max_dimension=1024):
    image = image.copy()
    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    return image


def quality_metrics(reference, encoded_bytes):
    encoded = Image.open(io.BytesIO(encoded_bytes))
    reference_rgb = metric_image(composite_rgb(reference))
    encoded_rgb = metric_image(composite_rgb(encoded))
    if encoded_rgb.size != reference_rgb.size:
        encoded_rgb = encoded_rgb.resize(
            reference_rgb.size,
            Image.Resampling.LANCZOS,
        )

    reference_array = np.asarray(reference_rgb, dtype=np.uint8)
    encoded_array = np.asarray(encoded_rgb, dtype=np.uint8)
    ssim = structural_similarity(
        reference_array,
        encoded_array,
        channel_axis=2,
        data_range=255,
    )
    psnr = peak_signal_noise_ratio(
        reference_array,
        encoded_array,
        data_range=255,
    )
    return {
        "ssim": float(ssim),
        "psnr": 99.0 if math.isinf(psnr) else float(psnr),
    }


def process_image(item):
    with Image.open(item["source_path"]) as source:
        source.load()
        oriented = ImageOps.exif_transpose(source)
        has_alpha = alpha_present(oriented)
        encoding_source = oriented.convert("RGBA" if has_alpha else "RGB")
        hash_source = composite_rgb(oriented)

        result = {
            **item,
            "source_format": source.format,
            "source_mode": source.mode,
            "width": oriented.width,
            "height": oriented.height,
            "has_alpha": has_alpha,
            "normalized_pixel_sha256": normalized_pixel_digest(oriented),
            "phash_64": str(imagehash.phash(hash_source, hash_size=8)),
            "dhash_64": str(imagehash.dhash(hash_source, hash_size=8)),
            "full": {},
            "thumbnails": {},
        }

        for quality in FULL_QUALITIES:
            encoded, duration_ms = encode_webp(encoding_source, quality)
            result["full"][str(quality)] = {
                "byte_size": len(encoded),
                "encode_ms": duration_ms,
                **quality_metrics(oriented, encoded),
            }

        for size in THUMBNAIL_SIZES:
            thumbnail = encoding_source.copy()
            thumbnail.thumbnail((size, size), Image.Resampling.LANCZOS)
            encoded, duration_ms = encode_webp(thumbnail, THUMBNAIL_QUALITY)
            result["thumbnails"][str(size)] = {
                "width": thumbnail.width,
                "height": thumbnail.height,
                "byte_size": len(encoded),
                "encode_ms": duration_ms,
            }

        return result


def ensure_transparency_control(downloaded, originals_dir):
    for item in downloaded:
        with Image.open(item["source_path"]) as image:
            if alpha_present(image):
                return downloaded, 0

    replaced = downloaded.pop()
    with Image.open(downloaded[0]["source_path"]) as source:
        source.load()
        control = ImageOps.exif_transpose(source).convert("RGBA")
        control.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        control.putalpha(Image.linear_gradient("L").resize(control.size))

    control_path = originals_dir / "-1.source"
    control.save(control_path, format="PNG")
    control_bytes = control_path.read_bytes()
    downloaded.append(
        {
            "id": -1,
            "object_id": None,
            "title": "Synthetic transparency control",
            "department": "Benchmark controls",
            "classification": "Transparency control",
            "medium": "RGBA PNG",
            "source_url": "synthetic://transparency-control",
            "sample_stratum": "transparency_control",
            "source_path": str(control_path),
            "source_sha256": hashlib.sha256(control_bytes).hexdigest(),
            "source_byte_size": len(control_bytes),
            "download_attempts": 0,
            "replaced_sample_id": replaced["id"],
        }
    )
    return downloaded, 1


def percentile(values, percentile_value):
    if not values:
        return None
    return float(np.percentile(np.asarray(values), percentile_value))


def aggregate_encoding(results):
    aggregate = {"full": {}, "thumbnails": {}}
    original_total = sum(item["source_byte_size"] for item in results)

    for quality in FULL_QUALITIES:
        records = [item["full"][str(quality)] for item in results]
        total = sum(record["byte_size"] for record in records)
        aggregate["full"][str(quality)] = {
            "total_bytes": total,
            "savings_percent": (1 - total / original_total) * 100,
            "median_encode_ms": statistics.median(
                record["encode_ms"] for record in records
            ),
            "p95_encode_ms": percentile(
                [record["encode_ms"] for record in records],
                95,
            ),
            "mean_ssim": statistics.fmean(
                record["ssim"] for record in records
            ),
            "p05_ssim": percentile(
                [record["ssim"] for record in records],
                5,
            ),
            "mean_psnr": statistics.fmean(
                record["psnr"] for record in records
            ),
        }

    for size in THUMBNAIL_SIZES:
        records = [item["thumbnails"][str(size)] for item in results]
        aggregate["thumbnails"][str(size)] = {
            "total_bytes": sum(record["byte_size"] for record in records),
            "median_byte_size": statistics.median(
                record["byte_size"] for record in records
            ),
            "median_encode_ms": statistics.median(
                record["encode_ms"] for record in records
            ),
            "p95_encode_ms": percentile(
                [record["encode_ms"] for record in records],
                95,
            ),
        }

    return aggregate


def hamming_distance(left, right):
    return (int(left, 16) ^ int(right, 16)).bit_count()


def padded_grayscale(path, size=256):
    with Image.open(path) as image:
        return padded_grayscale_image(ImageOps.exif_transpose(image), size)


def padded_grayscale_image(image, size=256):
    return ImageOps.pad(
        image.convert("L"),
        (size, size),
        method=Image.Resampling.LANCZOS,
        color=255,
    )


def candidate_pixel_similarity(left_path, right_path):
    left = np.asarray(padded_grayscale(left_path), dtype=np.uint8)
    right = np.asarray(padded_grayscale(right_path), dtype=np.uint8)
    return float(structural_similarity(left, right, data_range=255))


def duplicate_candidates(results):
    candidates = []
    for left_index, left in enumerate(results):
        for right in results[left_index + 1 :]:
            phash_distance = hamming_distance(
                left["phash_64"],
                right["phash_64"],
            )
            dhash_distance = hamming_distance(
                left["dhash_64"],
                right["dhash_64"],
            )
            exact_source = left["source_sha256"] == right["source_sha256"]
            exact_pixels = (
                left["normalized_pixel_sha256"]
                == right["normalized_pixel_sha256"]
            )

            if exact_source or exact_pixels or phash_distance <= 16:
                candidates.append(
                    {
                        "left_id": left["id"],
                        "right_id": right["id"],
                        "left_title": left["title"],
                        "right_title": right["title"],
                        "exact_source": exact_source,
                        "exact_pixels": exact_pixels,
                        "phash_distance": phash_distance,
                        "dhash_distance": dhash_distance,
                        "left_path": left["source_path"],
                        "right_path": right["source_path"],
                    }
                )

    candidates.sort(
        key=lambda candidate: (
            not candidate["exact_source"],
            not candidate["exact_pixels"],
            candidate["phash_distance"],
            candidate["dhash_distance"],
        )
    )
    for candidate in candidates:
        candidate["pixel_similarity"] = candidate_pixel_similarity(
            candidate["left_path"],
            candidate["right_path"],
        )
    return candidates


def transformed_hash_distances(results, count=50):
    selected = results[: min(count, len(results))]
    records = []

    for index, item in enumerate(selected):
        with Image.open(item["source_path"]) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            rgb = composite_rgb(image)
            base_phash = str(imagehash.phash(rgb, hash_size=8))
            base_dhash = str(imagehash.dhash(rgb, hash_size=8))

            recompressed_buffer = io.BytesIO()
            rgb.save(recompressed_buffer, format="JPEG", quality=70)
            recompressed = Image.open(
                io.BytesIO(recompressed_buffer.getvalue())
            ).convert("RGB")

            resized = rgb.copy()
            resized.thumbnail(
                (
                    max(1, resized.width // 2),
                    max(1, resized.height // 2),
                ),
                Image.Resampling.LANCZOS,
            )

            crop_margin_x = max(1, round(rgb.width * 0.1))
            crop_margin_y = max(1, round(rgb.height * 0.1))
            cropped = rgb.crop(
                (
                    crop_margin_x,
                    crop_margin_y,
                    max(crop_margin_x + 1, rgb.width - crop_margin_x),
                    max(crop_margin_y + 1, rgb.height - crop_margin_y),
                )
            )

            detail = rgb.crop(
                (
                    rgb.width // 4,
                    rgb.height // 4,
                    max(rgb.width // 4 + 1, rgb.width * 3 // 4),
                    max(rgb.height // 4 + 1, rgb.height * 3 // 4),
                )
            )

            different_item = selected[(index + 1) % len(selected)]
            with Image.open(different_item["source_path"]) as different:
                different = composite_rgb(ImageOps.exif_transpose(different))

            variants = {
                "exact_copy": rgb.copy(),
                "recompressed": recompressed,
                "resized": resized,
                "crop_10_percent": cropped,
                "detail_50_percent": detail,
                "different_image": different,
            }

            for label, variant in variants.items():
                variant_phash = str(imagehash.phash(variant, hash_size=8))
                variant_dhash = str(imagehash.dhash(variant, hash_size=8))
                base_pixels = np.asarray(
                    padded_grayscale_image(rgb),
                    dtype=np.uint8,
                )
                variant_pixels = np.asarray(
                    padded_grayscale_image(variant),
                    dtype=np.uint8,
                )
                records.append(
                    {
                        "source_id": item["id"],
                        "variant": label,
                        "phash_distance": hamming_distance(
                            base_phash,
                            variant_phash,
                        ),
                        "dhash_distance": hamming_distance(
                            base_dhash,
                            variant_dhash,
                        ),
                        "pixel_similarity": float(
                            structural_similarity(
                                base_pixels,
                                variant_pixels,
                                data_range=255,
                            )
                        ),
                    }
                )

    summary = {}
    for label in sorted({record["variant"] for record in records}):
        label_records = [
            record for record in records if record["variant"] == label
        ]
        summary[label] = {
            "count": len(label_records),
            "phash_p50": percentile(
                [record["phash_distance"] for record in label_records],
                50,
            ),
            "phash_p95": percentile(
                [record["phash_distance"] for record in label_records],
                95,
            ),
            "phash_max": max(
                record["phash_distance"] for record in label_records
            ),
            "dhash_p50": percentile(
                [record["dhash_distance"] for record in label_records],
                50,
            ),
            "dhash_p95": percentile(
                [record["dhash_distance"] for record in label_records],
                95,
            ),
            "dhash_max": max(
                record["dhash_distance"] for record in label_records
            ),
            "pixel_similarity_p05": percentile(
                [record["pixel_similarity"] for record in label_records],
                5,
            ),
            "pixel_similarity_p50": percentile(
                [record["pixel_similarity"] for record in label_records],
                50,
            ),
        }

    threshold_results = {}
    for threshold in PHASH_THRESHOLDS:
        threshold_results[str(threshold)] = {
            label: sum(
                record["phash_distance"] <= threshold
                for record in records
                if record["variant"] == label
            )
            for label in summary
        }

    return {
        "records": records,
        "summary": summary,
        "phash_threshold_matches": threshold_results,
    }


def draw_contained(image, size):
    contained = ImageOps.contain(
        composite_rgb(image),
        size,
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGB", size, "white")
    canvas.paste(
        contained,
        (
            (size[0] - contained.width) // 2,
            (size[1] - contained.height) // 2,
        ),
    )
    return canvas


def create_artifact_sheet(results, output_path):
    worst = sorted(
        results,
        key=lambda item: item["full"]["82"]["ssim"],
    )[:20]
    panel_size = (220, 180)
    label_height = 32
    sheet = Image.new(
        "RGB",
        (panel_size[0] * 4, (panel_size[1] + label_height) * len(worst)),
        "white",
    )
    draw = ImageDraw.Draw(sheet)

    for row, item in enumerate(worst):
        with Image.open(item["source_path"]) as source:
            source.load()
            source = ImageOps.exif_transpose(source)
            variants = [("original", source)]
            for quality in FULL_QUALITIES:
                encoded, _ = encode_webp(
                    source.convert("RGBA" if alpha_present(source) else "RGB"),
                    quality,
                )
                variants.append(
                    (f"q{quality}", Image.open(io.BytesIO(encoded)).copy())
                )

            top = row * (panel_size[1] + label_height)
            for column, (label, image) in enumerate(variants):
                panel = draw_contained(image, panel_size)
                left = column * panel_size[0]
                sheet.paste(panel, (left, top))
                draw.text(
                    (left + 6, top + panel_size[1] + 6),
                    f"{item['id']} {label}",
                    fill="black",
                )

    sheet.save(output_path, quality=90)


def create_artifact_crop_sheet(results, output_path):
    worst = sorted(
        results,
        key=lambda item: item["full"]["82"]["ssim"],
    )[:20]
    panel_size = 256
    label_height = 30
    sheet = Image.new(
        "RGB",
        (
            panel_size * 4,
            (panel_size + label_height) * len(worst),
        ),
        "white",
    )
    draw = ImageDraw.Draw(sheet)

    for row, item in enumerate(worst):
        with Image.open(item["source_path"]) as source:
            source.load()
            source = ImageOps.exif_transpose(source)
            encoding_source = source.convert(
                "RGBA" if alpha_present(source) else "RGB"
            )
            reference = metric_image(composite_rgb(source), 2048)
            variants = [("original", reference)]
            for quality in FULL_QUALITIES:
                encoded, _ = encode_webp(encoding_source, quality)
                encoded_image = metric_image(
                    composite_rgb(Image.open(io.BytesIO(encoded))),
                    2048,
                )
                if encoded_image.size != reference.size:
                    encoded_image = encoded_image.resize(
                        reference.size,
                        Image.Resampling.LANCZOS,
                    )
                variants.append((f"q{quality}", encoded_image))

            reference_array = np.asarray(reference, dtype=np.int16)
            q82_array = np.asarray(variants[1][1], dtype=np.int16)
            difference = np.abs(reference_array - q82_array).mean(axis=2)
            crop_size = min(panel_size, reference.width, reference.height)
            step = max(1, crop_size // 2)
            best_left = 0
            best_top = 0
            best_difference = -1.0

            for top in range(
                0,
                max(1, reference.height - crop_size + 1),
                step,
            ):
                for left in range(
                    0,
                    max(1, reference.width - crop_size + 1),
                    step,
                ):
                    score = difference[
                        top : top + crop_size,
                        left : left + crop_size,
                    ].mean()
                    if score > best_difference:
                        best_difference = score
                        best_left = left
                        best_top = top

            top = row * (panel_size + label_height)
            for column, (label, image) in enumerate(variants):
                crop = image.crop(
                    (
                        best_left,
                        best_top,
                        best_left + crop_size,
                        best_top + crop_size,
                    )
                )
                if crop.size != (panel_size, panel_size):
                    crop = crop.resize(
                        (panel_size, panel_size),
                        Image.Resampling.NEAREST,
                    )
                left = column * panel_size
                sheet.paste(crop, (left, top))
                draw.text(
                    (left + 6, top + panel_size + 5),
                    f"{item['id']} {label}",
                    fill="black",
                )

    sheet.save(output_path, quality=92)


def create_candidate_sheet(candidates, output_path):
    candidates = candidates[:50]
    panel_size = (220, 180)
    row_height = panel_size[1] + 38
    sheet = Image.new(
        "RGB",
        (panel_size[0] * 2, row_height * len(candidates)),
        "white",
    )
    draw = ImageDraw.Draw(sheet)

    for row, candidate in enumerate(candidates):
        top = row * row_height
        for column, key in enumerate(("left_path", "right_path")):
            with Image.open(candidate[key]) as image:
                panel = draw_contained(
                    ImageOps.exif_transpose(image),
                    panel_size,
                )
            sheet.paste(panel, (column * panel_size[0], top))

        draw.text(
            (6, top + panel_size[1] + 5),
            (
                f"{candidate['left_id']} vs {candidate['right_id']} "
                f"p={candidate['phash_distance']} "
                f"d={candidate['dhash_distance']} "
                f"pixel={candidate.get('pixel_similarity', 0):.3f}"
            ),
            fill="black",
        )

    sheet.save(output_path, quality=90)


def markdown_summary(summary):
    lines = [
        "# Image Ingestion Benchmark",
        "",
        f"- Processed images: {summary['processed_count']}",
        f"- Download failures: {summary['download_failure_count']}",
        f"- Original bytes: {summary['original_total_bytes']}",
        f"- Images with alpha: {summary['alpha_count']}",
        f"- Synthetic transparency controls: {summary['synthetic_transparency_controls']}",
        f"- Small images (largest dimension ≤512 px): {summary['small_count']}",
        f"- Very large images (largest dimension ≥3000 px): {summary['very_large_count']}",
        "",
        "## Full-size WebP",
        "",
        "| Quality | Savings | Mean SSIM | P05 SSIM | Mean PSNR | Median encode | P95 encode |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for quality in FULL_QUALITIES:
        record = summary["encoding"]["full"][str(quality)]
        lines.append(
            (
                f"| {quality} | {record['savings_percent']:.2f}% "
                f"| {record['mean_ssim']:.5f} | {record['p05_ssim']:.5f} "
                f"| {record['mean_psnr']:.2f} dB "
                f"| {record['median_encode_ms']:.1f} ms "
                f"| {record['p95_encode_ms']:.1f} ms |"
            )
        )

    lines.extend(
        [
            "",
            "## Graph thumbnails",
            "",
            "| Bounding size | Total bytes | Median bytes | Median encode | P95 encode |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for size in THUMBNAIL_SIZES:
        record = summary["encoding"]["thumbnails"][str(size)]
        lines.append(
            (
                f"| {size}px | {record['total_bytes']} "
                f"| {record['median_byte_size']:.0f} "
                f"| {record['median_encode_ms']:.1f} ms "
                f"| {record['p95_encode_ms']:.1f} ms |"
            )
        )

    lines.extend(
        [
            "",
            "## Duplicate fingerprints",
            "",
            f"- Exact source-byte candidate pairs: {summary['exact_source_pairs']}",
            f"- Exact normalized-pixel candidate pairs: {summary['exact_pixel_pairs']}",
            f"- Perceptual candidate pairs at pHash distance ≤16: {summary['perceptual_candidate_pairs']}",
            "",
            "See `duplicate-candidates.jpg`, `artifact-review.jpg`, and `summary.json` for review details.",
            "",
        ]
    )
    return "\n".join(lines)


def main():
    args = parse_args()
    load_dotenv()

    if not features.check("webp"):
        raise RuntimeError("Pillow WebP support is required")

    output_dir = args.output_dir
    originals_dir = output_dir / "originals"
    output_dir.mkdir(parents=True, exist_ok=True)
    originals_dir.mkdir(parents=True, exist_ok=True)

    if args.analyze_only:
        downloaded = json.loads(
            (output_dir / "sample.json").read_text(encoding="utf-8")
        )
        failures = json.loads(
            (output_dir / "download-failures.json").read_text(
                encoding="utf-8"
            )
        )
        synthetic_transparency_controls = sum(
            item["sample_stratum"] == "transparency_control"
            for item in downloaded
        )
    else:
        with database_connection() as connection:
            candidates = fetch_candidates(
                connection,
                args.sample_size,
                args.seed,
            )

        downloaded = []
        failures = []
        candidate_index = 0
        while (
            len(downloaded) < args.sample_size
            and candidate_index < len(candidates)
        ):
            needed = args.sample_size - len(downloaded)
            batch = candidates[candidate_index : candidate_index + needed]
            candidate_index += len(batch)

            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = [
                    executor.submit(download_source, item, originals_dir)
                    for item in batch
                ]
                for future in as_completed(futures):
                    result = future.result()
                    if result.get("download_error"):
                        failures.append(result)
                    else:
                        downloaded.append(result)
                    attempted = len(downloaded) + len(failures)
                    if attempted % 50 == 0:
                        print(
                            f"Downloaded {len(downloaded)}/{args.sample_size} "
                            f"with {len(failures)} failures",
                            flush=True,
                        )

        downloaded = downloaded[: args.sample_size]
        downloaded, synthetic_transparency_controls = (
            ensure_transparency_control(
                downloaded,
                originals_dir,
            )
        )
        (output_dir / "sample.json").write_text(
            json.dumps(downloaded, indent=2),
            encoding="utf-8",
        )
        (output_dir / "download-failures.json").write_text(
            json.dumps(failures, indent=2),
            encoding="utf-8",
        )

    results_path = output_dir / "results.jsonl"
    existing_results = {}
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                result = json.loads(line)
                existing_results[result["id"]] = result

    pending = [
        item for item in downloaded if item["id"] not in existing_results
    ]
    with results_path.open("a", encoding="utf-8") as output:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(process_image, item): item["id"]
                for item in pending
            }
            for future in as_completed(futures):
                result = future.result()
                existing_results[result["id"]] = result
                output.write(json.dumps(result) + "\n")
                output.flush()
                processed_count = len(existing_results)
                if processed_count % 50 == 0:
                    print(
                        f"Processed {processed_count}/{len(downloaded)}",
                        flush=True,
                    )

    results = [
        existing_results[item["id"]]
        for item in downloaded
        if item["id"] in existing_results
    ]
    candidates = duplicate_candidates(
        [item for item in results if item["id"] > 0]
    )
    transformations = transformed_hash_distances(results)

    (output_dir / "duplicate-candidates.json").write_text(
        json.dumps(candidates, indent=2),
        encoding="utf-8",
    )
    (output_dir / "fingerprint-transformations.json").write_text(
        json.dumps(transformations, indent=2),
        encoding="utf-8",
    )

    if results:
        create_artifact_sheet(results, output_dir / "artifact-review.jpg")
        create_artifact_crop_sheet(
            results,
            output_dir / "artifact-crops.jpg",
        )
    if candidates:
        create_candidate_sheet(
            candidates,
            output_dir / "duplicate-candidates.jpg",
        )

    summary = {
        "sample_size_requested": args.sample_size,
        "processed_count": len(results),
        "download_failure_count": len(failures),
        "synthetic_transparency_controls": synthetic_transparency_controls,
        "original_total_bytes": sum(
            item["source_byte_size"] for item in results
        ),
        "alpha_count": sum(item["has_alpha"] for item in results),
        "small_count": sum(
            max(item["width"], item["height"]) <= 512 for item in results
        ),
        "very_large_count": sum(
            max(item["width"], item["height"]) >= 3000 for item in results
        ),
        "departments": {
            department: sum(
                item["department"] == department for item in results
            )
            for department in sorted(
                {item["department"] for item in results}
            )
        },
        "strata": {
            stratum: sum(
                item["sample_stratum"] == stratum for item in results
            )
            for stratum in sorted(
                {item["sample_stratum"] for item in results}
            )
        },
        "encoding": aggregate_encoding(results),
        "exact_source_pairs": sum(
            candidate["exact_source"] for candidate in candidates
        ),
        "exact_pixel_pairs": sum(
            candidate["exact_pixels"] for candidate in candidates
        ),
        "perceptual_candidate_pairs": sum(
            candidate["phash_distance"] <= 16
            for candidate in candidates
        ),
        "candidate_threshold_counts": {
            f"phash_lte_{phash}_pixel_gte_{pixel:.2f}": sum(
                candidate["phash_distance"] <= phash
                and candidate["pixel_similarity"] >= pixel
                for candidate in candidates
            )
            for phash in PHASH_THRESHOLDS
            for pixel in (0.90, 0.95, 0.98)
        },
        "candidate_combined_hash_counts": {
            f"phash_lte_{phash}_dhash_lte_{dhash}": sum(
                candidate["phash_distance"] <= phash
                and candidate["dhash_distance"] <= dhash
                for candidate in candidates
            )
            for phash in PHASH_THRESHOLDS
            for dhash in (2, 4, 6)
        },
        "fingerprint_transformations": transformations["summary"],
        "phash_threshold_matches": transformations[
            "phash_threshold_matches"
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    (output_dir / "SUMMARY.md").write_text(
        markdown_summary(summary),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
