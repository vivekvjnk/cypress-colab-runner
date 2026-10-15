#!/usr/bin/env bash
set -euo pipefail

# build_sif.sh: Builds the Cypress Apptainer (.sif) image from the Docker environment.
# Run from the Cypress repository root where Dockerfile and Dockerfile.colab are located.

echo "=== 1. Building Base Docker Image (cypress-dev-env) ==="
docker compose build --build-arg BUILDKIT_INLINE_CACHE=1

echo "=== 2. Building Self-contained Standalone Image ==="
docker build -f Dockerfile.colab -t cypress-standalone:latest .

echo "=== 3. Converting to Apptainer SIF ==="
TMP_DIR="${HOME}/.apptainer_tmp"
mkdir -p "${TMP_DIR}"

# Build apptainer SIF using apptainer-converter helper image or local apptainer
if command -v apptainer &> /dev/null; then
    APPTAINER_TMPDIR="${TMP_DIR}" apptainer build --force cypress.sif docker-daemon:cypress-standalone:latest
else
    docker run --rm --privileged \
      -v "${PWD}:${PWD}" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "${TMP_DIR}:${TMP_DIR}" \
      -e APPTAINER_TMPDIR="${TMP_DIR}" \
      -w "${PWD}" \
      apptainer-converter:latest build --force cypress.sif docker-daemon:cypress-standalone:latest
fi

rm -rf "${TMP_DIR}"
echo "=== SIF Build Complete: cypress.sif ==="
