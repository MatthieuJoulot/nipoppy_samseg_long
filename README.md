# nipoppy_samseg_long

[Nipoppy](https://nipoppy.readthedocs.io) processing pipelines for **SAMSEG
longitudinal** segmentation, wrapping the `mri_robust_template` and
`run_samseg_long` tools through [niwrap](https://niwrap.dev/):

1. **`mri_robust_template`** — builds an unbiased within-subject template from a
   participant's T1w images and resamples each session into that template space.
2. **`run_samseg_long`** — runs SAMSEG longitudinal segmentation on those
   template-space images.

Everything runs **once per participant**; the sessions are discovered
automatically from the BIDS tree.

## Two flavours

The same workflow is packaged two ways — pick whichever fits how you like to run
things:

| Directory | Pipeline (`NAME`) | Steps | Worker script | When to use |
|:---------:|:-----------------:|:-----:|:-------------:|:-----------:|
| [`one_shot/`](one_shot) | `samseg_long_onestep` | 1 | `one_stage_samseg_long.py` | Simplest — a single `nipoppy process` call runs both tools back-to-back per participant. |
| [`two_shot/`](two_shot) | `samseg_long` | 2 | `two_stage_samseg_long.py` | Two steps, `robust_template` then `samseg_long` — run and track the template and the segmentation separately (e.g. inspect templates before segmenting, or parallelise differently). |

Both produce identical outputs and share the same fixes (see [Notes](#notes)).

Each folder ships a precise, step-by-step runbook —
[`one_shot/instruction.md`](one_shot/instruction.md) and
[`two_shot/instruction.md`](two_shot/instruction.md) — that can be followed by hand
or fed to an LLM/agent to drive the processing end to end. The sections below are the
overview; the `instruction.md` files are the exact procedure.

## Requirements

- The **`mri_robust_template`** and **`run_samseg_long`** commands on `$PATH`, with
  `FREESURFER_HOME` set — the pipelines invoke these tools **by name** (niwrap
  `use_local` runs whatever binary is on `$PATH`), and `run_samseg_long` (SAMSEG)
  loads its atlas files from `$FREESURFER_HOME/average/samseg` at runtime, so both
  the binaries and that env var must be present or the run fails.
- **Python ≥ 3.11**
- **niwrap** — `pip install -r requirements.txt`
- **nipoppy** ≥ 0.4 (tested with 0.4.6)

> [!IMPORTANT]
> These pipelines run **container-less**: nipoppy executes `python …` directly
> on the host. So `python` must resolve to a **Python ≥ 3.11** interpreter that
> has the `requirements.txt` packages installed — i.e. **run `nipoppy` itself
> from that environment**. If your `nipoppy` lives in a Python 3.10 env, either
> create a 3.11 env that has both `nipoppy` and the requirements, or edit the
> `command-line` in `descriptor.json` to point at a specific interpreter, e.g.
> `/path/to/py311/bin/python [SCRIPT_PATH] …`.

## Install

```bash
# 1. Install the Python dependencies into the env you run nipoppy from (Py >=3.11)
pip install -r requirements.txt

# 2. Make sure your nipoppy dataset runs container-less: in <dataset>/global_config.json
#      "CONTAINER_CONFIG": { "COMMAND": null, ... }

# 3. Install whichever pipeline you want into your nipoppy dataset
nipoppy pipeline install --dataset <dataset> path/to/nipoppy_samseg_long/one_shot
# or
nipoppy pipeline install --dataset <dataset> path/to/nipoppy_samseg_long/two_shot
```

## Run

### one_shot — single step
```bash
nipoppy process --dataset <dataset> \
  --pipeline samseg_long_onestep --pipeline-version 1.0.0 \
  --participant-id <ID>          # optional; omit to run all participants

nipoppy track-processing --dataset <dataset> \
  --pipeline samseg_long_onestep --pipeline-version 1.0.0
```

### two_shot — two steps (run in order)
```bash
# step 1: build the templates
nipoppy process --dataset <dataset> \
  --pipeline samseg_long --pipeline-version 1.0.0 \
  --pipeline-step robust_template --participant-id <ID>

# step 2: longitudinal segmentation (reads step 1's output)
nipoppy process --dataset <dataset> \
  --pipeline samseg_long --pipeline-version 1.0.0 \
  --pipeline-step samseg_long --participant-id <ID>

nipoppy track-processing --dataset <dataset> \
  --pipeline samseg_long --pipeline-version 1.0.0 --pipeline-step robust_template
nipoppy track-processing --dataset <dataset> \
  --pipeline samseg_long --pipeline-version 1.0.0 --pipeline-step samseg_long
```

## Inputs & outputs

**Input:** a BIDS dataset with anatomical T1w images
`sub-<ID>/ses-<S>/anat/sub-<ID>_ses-<S>_T1w.nii.gz`.

Sessions are discovered by globbing and **naturally sorted** (numeric first, then
letter suffix: `ses-1a < ses-1b < ses-2`). A participant with fewer than **2**
sessions is **skipped and logged** (a longitudinal template needs ≥2 timepoints).

**Output** (under the pipeline's output dir, per participant `sub-<ID>/`):

```
sub-<ID>/
  sub-<ID>_longTemplate<S1.S2...>.mgz                              # unbiased template
  sub-<ID>_ses-<S>_space-longTemplate<S1.S2...>_T1w.nii.gz         # registered image (per session)
  sub-<ID>_ses-<S>_from-native_to-space-longTemplate<...>_xfm.lta  # transform (per session)
  samseg_long/
    base/            # SAMSEG base subject
    latentAtlases/
    tp001/ tp002/ …  # per-timepoint segmentation; tpNNN/seg.mgz is the completion marker
```

The trackers mark a participant complete when `sub-<ID>/samseg_long/tp*/seg.mgz`
(and, for two_shot's first step, the template/registered/transform files) exist.

## Notes

- **Thread cap (segfault fix).** `run_samseg_long`'s sklearn/GMM step uses
  OpenBLAS, which can **segfault** on many-core hosts
  (`OpenBLAS warning: precompiled NUM_THREADS exceeded`). The worker sets
  `OMP_NUM_THREADS=2` (unless already set) and `OPENBLAS_NUM_THREADS=1` before
  launching it. No action needed on your part.
- **Repeated `--timepoint`.** niwrap's high-level `run_samseg_long` wrapper emits
  a single `-t` for all inputs, which `run_samseg_long` rejects ("must provide more
  than 1 timepoint"). The worker builds the command with one `--timepoint` per session
  itself, while still using niwrap's runner for execution/portability.
- **Runtime.** SAMSEG longitudinal is slow (tens of minutes even for 2 sessions).
  For long jobs, launch detached (e.g. `setsid`/`nohup`) so a dropped shell
  doesn't kill the run.

## Layout

```
nipoppy_samseg_long/
├── README.md
├── requirements.txt                # shared by both flavours
├── .gitignore
├── one_shot/                       # pipeline: samseg_long_onestep (1 step)
│   ├── config.json
│   ├── descriptor.json
│   ├── invocation.json
│   ├── tracker.json
│   ├── one_stage_samseg_long.py
│   └── instruction.md
└── two_shot/                       # pipeline: samseg_long (2 steps)
    ├── config.json
    ├── descriptor.json
    ├── invocation_robust.json
    ├── invocation_samseg.json
    ├── tracker_robust.json
    ├── tracker_samseg.json
    ├── two_stage_samseg_long.py
    └── instruction.md
```

## Credit

Wraps the `mri_robust_template` and `run_samseg_long` commands via
[niwrap](https://niwrap.dev/); packaged for
[Nipoppy](https://nipoppy.readthedocs.io). Please cite the relevant method papers
(Reuter et al. 2012 for the robust template; Puonti et al. 2016 / Cerri et al.
2021 for SAMSEG) when using these outputs.
