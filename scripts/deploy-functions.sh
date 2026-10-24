#!/usr/bin/env bash
# Deploy the API + 3 AM alarm to DigitalOcean Functions.
# Needs doctl (`brew install doctl`), `doctl auth init`, `doctl serverless install`,
# `doctl serverless connect`, and functions/.env filled in.
set -euo pipefail
cd "$(dirname "$0")/.."
cp mumble/core.py functions/packages/mumble/api/core.py
doctl serverless deploy functions --remote-build
doctl serverless functions get mumble/api --url
