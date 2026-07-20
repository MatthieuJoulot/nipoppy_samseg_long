#!/usr/bin/env python
"""Unified per-participant worker for the SAMSEG longitudinal chain, via niwrap.

ONE script, two tools selected by the first positional argument:

    python two_step_samseg_long.py mri_robust_template BIDS_DIR  OUTPUT_DIR PARTICIPANT_ID [options]
    python two_step_samseg_long.py run_samseg_long     INPUT_DIR OUTPUT_DIR PARTICIPANT_ID [options]

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

Run with a Python 3.11 env that has niwrap >= 1.0.3 installed.

Runner modes (--runner):
  local       (default) Run binaries from $PATH; FREESURFER_HOME must be set.
  docker      Run inside a Docker container (requires styxdocker).
  singularity Run inside a Singularity/Apptainer container (requires styxsingularity).
  podman      Run inside a Podman container (requires styxpodman).
  auto        Auto-detect the best available runner.

For container runners, --license is required (path to the FreeSurfer license
file). The container image is auto-resolved from niwrap metadata (currently
freesurfer/freesurfer:7.4.1). Use --image to override it if needed.
"""

import argparse
import os
import re
import sys
from pathlib import Path

import niwrap
from styxdefs import get_global_runner

# niwrap >= 1.0.3 exposes freesurfer as a subpackage of niwrap; older versions
# (1.0.1) ship it as a standalone top-level package.
try:
    from niwrap import freesurfer
except ImportError:
    import freesurfer


TOOLS = ("mri_robust_template", "run_samseg_long")
RUNNERS = ("local", "docker", "singularity", "podman", "auto")
CONTAINER_RUNNERS = ("docker", "singularity", "podman")


