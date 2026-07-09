# Instruction — one_step (`samseg_long_onestep`)

Precise runbook to install and run this pipeline. Readable by a human or usable
as-is by an LLM/agent to drive the processing. Follow the steps in order. Where a
command is given, run it verbatim after substituting the variables in **Section 0**.

This pipeline runs, **once per participant, in a single nipoppy step**:
`mri_robust_template` (BIDS T1w → unbiased longitudinal template + registered
images) → `run_samseg_long` (those images → SAMSEG longitudinal segmentation).

- Pipeline `NAME`: **`samseg_long_onestep`**
- Pipeline `VERSION`: **`1.0.0`**
- Steps: **1** (default step; do **not** pass `--pipeline-step`)
- Worker script: `one_step_samseg_long.py`, called as
  `python one_step_samseg_long.py <BIDS_DIR> <OUTPUT_DIR> <PARTICIPANT_ID>`

---

## 0. Variables to set

```bash
BUNDLE=/ABS/PATH/TO/nipoppy_samseg_long/one_step   # this folder
DATASET=/ABS/PATH/TO/nipoppy_dataset               # an initialized nipoppy dataset root
PARTICIPANT=SUB01                                  # participant label, with or without "sub-"
```

- `BUNDLE` must be this directory (contains `config.json`, `one_step_samseg_long.py`, …).
- `DATASET` must be an existing nipoppy dataset (created with `nipoppy init`) whose
  `bids/` contains `sub-<PARTICIPANT>/ses-*/anat/sub-<PARTICIPANT>_ses-*_T1w.nii.gz`.
- Omit `--participant-id "$PARTICIPANT"` in Step 4 to process **all** participants.

---

## 1. Preconditions — verify ALL of these pass before continuing

Run each; every one must succeed (non-error output). Do **not** proceed if any fails —
see **Troubleshooting**.

```bash
# 1a. nipoppy is installed and runnable
nipoppy --version

# 1b. The SAME python that runs nipoppy is >= 3.11  (hard requirement)
python -c "import sys; assert sys.version_info[:2] >= (3,11), sys.version; print('py ok', sys.version.split()[0])"

# 1c. niwrap and its tool-wrapper module import in that python
python -c "import niwrap, freesurfer; print('niwrap ok')"

# 1d. mri_robust_template / run_samseg_long commands are on PATH and FREESURFER_HOME is set
which mri_robust_template run_samseg_long
echo "FREESURFER_HOME=${FREESURFER_HOME:?FREESURFER_HOME is not set}"

# 1e. The dataset runs container-less (must print: null)
python -c "import json; print(json.load(open('$DATASET/global_config.json'))['CONTAINER_CONFIG']['COMMAND'])"

# 1f. The participant has >= 2 T1w sessions (must print a number >= 2)
ls "$DATASET"/bids/sub-${PARTICIPANT#sub-}/ses-*/anat/*_T1w.nii.gz | wc -l
```

> IMPORTANT: `nipoppy`, the `python` on PATH, `niwrap`, and the `mri_robust_template` /
> `run_samseg_long` commands must all be reachable from the **same environment**, because
> the pipeline runs container-less and the descriptor calls bare `python`. The simplest setup
> is a single Python ≥3.11 conda/venv that has `nipoppy` + `requirements.txt` installed, with
> those commands on `$PATH` and `FREESURFER_HOME` set. Activate it now.

---

## 2. Install the Python dependencies (one-time per environment)

```bash
pip install -r "$BUNDLE/../requirements.txt"
```

(`requirements.txt` lives at the repository root, shared with `two_step`.)

---

## 3. Ensure the dataset is container-less (one-time per dataset)

If Step 1e did **not** print `null`, set it:

```bash
python - "$DATASET/global_config.json" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p))
c["CONTAINER_CONFIG"]["COMMAND"] = None
json.dump(c, open(p, "w"), indent=4)
print("CONTAINER_CONFIG.COMMAND set to", c["CONTAINER_CONFIG"]["COMMAND"])
PY
```

