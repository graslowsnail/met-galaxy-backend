# Image Ingestion Benchmark

## Sample

- Seed: `20260729`.
- Requested and processed: 1,000 images.
- Source download failures: 15; every failed slot was replaced from the reserve sample.
- Source bytes read: 151,483,244 bytes.
- Departments represented: all 19 departments with eligible primary images, plus one benchmark control.
- Explicit strata: 169 photographs, 125 paintings, 170 line-art records, 97 text-heavy records, department-balanced records, and diverse top-ups.
- Dimensions: 70 images at or below 512 px and 1 image at or above 3,000 px.
- Transparency: no transparent Met source was found, so one RGBA gradient-alpha control replaced one sampled record.

The run is reproducible with:

```bash
venv/bin/python scripts/benchmark-image-pipeline.py --sample-size 1000 --workers 4
```

The ignored `benchmark-data/image-ingestion-v1` directory contains the source sample, incremental results, metrics, candidate manifest, and visual-review sheets.

## WebP Results

| Quality | Total size | Savings | Mean SSIM | P05 SSIM | Mean PSNR | Median encode | P95 encode |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 82 | 114,310,332 B | 24.54% | 0.96996 | 0.93799 | 38.86 dB | 41.2 ms | 77.2 ms |
| 85 | 127,716,086 B | 15.69% | 0.97503 | 0.94710 | 39.79 dB | 42.6 ms | 80.5 ms |
| 88 | 145,628,584 B | 3.86% | 0.98025 | 0.95672 | 40.95 dB | 44.8 ms | 84.2 ms |

The full-image and highest-difference crop sheets showed no obvious blocking artifacts at quality 82, including line art, halftones, text, dark photography, textiles, and saturated color. Quality 85 costs about 13.4 MB more per 1,000 sample images for a 0.0051 mean SSIM improvement. Quality 88 retains nearly the entire source size.

**Selected full-size setting:** WebP quality 82, Pillow method 4, exact alpha preservation.

## Graph Thumbnail Results

Both derivatives use WebP quality 85 and preserve aspect ratio inside the bounding size.

| Bounding size | Total size | Median size | Median encode | P95 encode |
|---:|---:|---:|---:|---:|
| 512 px | 31,103,934 B | 26,590 B | 10.3 ms | 15.8 ms |
| 768 px | 62,371,656 B | 50,634 B | 19.8 ms | 34.1 ms |

The 768 px derivative doubles sample transfer and encoding time. Graph nodes are displayed well below 512 px.

**Selected graph derivative:** 512 px bounding box, WebP quality 85, Pillow method 4.

## Fingerprints

- Original source identity: SHA-256 of the downloaded byte stream.
- Normalized identity: auto-orient, convert to RGBA, then SHA-256 over dimensions, mode marker, and raw pixel bytes.
- Perceptual fingerprints: 64-bit pHash and 64-bit dHash.
- Automatic merging: source SHA-256 or normalized-pixel SHA-256 equality only.
- Approximate candidates: pHash distance at most 8 and dHash distance at most 4.
- Pixel similarity: SSIM over 256 px padded grayscale images, used for review ranking rather than automatic merging.
- Crops, details, and differently framed photographs rely on the later OpenCLIP nearest-neighbor candidate stage because strict perceptual hashes intentionally do not capture them reliably.

The controlled set contained 50 examples each of exact copies, JPEG recompressions, resizes, 10% crops, 50% detail crops, and unrelated images:

- pHash and dHash distances were at most 2 for every exact copy, recompression, and resize.
- The selected combined threshold retained all 150 exact/recompressed/resized controls.
- No unrelated control passed the selected threshold.
- No pair from the 1,000-image real sample passed the selected combined threshold.
- pHash alone produced false candidates among similarly framed pottery fragments, which is why both hashes are required.

Approximate candidates remain review-only regardless of their fingerprint distance.

## Source Retention

- Do not permanently archive newly downloaded source files after both WebP derivatives, hashes, object metadata, and canonical links are verified.
- Retain temporary source bytes until the canonical asset reaches `ready`.
- Existing `artworks/*.jpg` objects remain through Priority 3 and its 30-day rollback window, then may be archived but not deleted as part of the experiment.

## Fixed Ingestion Settings

| Setting | Value |
|---|---|
| Full-size encoding | WebP quality 82, method 4 |
| Graph derivative | 512 px, WebP quality 85, method 4 |
| Alpha | Preserve exact alpha |
| Byte fingerprint | SHA-256 |
| Normalized fingerprint | SHA-256 of auto-oriented RGBA dimensions and pixels |
| Perceptual hashes | pHash64 and dHash64 |
| Review threshold | pHash ≤8 and dHash ≤4 |
| Automatic approximate merge | Never |
| Canonical cache control | `public, max-age=31536000, immutable` |
