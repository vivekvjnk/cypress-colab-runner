# Cypress Colab Runtime: Architecture, Learnings & Technical Reference

This document captures the architectural decisions, root-cause analyses, and operational knowledge gained while designing and deploying **Cypress** to an on-demand **Google Colab Free Tier** GPU instance.

---

## 1. System Architecture

```text
 ┌────────────────────────────────────────────────────────┐
 │                   Local Host (Any OS)                  │
 │  • EDA CAD Tools / Bookshelf files / configs           │
 │  • submit_job.py (CLI Client via rclone)               │
 └───────────────────────────┬────────────────────────────┘
                             │
                             │ (1) rclone copy design files & config.json
                             │ (2) rclone copyto READY trigger
                             ▼
 ┌────────────────────────────────────────────────────────┐
 │               Google Drive Shared Storage              │
 │  • cypress.sif                 (7.7GB Apptainer Image) │
 │  • colab_queue_worker.py       (Hot-reloadable worker) │
 │  • queue/<job_id>/             (Incoming job requests) │
 │  • running/<job_id>/           (Active execution)      │
 │  • completed/<job_id>/         (.gp.pl, logs, plots)   │
 │  • failed/<job_id>/            (error.log, traceback)  │
 └───────────────────────────┬────────────────────────────┘
                             │
                             │ (3) Atomic claim & execution
                             │ (4) Result extraction
                             ▼
 ┌────────────────────────────────────────────────────────┐
 │           Google Colab Free Tier (Tesla T4 GPU)        │
 │                                                        │
 │  1. Mount Drive (/content/drive/MyDrive/...)           │
 │  2. Apptainer Runtime (--nv, --no-privs)               │
 │  3. Cypress Container (PyTorch 1.10.0+cu113)           │
 │  4. Queue Worker (Processes queue/, outputs to Drive)  │
 └────────────────────────────────────────────────────────┘
```

---

## 2. Why Containerized Build vs. Host Native Build

### The Native Build Failure
Attempting to compile Cypress natively on modern Linux distributions (e.g. Fedora 43, Ubuntu 24.04, Arch Linux) fails catastrophically:
- **Compiler Incompatibility**: Modern GCC (GCC 13, 14, 15) rejects legacy C++ idioms in **Boost 1.66.0** (released in 2017) and DREAMPlace C++ templates.
- **Python & PyTorch ABI Mismatches**: Cypress custom operators were authored and compiled against **Python 3.8** and **PyTorch 1.10.0+cu113** with the legacy C++ ABI (`_GLIBCXX_USE_CXX11_ABI=0`). Modern Python 3.10+ and PyTorch 2.x cannot link or load these compiled `.so` modules without severe symbol lookup failures.

### The Container Solution
The base Docker container (`nvcr.io/nvidia/pytorch:21.10-py3`) pins:
- Ubuntu 20.04 LTS
- GCC 9.3
- Python 3.8.12
- PyTorch 1.10.0+cu113
- CUDA 11.4 / 11.3
- Boost 1.66.0 compiled with `./b2 -q install -j 16 --without-python`
- CMake 3.21.3

All 38 DREAMPlace custom C++/CUDA operators are compiled once in this pinned environment.

### Target Hardware Compatibility
- Google Colab Free Tier assigns an **NVIDIA Tesla T4 GPU** (Turing architecture, Compute Capability **`sm_75`**).
- Cypress `CMakeLists.txt` explicitly includes `CUDA_ARCH_LIST: 6.0 6.1 7.0 7.5 8.0`.
- The precompiled CUDA kernels in `cypress.sif` natively target `sm_75`, guaranteeing optimal execution on Colab's T4 GPU.

---

## 3. Google Colab Quirks & Root-Cause Solutions

During deployment to Google Colab, four distinct failure modes were identified and systematically resolved:

### Installation: Official Apptainer PPA
Ubuntu repositories on some Google Colab instances lack the latest Apptainer package or have missing dependency bindings. To guarantee a clean install, add the official PPA:
```bash
sudo apt-get update && sudo apt-get install -y software-properties-common
sudo add-apt-repository -y ppa:apptainer/ppa
sudo apt-get update -qq
sudo apt-get install -y -qq apptainer
```

### Issue 1: Kernel Overlayfs Capability Failure
- **Error**:
  ```text
  FATAL: container creation failed: mount hook function failure: mount overlay->/var/lib/apptainer/mnt/session/final error: while mounting overlay: can't mount overlay filesystem to /var/lib/apptainer/mnt/session/final: while setting effective capabilities: CAP_DAC_READ_SEARCH is not in the permitted capability set
  ```