def setup_runner(
    runner: str = "local",
    license_file: str | None = None,
    image: str | None = None,
    mount_dirs: list[str] | None = None,
) -> None:
    """Configure the niwrap global runner.

    Args:
        runner: One of RUNNERS.
        license_file: Path to the FreeSurfer license.txt (required for
            container runners; the file is bind-mounted into the container).
        image: Optional container image override. When omitted the image tag
            baked into the niwrap metadata is used (e.g.
            ``freesurfer/freesurfer:7.4.1``).
        mount_dirs: Host directories to bind-mount read-write into the
            container (so absolute output paths resolve inside it).
    """
    if runner == "local":
        niwrap.use_local()
        print("Runner: local")
        return

    # -- container runner --------------------------------------------------
    if runner in CONTAINER_RUNNERS and not license_file:
        raise SystemExit(f"--license is required when --runner={runner}")

    # Override the metadata image tag if the user asked for a custom image.
    if image:
        freesurfer.MRI_ROBUST_TEMPLATE_METADATA = (
            freesurfer.MRI_ROBUST_TEMPLATE_METADATA._replace(container_image_tag=image)
        )
        freesurfer.RUN_SAMSEG_LONG_METADATA = (
            freesurfer.RUN_SAMSEG_LONG_METADATA._replace(container_image_tag=image)
        )

    # Build bind-mount flags. Docker/Podman use OCI -v; Singularity uses
    # --bind with the same host:container[:ro] syntax. SingularityRunner also
    # drops the host filesystem by default (--no-mount hostfs), so we keep that
    # and add explicit binds for the license file and the output directory.
    if runner == "singularity":
        mount_flag = "--bind"
        extra_args = ["--no-mount", "hostfs"]
    else:  # docker, podman, auto -> docker-style -v
        mount_flag = "-v"
        extra_args = []
    if license_file:
        lf = str(Path(license_file).resolve())
        extra_args += [mount_flag, f"{lf}:/usr/local/freesurfer/.license:ro"]
    for d in mount_dirs or []:
        rd = str(Path(d).resolve())
        extra_args += [mount_flag, f"{rd}:{rd}"]

    kwargs: dict = {}
    if runner == "docker":
        kwargs["docker_extra_args"] = extra_args
        niwrap.use_docker(**kwargs)
    elif runner == "singularity":
        kwargs["singularity_extra_args"] = extra_args
        niwrap.use_singularity(**kwargs)
    elif runner == "podman":
        kwargs["podman_extra_args"] = extra_args
        niwrap.use_podman(**kwargs)
    elif runner == "auto":
        niwrap.use_auto(**kwargs)
    else:
        raise SystemExit(f"Unknown runner {runner!r}; expected one of {RUNNERS}")

    tag = freesurfer.MRI_ROBUST_TEMPLATE_METADATA.container_image_tag
    print(f"Runner: {runner}  image: {image or tag}")


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

    # Outputs grouped by tool: OUTPUT_DIR/mri_robust_template/<sub>/.
    subject_out_dir = out / bids_sub / "mri_robust_template"
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
    session. Returns the timepoint file paths (one per session). Step 1 writes
    these under <in_dir>/mri_robust_template/<sub>/."""
    found = []  # (session_label, path)
    for img in (in_dir / bids_sub / "mri_robust_template").glob(
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
            f"{in_ / bids_sub / 'mri_robust_template'}; run_samseg_long needs at "
            f"least 2 timepoints. Nothing to do."
        )
        return

    # Samseg results grouped by tool: OUTPUT_DIR/samseg_long/<sub>/.
    output_path = f"{out / bids_sub / 'samseg_long'}/"
    Path(output_path).mkdir(parents=True, exist_ok=True)

    # Cap thread counts before launching run_samseg_long. Its Python/sklearn
    # (GMM init) step uses OpenBLAS, which segfaults on many-core hosts with
    # "precompiled NUM_THREADS exceeded" if left unbounded. The original bash
    # set OMP_NUM_THREADS=2 for the same reason. These env vars propagate to the
    # run_samseg_long subprocess spawned by niwrap below.
    os.environ["OMP_NUM_THREADS"] = os.environ.get("OMP_NUM_THREADS", "2")
    os.environ["OPENBLAS_NUM_THREADS"] = "1"

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
def main(
    tool: str,
    in_dir: str,
    out_dir: str,
    participant_id: str,
    runner: str = "local",
    license_file: str | None = None,
    image: str | None = None,
) -> None:
    # Collect directories that need to be visible inside the container.
    mount_dirs = [str(Path(out_dir).resolve())]
    if tool == "run_samseg_long" and str(Path(in_dir).resolve()) != mount_dirs[0]:
        mount_dirs.append(str(Path(in_dir).resolve()))

    setup_runner(runner, license_file=license_file, image=image,
                 mount_dirs=mount_dirs)

    if tool == "mri_robust_template":
        run_mri_robust_template(in_dir, out_dir, participant_id)
    elif tool == "run_samseg_long":
        run_run_samseg_long(in_dir, out_dir, participant_id)
    else:
        raise SystemExit(f"Unknown TOOL {tool!r}; expected one of {TOOLS}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Two-step SAMSEG longitudinal worker (select tool via first argument).",
    )
    parser.add_argument(
        "tool", choices=TOOLS,
        help="Which tool to run: mri_robust_template or run_samseg_long.",
    )
    parser.add_argument("input_dir", help="Input root (BIDS dir for step 1; step-1 output for step 2).")
    parser.add_argument("output_dir", help="Output directory.")
    parser.add_argument("participant_id", help="Participant label (with or without sub- prefix).")
    parser.add_argument(
        "--runner", choices=RUNNERS, default="local",
        help="niwrap runner backend (default: local).",
    )
    parser.add_argument(
        "--license", dest="license_file", default=None,
        help="Path to FreeSurfer license.txt (required for container runners).",
    )
    parser.add_argument(
        "--image", default=None,
        help=(
            "Override the container image (default: auto-resolved from niwrap "
            "metadata, currently freesurfer/freesurfer:7.4.1)."
        ),
    )
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    main(args.tool, args.input_dir, args.output_dir, args.participant_id,
         runner=args.runner, license_file=args.license_file, image=args.image)
