#!/usr/bin/env python
"""Unified per-participant worker for the SAMSEG longitudinal chain, via niwrap.

ONE script, two tools selected by the first positional argument:

    python two_step_samseg_long.py mri_robust_template BIDS_DIR  OUTPUT_DIR PARTICIPANT_ID
    python two_step_samseg_long.py run_samseg_long     INPUT_DIR OUTPUT_DIR PARTICIPANT_ID

Step 1 (mri_robust_template): reads the participant's BIDS anat T1w images and
writes the unbiased template, the per-session registered images
(``<sub>_ses-<s>_space-longTemplate<S>_T1w.nii.gz``) and the lta transforms to
``OUTPUT_DIR/<sub>/``.

Step 2 (run_samseg_long): reads those registered images from ``INPUT_DIR/<sub>/``
(i.e. step-1 output) and writes the SAMSEG longitudinal results to
``OUTPUT_DIR/<sub>/samseg_long/``.

Both steps: per-participant, sessions/timepoints discovered by globbing and
ordered with the same natural sort (numeric value first, then letter suffix:
ses-1a < ses-1b < ses-2), participant id tolerant of the ``sub-`` prefix, and a
participant with fewer than 2 timepoints is skipped and logged (exit 0).

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


TOOLS = ("mri_robust_template", "run_samseg_long")


def natural_key(session_label: str):
    """Sort key: numeric value first, then letters (ses-1a < ses-1b < ses-2).

    Each chunk is tagged with a type rank (0 = number, 1 = text) so ints and
    strs are never compared directly; this stays safe for mixed cohorts where
    some sessions start with a digit and others with a letter (digit-leading
    labels then sort before letter-leading ones).
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


# --------------------------------------------------------------------------- #
# Step 1: mri_robust_template                                                #
# --------------------------------------------------------------------------- #
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


def run_mri_robust_template(bids_dir: str, out_dir: str, participant_id: str) -> None:
    bids = Path(bids_dir)
    out = Path(out_dir)
    bids_sub = bids_subject(participant_id)

    print(f"[mri_robust_template] Participant: {bids_sub}")
    print(f"with BIDS: {bids_dir}")
    print(f"with OUT: {out_dir}")

    discovered = discover_bids_sessions(bids, bids_sub)  # [(session, t1w_path), ...]
    sessions = [s for s, _ in discovered]
    print(f"Discovered sessions: {sessions}")

    if len(discovered) < 2:
        print(
            f"SKIP {bids_sub}: found {len(discovered)} session(s); "
            f"mri_robust_template needs at least 2. Nothing to do."
        )
        return

    # Inputs are the actual discovered files (.nii or .nii.gz).
    input_filenames = [path for _, path in discovered]

    subject_out_dir = out / bids_sub
    subject_out_dir.mkdir(parents=True, exist_ok=True)

    template_sessions = ".".join(sessions)  # e.g. 1a.1b.2
    template_filename = str(
        subject_out_dir / f"{bids_sub}_longTemplate{template_sessions}.mgz"
    )

    # Each registered output mirrors its input's extension.
    registered_filenames: list[str] = []
    transformation_filenames: list[str] = []
    for s, path in discovered:
        registered_filenames.append(str(
            subject_out_dir
            / f"{bids_sub}_ses-{s}_space-longTemplate{template_sessions}_T1w{t1w_ext(path)}"
        ))
        transformation_filenames.append(str(
            subject_out_dir
            / f"{bids_sub}_ses-{s}_from-native_to-space-longTemplate{template_sessions}_xfm.lta"
        ))

    print("Input filenames:")
    print("\n".join(input_filenames))
    print("Template filename:")
    print(template_filename)
    print("Registered filenames:")
    print("\n".join(registered_filenames))
    print("Transformation filenames:")
    print("\n".join(transformation_filenames))

    niwrap.use_local()
    print("Running mri_robust_template via niwrap...")
    outputs = freesurfer.mri_robust_template(
        mov=input_filenames,
        template=template_filename,
        satit=True,
        mapmov=registered_filenames,
        lta=transformation_filenames,
    )
    print(f"Done. Template written to: {outputs.template_output}")


