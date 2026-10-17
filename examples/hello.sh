#!/usr/bin/env bash
set -euo pipefail
mkdir -p /tmp/agent-cloud-results
{
  date -u +%FT%TZ
  python3 --version
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
} > /tmp/agent-cloud-results/environment.txt
cat /tmp/agent-cloud-results/environment.txt
