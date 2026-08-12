#!/usr/bin/env bash
set -euo pipefail

REPO="Jwagener1/Systems-One-Server"
RUNNER_DIR="$HOME/actions-runner"
LABELS="self-hosted,s1-server"
RUNNER_NAME="s1-server"

if [ -z "${RUNNER_TOKEN:-}" ]; then
  echo "RUNNER_TOKEN env var required. Get one with:" >&2
  echo "  gh api -X POST repos/$REPO/actions/runners/registration-token --jq .token" >&2
  exit 1
fi

VERSION=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
  | grep -oP '"tag_name": "v\K[0-9.]+' | head -1)

if [ -z "$VERSION" ]; then
  echo "failed to detect latest actions/runner version" >&2
  exit 1
fi

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"
curl -fsSL -o actions-runner-linux-x64.tar.gz \
  "https://github.com/actions/runner/releases/download/v${VERSION}/actions-runner-linux-x64-${VERSION}.tar.gz"
tar xzf actions-runner-linux-x64.tar.gz

./config.sh --url "https://github.com/$REPO" \
  --token "$RUNNER_TOKEN" \
  --labels "$LABELS" \
  --name "$RUNNER_NAME" \
  --work "_work" \
  --unattended

sudo ./svc.sh install
sudo ./svc.sh start