- **Root Cause**: Colab VMs run inside Docker containers where the capability `CAP_DAC_READ_SEARCH` is stripped from the permitted set. Apptainer's default configuration tries to mount a kernel `overlayfs`, which requires `CAP_DAC_READ_SEARCH`.
- **Solution**:
  1. Install `fuse-overlayfs` (`sudo apt-get install -y fuse-overlayfs`).
  2. Configure Apptainer to use the userspace FUSE driver:
     ```bash
     sudo sed -i 's/^enable overlay = .*/enable overlay = driver/' /etc/apptainer/apptainer.conf
     ```
  3. Configure underlay fallback:
     ```bash
     sudo sed -i 's/^enable underlay = .*/enable underlay = preferred/' /etc/apptainer/apptainer.conf
     sudo sed -i 's/^max underlay binds = .*/max underlay binds = 5000/' /etc/apptainer/apptainer.conf
     ```

### Issue 2: Root Capability Assertion Mismatch
- **Error**:
  ```text
  ERROR: Requesting capability set 0x000001ffffffffff while permitted capability set is 0x00000000a82c25fb
  ```
- **Root Cause**: Because Colab runs as root (UID 0), Apptainer's default setting (`root default capabilities = full`) requests all 41 Linux capabilities (`0x000001ffffffffff`). But Colab's container runtime only permits `0x00000000a82c25fb`. Apptainer's starter binary detects that requested capabilities exceed permitted capabilities and aborts.
- **Solution**:
  1. Configure Apptainer to drop full root capability requests:
     ```bash
     sudo sed -i 's/^root default capabilities = full/root default capabilities = no/' /etc/apptainer/apptainer.conf
     ```
  2. Pass `--no-privs` to `apptainer exec`:
     ```bash
     apptainer exec --nv --no-privs ...
     ```

### Issue 3: CUDA Available: False Inside Container
- **Symptom**:
  ```text
  PyTorch Version: 1.10.0+cu113
  CUDA Available: False
  Device Name: None
  ```
- **Root Causes**:
  1. **Non-standard Library Path**: On Colab, NVIDIA driver libraries (`libcuda.so.1`, `libnvidia-ml.so`, etc.) reside in `/usr/lib64-nvidia/` instead of `/usr/lib/x86_64-linux-gnu`. Colab sets `LD_LIBRARY_PATH` instead of registering them in `ldconfig`, so Apptainer's `--nv` discovery mechanism cannot find them.
  2. **Lazy-loaded Unified Memory Driver**: On Colab, `/dev/nvidia-uvm` is not created until a CUDA operation runs on the host. If Apptainer starts before any host CUDA call, the UVM device node is missing.
- **Solution**:
  1. Register `/usr/lib64-nvidia` in `ldconfig` on the Colab host:
     ```bash
     echo "/usr/lib64-nvidia" | sudo tee /etc/ld.so.conf.d/nvidia.conf && sudo ldconfig
     ```
  2. Wake up CUDA on the Colab host:
     ```python
     import torch
     if torch.cuda.is_available():
         torch.zeros(1).cuda()
     ```
  3. Bind `/usr/lib64-nvidia` and set `LD_LIBRARY_PATH` during `apptainer exec`:
     ```bash
     apptainer exec --nv --no-privs \
       -B /usr/lib64-nvidia:/usr/lib64-nvidia \
       --env LD_LIBRARY_PATH="/usr/lib64-nvidia:/usr/local/nvidia/lib64" \
       ...
     ```

### Issue 4: Google Drive Asynchronous Transfer Race Condition
- **Error**:
  ```text
  Error: No .json configuration file found in /workspace/running/<job_id>
  ```
- **Root Cause**: When uploading a job folder via `rclone`, Google Drive creates the directory immediately and syncs files in chunks. The Colab worker detected the directory before `config.json` had landed, claimed the incomplete directory, and moved it to `failed/`.
- **Solution (Two-Phase Handshake)**:
  1. **Phase 1 (Client)**: `submit_job.py` uploads all design files and `config.json` with `--exclude READY`.
  2. **Phase 2 (Client)**: Only after all files are 100% written, `submit_job.py` uploads `READY`.
  3. **Verification (Worker)**: `colab_queue_worker.py` strictly ignores any job folder until `READY` exists, and validates that `config.json` is readable JSON before claiming.