---

## 4. Install the pipeline into the dataset

```bash
nipoppy pipeline install --dataset "$DATASET" "$BUNDLE" --assume-yes
nipoppy pipeline list --dataset "$DATASET"   # expect: samseg_long_onestep (1.0.0)
```

---

## 5. (Recommended) Dry-run to inspect the exact command

```bash
nipoppy process --dataset "$DATASET" \
  --pipeline samseg_long_onestep --pipeline-version 1.0.0 \
  --participant-id "$PARTICIPANT" --simulate
```

Expect a "Generated Command" of the form:
`python <DATASET>/pipelines/processing/samseg_long_onestep-1.0.0/one_step_samseg_long.py <bids> <output> sub-<PARTICIPANT>`
and `--no-container`.

---

## 6. Run

SAMSEG longitudinal is **slow** (≈30–60 min even for 2 sessions). Launch **detached**
so a dropped shell/SSH session does not kill it:

```bash
setsid bash -c '
  nipoppy process --dataset "'"$DATASET"'" \
    --pipeline samseg_long_onestep --pipeline-version 1.0.0 \
    --participant-id "'"$PARTICIPANT"'"
' > "$DATASET/one_step_${PARTICIPANT}.log" 2>&1 < /dev/null &
echo "started; log: $DATASET/one_step_${PARTICIPANT}.log"
```

Watch progress: `tail -f "$DATASET/one_step_${PARTICIPANT}.log"`.
The run is finished when the log contains `Ran for 1 out of 1`.

---

## 7. Verify success

```bash
OUT="$DATASET/derivatives/samseg_long_onestep/1.0.0/output/sub-${PARTICIPANT#sub-}"
ls "$OUT"/sub-*_longTemplate*.mgz                 # unbiased template
ls "$OUT"/samseg_long/tp*/seg.mgz                 # per-timepoint segmentations (>=2)

nipoppy track-processing --dataset "$DATASET" \
  --pipeline samseg_long_onestep --pipeline-version 1.0.0
```

The pipeline is **complete** when `track-processing` reports each session as
`SUCCESS` in `$DATASET/derivatives/processing_status.tsv`.

### Expected outputs (under `.../output/sub-<ID>/`)
```
sub-<ID>_longTemplate<S1.S2...>.mgz                               # template
sub-<ID>_ses-<S>_space-longTemplate<S1.S2...>_T1w.nii.gz          # registered image, per session
sub-<ID>_ses-<S>_from-native_to-space-longTemplate<...>_xfm.lta   # transform, per session
samseg_long/base/  samseg_long/latentAtlases/  samseg_long/tpNNN/ # SAMSEG; tpNNN/seg.mgz per timepoint
```
Sessions are auto-discovered from BIDS and naturally ordered (`ses-1a < ses-1b < ses-2`).

---

## Troubleshooting

- **`AttributeError: module 'typing' has no attribute 'NotRequired'`** (or `ModuleNotFoundError: niwrap`)
  → the `python` running the script is < 3.11 or lacks the deps. Run everything from a
  Python ≥3.11 env that has `requirements.txt` and `nipoppy` installed (Section 1).
- **`return code -11` / segfault with `OpenBLAS warning: precompiled NUM_THREADS exceeded`**
  → thread over-subscription. The worker already sets `OMP_NUM_THREADS=2` and
  `OPENBLAS_NUM_THREADS=1`; if it still occurs, export lower values before running
  (`export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1`).
- **A container/apptainer/singularity error** → the dataset is not container-less. Redo Step 3
  (`CONTAINER_CONFIG.COMMAND` must be `null`).
- **`SKIP … found N session(s) … needs at least 2`** → the participant has <2 T1w sessions.
  Expected behaviour (a longitudinal template needs ≥2 timepoints); nothing to fix.
- **Run died when the terminal closed** → you didn't launch detached; redo Step 6 with `setsid`.
