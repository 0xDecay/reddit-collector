#!/bin/bash
set -euo pipefail

# reddit-collector: one full cycle (collect → profile → status)
# Resolves its own directory so it works from cron.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Collecting Reddit feeds..."
python3 collect.py --subs-file subreddits.txt

echo "Profiling subreddits..."
python3 profile_subs.py

echo "Poll status:"
python3 collect.py --status
