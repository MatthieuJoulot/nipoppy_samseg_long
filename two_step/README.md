# Instruction — two_step (`samseg_long`)

Precise runbook to install and run this pipeline. Readable by a human or usable
as-is by an LLM/agent to drive the processing. Follow the steps in order. Where a
command is given, run it verbatim after substituting the variables in **Section 0**.

This pipeline runs the same workflow as `one_step` but as **two separate nipoppy
steps**, executed **in order**, once per participant:

1. step **`robust_template`** — `mri_robust_template`: BIDS T1w → unbiased
   longitudinal template + per-session registered images.
2. step **`samseg_long`** — `run_samseg_long`: the registered images from step 1
   → SAMSEG longitudinal segmentation.

Step 2 reads step 1's output (both steps share the pipeline output dir), so **step 1
must finish before step 2**.

- Pipeline `NAME`: **`samseg_long`**
- Pipeline `VERSION`: **`1.0.0`**
- Steps: **2** — `robust_template`, then `samseg_long` (pass with `--pipeline-step`)
- Worker script: `two_step_samseg_long.py`, called as
  `python two_step_samseg_long.py <TOOL> <INPUT_DIR> <OUTPUT_DIR> <PARTICIPANT_ID>`
  where `<TOOL>` is `mri_robust_template` (step 1) or `run_samseg_long` (step 2).

---

## 0. Variables to set

```bash
BUNDLE=/ABS/PATH/TO/nipoppy_samseg_long/two_step   # this folder
DATASET=/ABS/PATH/TO/nipoppy_dataset               # an initialized nipoppy dataset root
PARTICIPANT=SUB01                                  # participant label, with or without "sub-"
```

- `BUNDLE` must be this directory (contains `config.json`, `two_step_samseg_long.py`, …).
- `DATASET` must be an existing nipoppy dataset (created with `nipoppy init`) whose
  `bids/` contains `sub-<PARTICIPANT>/ses-*/anat/sub-<PARTICIPANT>_ses-*_T1w.nii.gz`.
- Omit `--participant-id "$PARTICIPANT"` in Steps 5–6 to process **all** participants.

---

## 1. Preconditions — verify ALL of these pass before continuing

Run each; every one must succeed. Do **not** proceed if any fails — see **Troubleshooting**.

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
> `run_samseg_long` commands must all be reachable from the **same environment**
> (container-less + bare `python` in the descriptor). Use a single Python ≥3.11 conda/venv
> with `nipoppy` + `requirements.txt` installed and those commands on `$PATH`
> (`FREESURFER_HOME` set). Activate it now.

---

## 2. Install the Python dependencies (one-time per environment)

```bash
pip install -r "$BUNDLE/../requirements.txt"
```

(`requirements.txt` lives at the repository root, shared with `one_step`.)

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
nipoppy pipeline list --dataset "$DATASET"   # expect: samseg_long (1.0.0)
```

---

## 5. Run step 1 — `robust_template` (fast, ≈2–3 min per 2 sessions)

```bash
nipoppy process --dataset "$DATASET" \
  --pipeline samseg_long --pipeline-version 1.0.0 \
  --pipeline-step robust_template \
  --participant-id "$PARTICIPANT"
```

Confirm step 1 produced the registered images before starting step 2:

```bash
OUT="$DATASET/derivatives/samseg_long/1.0.0/output"
SUB="sub-${PARTICIPANT#sub-}"
ls "$OUT"/mri_robust_template/"$SUB"/"$SUB"_longTemplate*.mgz
ls "$OUT"/mri_robust_template/"$SUB"/"$SUB"_ses-*_space-longTemplate*_T1w.nii*   # step-2 inputs; need >=2
```

Do **not** continue until these exist.

---

## 6. Run step 2 — `samseg_long` (slow, ≈30–60 min; launch detached)

```bash
setsid bash -c '
  nipoppy process --dataset "'"$DATASET"'" \
    --pipeline samseg_long --pipeline-version 1.0.0 \
    --pipeline-step samseg_long \
    --participant-id "'"$PARTICIPANT"'"
' > "$DATASET/samseg_long_${PARTICIPANT}.log" 2>&1 < /dev/null &
echo "started; log: $DATASET/samseg_long_${PARTICIPANT}.log"
```

Watch: `tail -f "$DATASET/samseg_long_${PARTICIPANT}.log"`.
Finished when the log contains `Ran for 1 out of 1`.

---

## 7. Verify success

```bash
OUT="$DATASET/derivatives/samseg_long/1.0.0/output"
SUB="sub-${PARTICIPANT#sub-}"
ls "$OUT"/samseg_long/"$SUB"/tp*/seg.mgz     # per-timepoint segmentations (>=2)

nipoppy track-processing --dataset "$DATASET" \
  --pipeline samseg_long --pipeline-version 1.0.0 --pipeline-step robust_template
nipoppy track-processing --dataset "$DATASET" \
  --pipeline samseg_long --pipeline-version 1.0.0 --pipeline-step samseg_long
```

The pipeline is **complete** when **both** `track-processing` calls report each session
as `SUCCESS` in `$DATASET/derivatives/processing_status.tsv`.

### Expected outputs (under `.../output/`, grouped by tool)
```
mri_robust_template/sub-<ID>/                                      # from step 1 (robust_template)
  sub-<ID>_longTemplate<S1.S2...>.mgz                              # template
  sub-<ID>_ses-<S>_space-longTemplate<S1.S2...>_T1w.nii[.gz]       # registered image, per session
  sub-<ID>_ses-<S>_from-native_to-space-longTemplate<...>_xfm.lta  # transform, per session
samseg_long/sub-<ID>/                                              # from step 2 (samseg_long)
  base/   latentAtlases/   tp001/ tp002/ …                         # SAMSEG; tpNNN/seg.mgz per timepoint
```
Sessions are auto-discovered and naturally ordered (`ses-1a < ses-1b < ses-2`);
step 2 discovers its timepoints by globbing step 1's `*_space-longTemplate*_T1w.nii.gz`.

---

## Troubleshooting

- **`AttributeError: module 'typing' has no attribute 'NotRequired'`** (or `ModuleNotFoundError: niwrap`)
  → the `python` running the script is < 3.11 or lacks the deps. Run everything from a
  Python ≥3.11 env that has `requirements.txt` and `nipoppy` installed (Section 1).
- **Step 2 fails immediately / no inputs found** → step 1 didn't finish or wrote nowhere.
  Re-check the `*_space-longTemplate*_T1w.nii.gz` files exist (end of Step 5) before running step 2.
- **`return code -11` / segfault with `OpenBLAS warning: precompiled NUM_THREADS exceeded`**
  → thread over-subscription. The worker already sets `OMP_NUM_THREADS=2` and
  `OPENBLAS_NUM_THREADS=1`; if it still occurs, export lower values before running
  (`export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1`).
- **A container/apptainer/singularity error** → the dataset is not container-less. Redo Step 3.
- **`SKIP … found N session(s) … needs at least 2`** → the participant has <2 T1w sessions.
  Expected behaviour; nothing to fix.
- **Run died when the terminal closed** → you didn't launch detached; redo Step 6 with `setsid`.
