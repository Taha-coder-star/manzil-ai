#!/usr/bin/env bash
# One-time server setup + (re)deploy on Ubuntu 24.04. Run from the repo root.
set -euo pipefail

if ! command -v docker >/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y docker.io docker-compose-v2
  sudo systemctl enable --now docker
fi

test -f .env || { echo "Missing .env in $(pwd)"; exit 1; }

sudo docker compose -f deploy/compose.yml --env-file .env build
sudo docker compose -f deploy/compose.yml --env-file .env up -d
sudo docker compose -f deploy/compose.yml --env-file .env ps
