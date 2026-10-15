# Cypress Colab Runner 🌲⚡

A lightweight, portable toolkit to run **Cypress** (GPU-accelerated PCB Placement) on **Google Colab Free Tier** (NVIDIA Tesla T4 GPU) from **any machine**, using Google Drive as an asynchronous batch queue.

---

## Why This Exists

Cypress relies on **DREAMPlace** and legacy dependencies (Boost 1.66, Python 3.8, CUDA 11.3, PyTorch 1.10.0+cu113). Building Cypress natively on modern operating systems (Fedora 40+, Ubuntu 22.04+, macOS) is notoriously brittle and fails due to compiler and ABI incompatibilities.

This repository decouples the host machine from the build toolchain:
- **Zero local build dependencies**: You only need `python` and `rclone` locally.
- **Generous GPU compute**: Uses Google Colab's Free Tier NVIDIA Tesla T4 (16GB VRAM) on demand.
- **No static infrastructure costs**: Runs entirely on free Google Drive and Colab resources.
- **Portability**: Submit placement jobs from your laptop, desktop, or CI/CD runner.

---

## Repository Structure

```text
cypress-colab-runner/
├── notebooks/
│   └── Cypress_Colab_Runner.ipynb    # Google Colab notebook (T4 GPU runtime)
├── scripts/
│   ├── submit_job.py                 # Host CLI: packages Bookshelf design & submits
│   ├── colab_queue_worker.py         # Colab daemon: watches queue/ & executes jobs
│   └── build_sif.sh                  # Optional: script to rebuild Apptainer .sif
├── configs/
│   ├── test_small1.json              # Working small benchmark configuration
│   └── template.json                 # Annotated configuration template
├── benchmarks/
│   └── small-1/                      # Sample Bookshelf PCB placement design
├── docs/
│   └── architecture_and_learnings.md # Deep dive into Colab quirks, capabilities, & fixes
└── README.md
```

---

## Quickstart: Use from Any Machine

### Step 1: Clone this Repository
```bash
git clone <your-repo-url>/cypress-colab-runner.git
cd cypress-colab-runner
```

### Step 2: One-Time Google Drive Setup
1. Open Google Drive and create a dedicated folder, e.g. `cypress_colab_runtime`.
2. Upload the prebuilt Apptainer image to this folder:
   - Target location: `My Drive/cypress_colab_runtime/cypress.sif`
3. Upload `notebooks/Cypress_Colab_Runner.ipynb` to Google Drive (or open directly in [Google Colaboratory](https://colab.research.google.com/)).

### Step 3: Configure `rclone` (Local Machine)
Install `rclone` (standard cross-platform cloud storage CLI):
- **Fedora/RHEL**: `sudo dnf install rclone`
- **Ubuntu/Debian**: `sudo apt install rclone`
- **macOS**: `brew install rclone`
- **Windows**: `winget install Rclone.Rclone`

Link your Google Drive account:
```bash
rclone config
```
- Select `n` (New remote), name it `cypress_runtime`.
- Select `drive` (Google Drive).
- Leave client ID / Secret blank (defaults).
- Select scope `1` (Full access).
- Authorize in the browser with your Colab Google Account.
- Test connection:
  ```bash
  rclone lsd cypress_runtime:
  ```

---

## Running Placements

### 1. Start the Colab Worker
1. Open `Cypress_Colab_Runner.ipynb` in Colab.
2. Select **T4 GPU** (`Runtime` $\to$ `Change runtime type` $\to$ `T4 GPU`).
3. Run **Cells 1 through 4**.
4. Cell 4 will start listening for jobs:
   ```text
   ============================================================
     CYPRESS GOOGLE DRIVE QUEUE WORKER STARTED
     Workspace:      /workspace
     Poll Interval:  5s
   ============================================================
   [WORKER] Monitoring '/workspace/queue' for incoming jobs...
   ```

### 2. Submit a Job from Your Local Terminal
Submit the built-in benchmark design or any of your custom designs:

```bash
python scripts/submit_job.py \
  --rclone cypress_runtime:cypress_colab_runtime \
  --name my_first_placement \
  --config configs/test_small1.json \
  --wait
```

### 3. Retrieve Results
Because `--wait` is enabled, the CLI client will:
1. Package all Bookshelf files (`.aux`, `.nodes`, `.nets`, `.wts`, `.pl`, `.scl`) and `config.json`.
2. Upload them atomically to Google Drive.
3. Colab's GPU will execute placement.
4. The client will download the finished `.gp.pl` coordinates, logs, and plots into `./results/my_first_placement/`.
5. Output summary metrics:
   ```json
   === PLACEMENT SUMMARY ===
   {
     "status": "SUCCESS",
     "final_hpwl": 83187.36,
     "final_overflow": 0.00742,
     "iterations": 2711,
     "elapsed_seconds": 18.42
   }
   ```

---

## Custom Designs: Ingestion Workflow

To place your custom board design:
1. Translate your CAD design (KiCad, IDF, Circuit-JSON) into **Bookshelf format** (`.aux`, `.nodes`, `.nets`, `.wts`, `.pl`, `.scl`).
2. Create a JSON config pointing to your `.aux` file (see `configs/template.json`).
3. Run:
   ```bash
   python scripts/submit_job.py \
     --rclone cypress_runtime:cypress_colab_runtime \
     --name board_rev1 \
     --config /path/to/my_config.json \
     --aux /path/to/my_board.aux \
     --wait
   ```
4. Back-annotate the returned `.gp.pl` component center coordinates and rotations to your downstream EDA tool.

---

## Technical Learnings & Troubleshooting

For a detailed explanation of Google Colab's Docker environment, capability drops (`CAP_DAC_READ_SEARCH`), Apptainer configurations, and the lazy-loading of `/dev/nvidia-uvm`, see:
👉 **[docs/architecture_and_learnings.md](docs/architecture_and_learnings.md)**