# --------------------------------------------------------------------------- #
# Step 2: run_samseg_long                                                     #
# --------------------------------------------------------------------------- #
def discover_registered(in_dir: Path, bids_sub: str) -> list[str]:
    """Step-1 registered images for the participant, naturally ordered by
    session. Returns the timepoint file paths (one per session)."""
    found = []  # (session_label, path)
    for img in (in_dir / bids_sub).glob(
        f"{bids_sub}_ses-*_space-longTemplate*_T1w.nii*"
    ):
        m = re.search(r"_ses-([^_]+)_space-longTemplate", img.name)
        if m:
            found.append((m.group(1), str(img)))
    found.sort(key=lambda pair: natural_key(pair[0]))
    return [path for _, path in found]


def run_run_samseg_long(in_dir: str, out_dir: str, participant_id: str) -> None:
    in_ = Path(in_dir)
    out = Path(out_dir)
    bids_sub = bids_subject(participant_id)

    print(f"[run_samseg_long] Participant: {bids_sub}")
    print(f"with IN: {in_dir}")
    print(f"with OUT: {out_dir}")

    input_files = discover_registered(in_, bids_sub)
    print(f"Discovered {len(input_files)} registered timepoint image(s):")
    print("\n".join(input_files))

    if len(input_files) < 2:
        print(
            f"SKIP {bids_sub}: found {len(input_files)} registered image(s) in "
            f"{in_ / bids_sub}; run_samseg_long needs at least 2 timepoints. "
            f"Nothing to do."
        )
        return

    # Samseg results live in their own subdir, separate from step-1 files.
    output_path = f"{out / bids_sub / 'samseg_long'}/"
    Path(output_path).mkdir(parents=True, exist_ok=True)

    # Cap thread counts before launching run_samseg_long. Its Python/sklearn
    # (GMM init) step uses OpenBLAS, which segfaults on many-core hosts with
    # "precompiled NUM_THREADS exceeded" if left unbounded. The original bash
    # set OMP_NUM_THREADS=2 for the same reason. These env vars propagate to the
    # run_samseg_long subprocess spawned by niwrap below.
    os.environ["OMP_NUM_THREADS"] = os.environ.get("OMP_NUM_THREADS", "2")
    os.environ["OPENBLAS_NUM_THREADS"] = "1"

    niwrap.use_local()
    execution = get_global_runner().start_execution(
        freesurfer.RUN_SAMSEG_LONG_METADATA
    )

    # Build the command ourselves: one --timepoint per file (the niwrap 1.0.1
    # high-level wrapper emits a single -t and run_samseg_long then aborts).
    cargs = ["run_samseg_long"]
    for f in input_files:
        cargs += ["--timepoint", execution.input_file(f)]
    cargs += [
        "--output", output_path,
        "--save-warp",
        "--save-mesh",
        "--save-posteriors",
    ]

    print("Running:", " ".join(cargs))
    execution.run(cargs)
    print(f"Done. Output written under: {output_path}")


# --------------------------------------------------------------------------- #
def main(tool: str, in_dir: str, out_dir: str, participant_id: str) -> None:
    if tool == "mri_robust_template":
        run_mri_robust_template(in_dir, out_dir, participant_id)
    elif tool == "run_samseg_long":
        run_run_samseg_long(in_dir, out_dir, participant_id)
    else:
        raise SystemExit(f"Unknown TOOL {tool!r}; expected one of {TOOLS}")


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] not in TOOLS:
        raise SystemExit(
            "Usage: python two_step_samseg_long.py "
            "{mri_robust_template|run_samseg_long} INPUT_DIR OUTPUT_DIR PARTICIPANT_ID"
        )
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
