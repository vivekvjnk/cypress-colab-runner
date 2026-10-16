#!/usr/bin/env python3
"""
Cypress Colab Queue Worker (Protocol Option A)

Monitors a shared Google Drive directory for placement jobs, executes them
sequentially using Cypress with GPU acceleration, and writes the output back
to Google Drive.

Directory Structure:
  <workspace>/
    queue/         <- Input jobs deposited here
    running/       <- Job currently being placed
    completed/     <- Successful jobs with .gp.pl, logs, plots, summary.json
    failed/        <- Failed jobs with error logs
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def parse_summary_from_log(log_path):
    summary = {
        "status": "UNKNOWN",
        "final_hpwl": None,
        "final_overflow": None,
        "iterations": None,
        "timestamp": datetime.now().isoformat(),
    }
    if not os.path.exists(log_path):
        return summary

    with open(log_path, "r", errors="ignore") as f:
        content = f.read()

    # Look for last iteration line:
    # iteration 2711, (2711, 0, 0), Obj ..., HPWL 8.318736E+04, Overflow 7.420439E-03
    matches = re.findall(
        r"iteration\s+(\d+).*?HPWL\s+([\d\.E\+\-]+).*?Overflow\s+([\d\.E\+\-]+)",
        content,
    )
    if matches:
        last_iter, last_hpwl, last_overflow = matches[-1]
        summary["iterations"] = int(last_iter)
        try:
            summary["final_hpwl"] = float(last_hpwl)
            summary["final_overflow"] = float(last_overflow)
        except ValueError:
            pass

    if "placement succeeds" in content.lower() or summary["iterations"] is not None:
        summary["status"] = "SUCCESS"

    return summary


def process_job(job_dir, cypress_root):
    job_path = Path(job_dir)
    print(f"\n[WORKER] ===== Processing Job: {job_path.name} =====")
    start_time = time.time()

    # Find config.json in job directory
    config_candidates = list(job_path.glob("*.json"))
    # Prefer config.json if present
    config_file = None
    for cand in config_candidates:
        if cand.name.lower() == "config.json":
            config_file = cand
            break
    if not config_file and config_candidates:
        config_file = config_candidates[0]

    if not config_file:
        raise FileNotFoundError(f"No .json configuration file found in {job_path}")

    print(f"[WORKER] Found configuration: {config_file.name}")
    with open(config_file, "r") as f:
        config = json.load(f)

    # Adjust paths: aux_input relative to job_dir if relative
    if "aux_input" in config:
        aux_input = config["aux_input"]
        aux_candidate = job_path / aux_input
        if aux_candidate.exists():
            config["aux_input"] = str(aux_candidate)
        else:
            # Check if any .aux file exists in the directory
            aux_files = list(job_path.glob("*.aux"))
            if aux_files:
                config["aux_input"] = str(aux_files[0])
                print(f"[WORKER] Auto-resolved aux_input to {config['aux_input']}")

    # Configure output directory inside job_dir
    result_dir = job_path / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    config["result_dir"] = str(result_dir)

    # Force GPU flag to 1 on Colab
    config["gpu"] = 1

    # Write patched config
    resolved_config = job_path / "resolved_config.json"
    with open(resolved_config, "w") as f:
        json.dump(config, f, indent=2)

    # Run Placer.py with cwd set to the writable job directory
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{cypress_root}:{existing_pythonpath}".rstrip(":")

    cmd = [
        sys.executable,
        str(Path(cypress_root) / "dreamplace" / "Placer.py"),
        str(resolved_config),
    ]

    log_path = job_path / "DREAMPlace.log"
    print(f"[WORKER] Executing command: {' '.join(cmd)}")
    with open(log_path, "w") as log_file:
        proc = subprocess.run(
            cmd,
            cwd=str(job_path),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    elapsed = time.time() - start_time
    print(f"[WORKER] Completed in {elapsed:.2f} seconds. Exit code: {proc.returncode}")

    # Copy any generated .gp.pl or images to top of job_path for easy access
    for artifact in result_dir.glob("*"):
        if artifact.is_file():
            dest = job_path / artifact.name
            if not dest.exists():
                shutil.copy2(artifact, dest)

    summary = parse_summary_from_log(log_path)
    summary["elapsed_seconds"] = round(elapsed, 2)
    summary["exit_code"] = proc.returncode

    with open(job_path / "summary.json", "w") as sf:
        json.dump(summary, sf, indent=2)

    if proc.returncode != 0:
        raise RuntimeError(f"Cypress process exited with code {proc.returncode}. See DREAMPlace.log")


def run_worker(workspace, cypress_root, poll_interval=5):
    workspace = Path(workspace).resolve()
    queue_dir = workspace / "queue"
    running_dir = workspace / "running"
    completed_dir = workspace / "completed"
    failed_dir = workspace / "failed"

    for d in [queue_dir, running_dir, completed_dir, failed_dir]:
        d.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  CYPRESS GOOGLE DRIVE QUEUE WORKER STARTED")
    print(f"  Workspace:      {workspace}")
    print(f"  Cypress Root:   {cypress_root}")
    print(f"  Poll Interval:  {poll_interval}s")
    print("=" * 60)
    print(f"[WORKER] Monitoring '{queue_dir}' for incoming jobs...")

    while True:
        try:
            # Check for directories in queue
            job_dirs = sorted([p for p in queue_dir.iterdir() if p.is_dir()])

            if not job_dirs:
                time.sleep(poll_interval)
                continue

            for job in job_dirs:
                # Check for upload completion marker: MUST have READY and NOT have .uploading
                ready_marker = job / "READY"
                settle_file = job / ".uploading"
                if settle_file.exists() or not ready_marker.exists():
                    continue

                # Ensure config.json or a .json file actually landed and is valid
                json_files = list(job.glob("*.json"))
                if not json_files:
                    # Files may still be syncing across Google Drive, wait for next tick
                    continue

                try:
                    with open(json_files[0], "r") as f:
                        json.load(f)
                except Exception:
                    # File not fully flushed yet, wait
                    continue

                job_name = job.name
                target_running = running_dir / job_name
                target_completed = completed_dir / job_name
                target_failed = failed_dir / job_name

                # Clean previous instance if re-running
                if target_running.exists():
                    shutil.rmtree(target_running)

                print(f"[WORKER] Claiming job {job_name} (READY detected)...")
                # Remove READY marker to prevent duplicate triggers
                try:
                    ready_marker.unlink()
                except OSError:
                    pass

                shutil.move(str(job), str(target_running))

                try:
                    process_job(target_running, cypress_root)
                    if target_completed.exists():
                        shutil.rmtree(target_completed)
                    shutil.move(str(target_running), str(target_completed))
                    print(f"[WORKER] SUCCESS: Job {job_name} moved to {completed_dir}")
                except Exception as err:
                    print(f"[WORKER] ERROR processing {job_name}: {err}")
                    error_log = target_running / "error.log"
                    with open(error_log, "a") as ef:
                        ef.write(f"Timestamp: {datetime.now().isoformat()}\nError: {err}\n")
                    if target_failed.exists():
                        shutil.rmtree(target_failed)
                    shutil.move(str(target_running), str(target_failed))
                    print(f"[WORKER] FAILED: Job {job_name} moved to {failed_dir}")

        except KeyboardInterrupt:
            print("\n[WORKER] Stopped by user.")
            break
        except Exception as e:
            print(f"[WORKER] Unexpected worker loop error: {e}")
            time.sleep(poll_interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cypress Colab Queue Worker")
    parser.add_argument(
        "--workspace",
        type=str,
        default="/content/drive/MyDrive/Cypress_Colab",
        help="Path to shared Google Drive Cypress folder",
    )
    parser.add_argument(
        "--cypress-root",
        type=str,
        default="/Cypress",
        help="Path to Cypress repository root inside container",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Seconds between polling queue directory",
    )
    args = parser.parse_args()

    run_worker(args.workspace, args.cypress_root, args.poll_interval)
