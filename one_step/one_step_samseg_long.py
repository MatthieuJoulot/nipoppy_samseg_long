#!/usr/bin/env python
"""Single-step per-participant SAMSEG longitudinal worker, via niwrap.

Runs the whole chain for one participant, in sequence:
  1. mri_robust_template : BIDS T1w  -> unbiased template + per-session
     registered images (space-longTemplate) + lta transforms, in OUTPUT_DIR/<sub>/
  2. run_samseg_long     : those registered images -> longitudinal segmentation
     in OUTPUT_DIR/<sub>/samseg_long/

Usage:
    python one_step_samseg_long.py BIDS_DIR OUTPUT_DIR PARTICIPANT_ID

Sessions are discovered from the BIDS tree and naturally ordered (ses-1a <
ses-1b < ses-2). A participant with fewer than 2 sessions is skipped and logged
(exit 0), since a longitudinal template/segmentation needs at least 2 timepoints.

Run with a Python 3.11 env that has niwrap installed. The mri_robust_template and
run_samseg_long commands must be reachable the way the chosen niwrap runner
expects (use_local -> on $PATH, with FREESURFER_HOME set for the SAMSEG atlases).
"""

import os
import re
import sys
from pathlib import Path

import niwrap
from styxdefs import get_global_runner

# niwrap 1.0.1 ships each tool as its own top-level package (`import
# freesurfer`); older / hub-style layouts expose it as `niwrap.freesurfer`.
try:
    from niwrap import freesurfer
except ImportError:
    import freesurfer


def natural_key(session_label: str):
    """Sort key: numeric value first, then letters (ses-1a < ses-1b < ses-2).

    Each chunk is tagged with a type rank (0 = number, 1 = text) so ints and
    strs are never compared directly; this stays safe for mixed cohorts where
    some sessions start with a digit and others with a letter.
    """
    return [
        (0, int(chunk), "") if chunk.isdigit() else (1, 0, chunk)
        for chunk in re.split(r"(\d+)", session_label)
        if chunk != ""
    ]


def bids_subject(participant_id: str) -> str:
    """BIDS-style label (sub-XXX), tolerant of the participant id prefix."""
    bare = participant_id[len("sub-"):] if participant_id.startswith("sub-") else participant_id
    return f"sub-{bare}"


def t1w_ext(name: str) -> str:
    """The NIfTI extension of a T1w-style filename: '.nii.gz' or '.nii'."""
    return ".nii.gz" if name.endswith(".nii.gz") else ".nii"


def discover_bids_sessions(bids: Path, bids_sub: str) -> list[tuple[str, str]]:
    """(session label, T1w path) pairs for the participant, naturally ordered.

    Accepts both ``.nii`` and ``.nii.gz`` inputs; if a session has both, the
    ``.nii.gz`` one wins (deterministic: sorted iteration, last write wins)."""
    found: dict[str, str] = {}
    for t1w in sorted(bids.glob(f"{bids_sub}/ses-*/anat/{bids_sub}_ses-*_T1w.nii*")):
        m = re.search(r"_ses-([^_]+)_T1w", t1w.name)
        if m:
            found[m.group(1)] = str(t1w)
    return sorted(found.items(), key=lambda kv: natural_key(kv[0]))


def main(bids_dir: str, out_dir: str, participant_id: str) -> None:
    bids = Path(bids_dir)
    out = Path(out_dir)
    bids_sub = bids_subject(participant_id)

    print(f"Participant: {bids_sub}")
    print(f"with BIDS: {bids_dir}")
    print(f"with OUT: {out_dir}")

    discovered = discover_bids_sessions(bids, bids_sub)  # [(session, t1w_path), ...]
    sessions = [s for s, _ in discovered]
    print(f"Discovered sessions: {sessions}")

    if len(discovered) < 2:
        print(
            f"SKIP {bids_sub}: found {len(discovered)} session(s); the longitudinal "
            f"chain needs at least 2. Nothing to do."
        )
        return

    # Outputs are grouped by tool: OUTPUT_DIR/mri_robust_template/<sub>/ and
    # OUTPUT_DIR/samseg_long/<sub>/.
    mrt_dir = out / "mri_robust_template" / bids_sub
    mrt_dir.mkdir(parents=True, exist_ok=True)

    template_sessions = ".".join(sessions)  # e.g. 1a.1b.2

    # Inputs are the actual discovered files (.nii or .nii.gz); each registered
    # output mirrors its input's extension.
    input_filenames = [path for _, path in discovered]
    template_filename = str(
        mrt_dir / f"{bids_sub}_longTemplate{template_sessions}.mgz"
    )
    registered_filenames = [
        str(mrt_dir
            / f"{bids_sub}_ses-{s}_space-longTemplate{template_sessions}_T1w{t1w_ext(path)}")
        for s, path in discovered
    ]
    transformation_filenames = [
        str(mrt_dir
            / f"{bids_sub}_ses-{s}_from-native_to-space-longTemplate{template_sessions}_xfm.lta")
        for s, _ in discovered
    ]

    niwrap.use_local()  # run the mri_robust_template / run_samseg_long tools from $PATH

    # --- Step 1: mri_robust_template ------------------------------------ #
    print("=== Step 1: mri_robust_template ===")
    print(f"  mov      = {input_filenames}")
    print(f"  template = {template_filename}")
    freesurfer.mri_robust_template(
        mov=input_filenames,
        template=template_filename,
        satit=True,
        mapmov=registered_filenames,
        lta=transformation_filenames,
    )
    print(f"Step 1 done. Template: {template_filename}")

    # --- Step 2: run_samseg_long ---------------------------------------- #
    # The registered images we just produced are the timepoint inputs (one
    # --timepoint each; the niwrap high-level wrapper can't emit repeated -t).
    print("=== Step 2: run_samseg_long ===")
    samseg_out = f"{out / 'samseg_long' / bids_sub}/"
    Path(samseg_out).mkdir(parents=True, exist_ok=True)

    # Cap thread counts: run_samseg_long's sklearn/GMM step uses OpenBLAS, which
    # segfaults on many-core hosts ("precompiled NUM_THREADS exceeded") if left
    # unbounded. These env vars propagate to the run_samseg_long subprocess.
    os.environ["OMP_NUM_THREADS"] = os.environ.get("OMP_NUM_THREADS", "2")
    os.environ["OPENBLAS_NUM_THREADS"] = "1"

    execution = get_global_runner().start_execution(
        freesurfer.RUN_SAMSEG_LONG_METADATA
    )
    cargs = ["run_samseg_long"]
    for f in registered_filenames:
        cargs += ["--timepoint", execution.input_file(f)]
    cargs += [
        "--output", samseg_out,
        "--save-warp",
        "--save-mesh",
        "--save-posteriors",
    ]
    print("Running:", " ".join(cargs))
    execution.run(cargs)
    print(f"Step 2 done. Output under: {samseg_out}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(
            "Usage: python one_step_samseg_long.py BIDS_DIR OUTPUT_DIR PARTICIPANT_ID"
        )
    main(sys.argv[1], sys.argv[2], sys.argv[3])
