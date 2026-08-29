#!/usr/bin/env python3

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MODES = {
    "metadata": {"w_metadata": 1, "w_visual": 0, "w_keyword": 0},
    "visual": {"w_metadata": 0, "w_visual": 1, "w_keyword": 0},
    "keyword": {"w_metadata": 0, "w_visual": 0, "w_keyword": 1},
    "fused": {"w_metadata": 1, "w_visual": 1, "w_keyword": 1.25},
}
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class ReviewBuildError(RuntimeError):
    pass


def load_judgments(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReviewBuildError(f"judgment file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ReviewBuildError(
            f"invalid judgment JSON at line {error.lineno}: {error.msg}"
        ) from error
    if payload.get("version") != 1 or not isinstance(payload.get("judgments"), list):
        raise ReviewBuildError("judgment file must use version 1 with a judgments array")
    return payload


def read_response(response):
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ReviewBuildError("search response exceeded 10 MiB")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise ReviewBuildError("search returned invalid JSON") from error
    if payload.get("success") is not True or not isinstance(payload.get("data"), list):
        raise ReviewBuildError("search did not return a successful result list")
    return payload


def search(endpoint, query, count, mode, timeout_seconds):
    parameters = {"q": query, "count": count, **MODES[mode]}
    request = Request(
        f"{endpoint}?{urlencode(parameters)}",
        headers={
            "Accept": "application/json",
            "User-Agent": "met-galaxy-review-builder/1",
        },
    )
    started_at = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = read_response(response)
    except HTTPError as error:
        raise ReviewBuildError(f"{mode} search returned HTTP {error.code}") from error
    except URLError as error:
        raise ReviewBuildError(f"{mode} search failed: {error.reason}") from error
    except TimeoutError as error:
        raise ReviewBuildError(f"{mode} search timed out") from error
    return payload, round((time.perf_counter() - started_at) * 1000, 3)


def candidate_from_result(result):
    asset_id = result.get("canonicalAssetId")
    if type(asset_id) is not int or asset_id <= 0:
        raise ReviewBuildError("search result is missing a canonical asset ID")
    return {
        "canonicalAssetId": asset_id,
        "objectId": result.get("objectId"),
        "title": result.get("title"),
        "artist": result.get("artist"),
        "date": result.get("date"),
        "department": result.get("department"),
        "culture": result.get("culture"),
        "medium": result.get("medium"),
        "imageUrl": result.get("imageUrl"),
        "objectUrl": result.get("objectUrl"),
        "ranks": {},
    }


def build_query_pool(endpoint, judgment, count, candidate_limit, timeout_seconds):
    candidates = {}
    mode_reports = {}
    for mode in MODES:
        try:
            payload, latency_ms = search(
                endpoint,
                judgment["query"],
                count,
                mode,
                timeout_seconds,
            )
            metadata = payload.get("meta", {})
            mode_reports[mode] = {
                "latencyMs": latency_ms,
                "resultCount": len(payload["data"]),
                "availableModes": metadata.get("availableModes", []),
                "degradedModes": metadata.get("degradedModes", []),
                "modeErrors": metadata.get("modeErrors", {}),
            }
            for rank, result in enumerate(payload["data"], start=1):
                asset_id = result.get("canonicalAssetId")
                candidate = candidates.get(asset_id)
                if candidate is None:
                    candidate = candidate_from_result(result)
                    candidates[asset_id] = candidate
                candidate["ranks"][mode] = rank
        except ReviewBuildError as error:
            mode_reports[mode] = {"error": str(error)}

    if not candidates:
        raise ReviewBuildError(
            f"no candidates were returned for query {judgment['query']!r}"
        )

    def pool_score(candidate):
        return sum(1 / (60 + rank) for rank in candidate["ranks"].values())

    ordered = sorted(
        candidates.values(),
        key=lambda candidate: (
            -pool_score(candidate),
            min(candidate["ranks"].values()),
            candidate["canonicalAssetId"],
        ),
    )[:candidate_limit]
    for candidate in ordered:
        candidate["poolScore"] = round(pool_score(candidate), 8)
    return {
        "query": judgment["query"],
        "category": judgment["category"],
        "existingRelevantCanonicalAssetIds": judgment.get(
            "relevantCanonicalAssetIds", []
        ),
        "existingNotes": judgment.get("notes", ""),
        "existingReviewed": judgment.get("reviewed", False),
        "modes": mode_reports,
        "candidates": ordered,
    }


def write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def html_document(review_payload, judgments_payload):
    review_json = json.dumps(review_payload, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    judgment_json = json.dumps(judgments_payload, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Met Galaxy Search Relevance Review</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, system-ui, sans-serif; }}
    body {{ margin: 0; background: #111318; color: #f2f4f8; }}
    header {{ position: sticky; top: 0; z-index: 2; padding: 16px 24px; background: #181b22ee; border-bottom: 1px solid #353946; }}
    header h1 {{ margin: 0 0 8px; font-size: 20px; }}
    button {{ padding: 9px 14px; border: 0; border-radius: 7px; background: #6d7cff; color: white; cursor: pointer; }}
    main {{ padding: 20px; max-width: 1500px; margin: auto; }}
    section {{ margin-bottom: 32px; padding: 18px; border: 1px solid #353946; border-radius: 12px; background: #181b22; }}
    h2 {{ margin: 0 0 5px; font-size: 18px; }}
    .meta {{ color: #aeb5c5; margin-bottom: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill,minmax(220px,1fr)); gap: 12px; }}
    .card {{ display: block; border: 1px solid #353946; border-radius: 9px; overflow: hidden; background: #20242d; cursor: pointer; }}
    .card:has(input:checked) {{ border-color: #75d69c; box-shadow: 0 0 0 2px #75d69c55; }}
    .card img {{ width: 100%; height: 190px; object-fit: contain; background: #0c0e12; }}
    .details {{ padding: 10px; }}
    .title {{ font-weight: 650; min-height: 2.5em; }}
    .small {{ color: #aeb5c5; font-size: 12px; margin-top: 4px; }}
    .controls {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 14px; }}
    textarea,input[type=text] {{ box-sizing: border-box; width: 100%; padding: 9px; border: 1px solid #454b5a; border-radius: 6px; background: #111318; color: white; }}
    textarea {{ min-height: 72px; }}
    @media (max-width: 700px) {{ .controls {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Met Galaxy Search Relevance Review</h1>
    <button id="download">Download reviewed judgments</button>
    <span id="progress" style="margin-left:12px"></span>
  </header>
  <main id="queries"></main>
  <script>
    const review = {review_json};
    const source = {judgment_json};
    const root = document.getElementById('queries');
    const protocol = source.reviewProtocol || {{}};
    const escapeText = value => value == null || value === '' ? 'Unknown' : String(value);
    review.queries.forEach((query, queryIndex) => {{
      const section = document.createElement('section');
      section.dataset.queryIndex = queryIndex;
      const heading = document.createElement('h2');
      heading.textContent = `${{queryIndex + 1}}. ${{query.query}}`;
      section.appendChild(heading);
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = `${{query.category}} — ${{protocol[query.category] || ''}}`;
      section.appendChild(meta);
      const grid = document.createElement('div');
      grid.className = 'grid';
      query.candidates.forEach(candidate => {{
        const card = document.createElement('label');
        card.className = 'card';
        const image = document.createElement('img');
        image.loading = 'lazy';
        image.src = candidate.imageUrl || '';
        image.alt = escapeText(candidate.title);
        card.appendChild(image);
        const details = document.createElement('div');
        details.className = 'details';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'relevant';
        checkbox.value = candidate.canonicalAssetId;
        checkbox.checked = query.existingRelevantCanonicalAssetIds.includes(candidate.canonicalAssetId);
        details.appendChild(checkbox);
        details.append(' Relevant');
        const title = document.createElement('div');
        title.className = 'title';
        title.textContent = escapeText(candidate.title);
        details.appendChild(title);
        const artist = document.createElement('div');
        artist.className = 'small';
        artist.textContent = escapeText(candidate.artist);
        details.appendChild(artist);
        const ids = document.createElement('div');
        ids.className = 'small';
        ids.textContent = `asset ${{candidate.canonicalAssetId}} · object ${{escapeText(candidate.objectId)}}`;
        details.appendChild(ids);
        const ranks = document.createElement('div');
        ranks.className = 'small';
        ranks.textContent = Object.entries(candidate.ranks).map(([mode, rank]) => `${{mode}} #${{rank}}`).join(' · ');
        details.appendChild(ranks);
        card.appendChild(details);
        grid.appendChild(card);
      }});
      section.appendChild(grid);
      const controls = document.createElement('div');
      controls.className = 'controls';
      const missing = document.createElement('input');
      missing.type = 'text';
      missing.className = 'missing';
      missing.placeholder = 'Missing relevant canonical asset IDs, comma-separated';
      controls.appendChild(missing);
      const reviewedLabel = document.createElement('label');
      const reviewed = document.createElement('input');
      reviewed.type = 'checkbox';
      reviewed.className = 'reviewed';
      reviewed.checked = query.existingReviewed;
      reviewedLabel.appendChild(reviewed);
      reviewedLabel.append(' Complete review for this query');
      controls.appendChild(reviewedLabel);
      const notes = document.createElement('textarea');
      notes.className = 'notes';
      notes.placeholder = 'Review notes or ambiguity';
      notes.value = query.existingNotes;
      controls.appendChild(notes);
      section.appendChild(controls);
      root.appendChild(section);
    }});
    function updateProgress() {{
      const complete = document.querySelectorAll('.reviewed:checked').length;
      document.getElementById('progress').textContent = `${{complete}} / ${{review.queries.length}} queries reviewed`;
    }}
    document.addEventListener('change', updateProgress);
    updateProgress();
    document.getElementById('download').addEventListener('click', () => {{
      const judgments = [...document.querySelectorAll('section')].map((section, index) => {{
        const selected = [...section.querySelectorAll('.relevant:checked')].map(input => Number(input.value));
        const missing = section.querySelector('.missing').value
          .split(',')
          .map(value => value.trim())
          .filter(Boolean)
          .map(Number)
          .filter(value => Number.isSafeInteger(value) && value > 0);
        return {{
          query: review.queries[index].query,
          category: review.queries[index].category,
          relevantCanonicalAssetIds: [...new Set([...selected, ...missing])].sort((a, b) => a - b),
          notes: section.querySelector('.notes').value,
          reviewed: section.querySelector('.reviewed').checked
        }};
      }});
      const output = {{...source, judgments}};
      const blob = new Blob([JSON.stringify(output, null, 2) + '\\n'], {{type:'application/json'}});
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'search-relevance.json';
      link.click();
      URL.revokeObjectURL(link.href);
    }});
  </script>
</body>
</html>
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Pool canonical search results into a human review artifact."
    )
    parser.add_argument(
        "--judgments",
        type=Path,
        default=Path("evaluation/search-relevance.json"),
    )
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--candidate-limit", type=int, default=40)
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("evaluation/search-review-candidates.json"),
    )
    parser.add_argument(
        "--html-output",
        type=Path,
        default=Path("evaluation/search-review.html"),
    )
    args = parser.parse_args()
    if not 5 <= args.count <= 100:
        parser.error("--count must be between 5 and 100")
    if not args.count <= args.candidate_limit <= 200:
        parser.error("--candidate-limit must be between count and 200")
    if not 1 <= args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be between 1 and 300")
    return args


def main():
    args = parse_args()
    try:
        judgments = load_judgments(args.judgments)
        endpoint = f"{args.base_url.rstrip('/')}/api/artworks/search"
        queries = []
        for index, judgment in enumerate(judgments["judgments"], start=1):
            query_pool = build_query_pool(
                endpoint,
                judgment,
                args.count,
                args.candidate_limit,
                args.timeout_seconds,
            )
            queries.append(query_pool)
            print(
                json.dumps(
                    {
                        "query": index,
                        "total": len(judgments["judgments"]),
                        "text": judgment["query"],
                        "candidates": len(query_pool["candidates"]),
                    }
                ),
                flush=True,
            )
        payload = {
            "version": 1,
            "datasetId": judgments.get("datasetId"),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "endpoint": endpoint,
            "modes": MODES,
            "queries": queries,
        }
        write_json_atomic(args.json_output, payload)
        args.html_output.parent.mkdir(parents=True, exist_ok=True)
        temporary_html = args.html_output.with_suffix(f"{args.html_output.suffix}.tmp")
        temporary_html.write_text(
            html_document(payload, judgments),
            encoding="utf-8",
        )
        temporary_html.replace(args.html_output)
        print(
            json.dumps(
                {
                    "status": "complete",
                    "queries": len(queries),
                    "json": str(args.json_output),
                    "html": str(args.html_output),
                },
                indent=2,
            ),
            flush=True,
        )
        return 0
    except (ReviewBuildError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
