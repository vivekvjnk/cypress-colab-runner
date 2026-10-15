#!/usr/bin/env python3
"""
Host-side Job Submitter for Cypress Colab (Protocol Option A)

Prepares and submits a placement job to Google Drive:
  - Method 1: Via rclone remote (e.g. --rclone gdrive:Cypress_Colab)
  - Method 2: Via local synced folder (e.g. --workspace ~/GoogleDrive/Cypress_Colab)

Usage:
  python scripts/submit_colab_job.py \
    --rclone gdrive:Cypress_Colab \
    --name run_small_1 \
    --config configs/test_small1.json \
    --wait
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def prepare_job_dir(staging_dir, config_path, aux_path=None):
    staging_path = Path(staging_dir)
    config_file = Path(config_path).resolve()
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_file, "r") as f:
        config = json.load(f)

    if not aux_path and "aux_input" in config:
        aux_path = config["aux_input"]

    if aux_path:
        aux_file = Path(aux_path).resolve()
        if not aux_file.exists():
            alt_aux = config_file.parent / aux_path
            if alt_aux.exists():
                aux_file = alt_aux

        if not aux_file.exists():
            raise FileNotFoundError(f"Aux file not found: {aux_path}")

        aux_dir = aux_file.parent
        print(f"[SUBMIT] Packaging design files from {aux_dir}...")

        with open(aux_file, "r") as af:
            aux_content = af.read()

        shutil.copy2(aux_file, staging_path / aux_file.name)
        config["aux_input"] = aux_file.name

        for token in aux_content.split():
            candidate = aux_dir / token
            if candidate.is_file():
                shutil.copy2(candidate, staging_path / token)

    dest_config = staging_path / "config.json"
    with open(dest_config, "w") as f:
        json.dump(config, f, indent=2)

    (staging_path / "READY").touch()


def submit_via_rclone(remote, job_name, config_path, aux_path=None, wait=False, out_dir="results"):
    remote = remote.rstrip("/")

    # Sync latest worker script to Google Drive workspace root
    worker_script = Path(__file__).parent / "colab_queue_worker.py"
    if worker_script.exists():
        subprocess.run(["rclone", "copyto", str(worker_script), f"{remote}/colab_queue_worker.py"], capture_output=True)

    with tempfile.TemporaryDirectory(prefix="cypress_job_") as tmp_dir:
        staging_dir = Path(tmp_dir) / job_name
        staging_dir.mkdir(parents=True)

        prepare_job_dir(staging_dir, config_path, aux_path)

        target_queue = f"{remote}/queue/{job_name}"
        print(f"[SUBMIT] Uploading job files to {target_queue}...")
        # Step 1: Upload all design files and config.json FIRST (excluding READY)
        res = subprocess.run(
            ["rclone", "copy", str(staging_dir), target_queue, "--exclude", "READY"],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(f"rclone copy failed: {res.stderr}")

        # Step 2: Upload READY marker ONLY after all files have landed
        print(f"[SUBMIT] Signaling READY to Colab worker...")
        res_ready = subprocess.run(
            ["rclone", "copyto", str(staging_dir / "READY"), f"{target_queue}/READY"],
            capture_output=True,
            text=True,
        )
        if res_ready.returncode != 0:
            raise RuntimeError(f"rclone trigger failed: {res_ready.stderr}")

        print(f"[SUBMIT] Job '{job_name}' queued and triggered successfully on Google Drive!")

    if wait:
        print(f"[SUBMIT] Waiting for Colab GPU worker to process '{job_name}'...")
        completed_remote = f"{remote}/completed/{job_name}"
        failed_remote = f"{remote}/failed/{job_name}"
        local_result_dir = Path(out_dir) / job_name

        while True:
            # Check completed
            chk_comp = subprocess.run(["rclone", "lsf", completed_remote], capture_output=True, text=True)
            if chk_comp.returncode == 0 and chk_comp.stdout.strip():
                print(f"\n[SUBMIT] SUCCESS! Downloading results from {completed_remote}...")
                local_result_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(["rclone", "copy", completed_remote, str(local_result_dir)], check=True)
                print(f"[SUBMIT] Artifacts saved to: {local_result_dir}")
                summary_file = local_result_dir / "summary.json"
                if summary_file.exists():
                    with open(summary_file) as sf:
                        print("\n=== PLACEMENT SUMMARY ===")
                        print(json.dumps(json.load(sf), indent=2))
                break

            # Check failed
            chk_fail = subprocess.run(["rclone", "lsf", failed_remote], capture_output=True, text=True)
            if chk_fail.returncode == 0 and chk_fail.stdout.strip():
                print(f"\n[SUBMIT] FAILED! Downloading failure logs from {failed_remote}...")
                local_result_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(["rclone", "copy", failed_remote, str(local_result_dir)], check=True)
                error_file = local_result_dir / "error.log"
                if error_file.exists():
                    print("\n=== ERROR LOG ===")
                    print(error_file.read_text())
                break

            time.sleep(5)


def submit_via_local_workspace(workspace, job_name, config_path, aux_path=None, wait=False):
    workspace = Path(workspace).expanduser().resolve()
    queue_dir = workspace / "queue"
    completed_dir = workspace / "completed"
    failed_dir = workspace / "failed"

    queue_dir.mkdir(parents=True, exist_ok=True)
    target_job_dir = queue_dir / job_name

    if target_job_dir.exists():
        raise FileExistsError(f"Job {job_name} already exists in queue: {target_job_dir}")

    target_job_dir.mkdir(parents=True)
    uploading_marker = target_job_dir / ".uploading"
    uploading_marker.touch()

    prepare_job_dir(target_job_dir, config_path, aux_path)

    uploading_marker.unlink()
    print(f"[SUBMIT] Successfully queued job '{job_name}' at {target_job_dir}")

    if wait:
        print("[SUBMIT] Waiting for completion on Google Colab...")
        comp_dir = completed_dir / job_name
        fail_dir = failed_dir / job_name

        while True:
            if comp_dir.exists():
                print(f"\n[SUBMIT] Job completed successfully! Output in {comp_dir}")
                summary_file = comp_dir / "summary.json"
                if summary_file.exists():
                    with open(summary_file) as sf:
                        print(json.dumps(json.load(sf), indent=2))
                break
            elif fail_dir.exists():
                print(f"\n[SUBMIT] Job failed! See error log at {fail_dir}")
                break
            time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Submit Cypress job to Colab Queue")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--rclone",
        help="rclone remote path (e.g. gdrive:Cypress_Colab)",
    )
    group.add_argument(
        "--workspace",
        help="Local synced path (e.g. ~/GoogleDrive/Cypress_Colab)",
    )
    parser.add_argument(
        "--name",
        default=f"job_{int(time.time())}",
        help="Unique job name",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to JSON config file",
    )
    parser.add_argument(
        "--aux",
        help="Path to Bookshelf .aux file (optional if specified in config)",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Block until job finishes in Colab and download results",
    )
    parser.add_argument(
        "--out-dir",
        default="results",
        help="Directory to save downloaded results (default: results)",
    )
    args = parser.parse_args()

    if args.rclone:
        submit_via_rclone(args.rclone, args.name, args.config, args.aux, args.wait, args.out_dir)
    else:
        submit_via_local_workspace(args.workspace, args.name, args.config, args.aux, args.wait)
