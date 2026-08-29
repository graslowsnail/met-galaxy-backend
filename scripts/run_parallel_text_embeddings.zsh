#!/bin/zsh

set -u
set -o pipefail

script_dir=${0:A:h}
repo_dir=${script_dir:h}
cd "$repo_dir"

worker_count=${TEXT_EMBEDDING_WORKERS:-12}
batch_size=${TEXT_EMBEDDING_BATCH_SIZE:-100}
worker_limit=${TEXT_EMBEDDING_WORKER_LIMIT:-50000}
max_rounds=${TEXT_EMBEDDING_MAX_ROUNDS:-12}
retry_wait_seconds=${TEXT_EMBEDDING_RETRY_WAIT_SECONDS:-45}
tokens_per_minute=${TEXT_EMBEDDING_TOKENS_PER_MINUTE:-950000}
rate_limit_file=${TEXT_EMBEDDING_RATE_LIMIT_FILE:-/tmp/met-galaxy-text-embedding-rate-limit.json}

if (( worker_count < 1 || batch_size < 1 || batch_size > 100 || tokens_per_minute < 1 )); then
  print -u2 "Worker count and token budget must be positive; batch size must be 1-100."
  exit 1
fi

caffeinate -dimsu &
caffeinate_pid=$!
typeset -a worker_pids
worker_pids=()

cleanup() {
  for worker_pid in "${worker_pids[@]}"; do
    kill "$worker_pid" >/dev/null 2>&1 || true
  done
  kill "$caffeinate_pid" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM HUP

for ((round = 1; round <= max_rounds; round++)); do
  print "Starting round $round of $max_rounds: $worker_count workers, shared $tokens_per_minute TPM budget."
  worker_pids=()

  for ((worker = 1; worker <= worker_count; worker++)); do
    TEXT_EMBEDDING_WORKER_ID="text-$round-$worker" \
      venv-embeddings/bin/python -u \
      scripts/generate-text-embeddings.py work \
      --limit "$worker_limit" \
      --batch-size "$batch_size" \
      --tokens-per-minute "$tokens_per_minute" \
      --token-rate-limit-file "$rate_limit_file" \
      --timeout 90 &
    worker_pids+=($!)
  done

  for worker_pid in "${worker_pids[@]}"; do
    wait "$worker_pid" || true
  done

  stats_json=$(venv-embeddings/bin/python scripts/generate-text-embeddings.py stats)
  print "$stats_json"

  pending=$(print -r -- "$stats_json" | jq -r '.pending')
  if [[ "$pending" = "0" ]]; then
    print "Text embedding backfill finished."
    exit 0
  fi

  if (( round < max_rounds )); then
    print "Waiting $retry_wait_seconds seconds for retryable rows."
    sleep "$retry_wait_seconds"
  fi
done

print -u2 "Stopped after $max_rounds rounds; inspect the final stats above before rerunning."
exit 1
