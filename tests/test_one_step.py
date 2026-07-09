"""Tests for one_step/one_step_samseg_long.py (single-step worker)."""

import os

import pytest


# --------------------------------------------------------------------------- #
# setup_runner                                                                 #
# --------------------------------------------------------------------------- #
def test_setup_runner_local_default(one_step, make_bids, tmp_path):
    """Default runner='local' calls niwrap.use_local()."""
    bids = make_bids("X", ["1a", "1b"])
    one_step.main(str(bids), str(tmp_path / "out"), "sub-X")
    assert one_step._record["use_local_called"]


def test_setup_runner_docker(one_step, make_bids, tmp_path):
    """runner='docker' calls niwrap.use_docker() with license mount."""
    bids = make_bids("X", ["1a", "1b"])
    one_step.main(str(bids), str(tmp_path / "out"), "sub-X",
                  runner="docker", license_file="/fake/license.txt")
    assert "use_docker_kwargs" in one_step._record
    # Verify license is mounted
    extra = one_step._record["use_docker_kwargs"]["docker_extra_args"]
    assert "/usr/local/freesurfer/.license:ro" in " ".join(extra)


def test_setup_runner_singularity(one_step, make_bids, tmp_path):
    """runner='singularity' uses --bind (not docker -v) for mounts."""
    bids = make_bids("X", ["1a", "1b"])
    one_step.main(str(bids), str(tmp_path / "out"), "sub-X",
                  runner="singularity", license_file="/fake/license.txt")
    assert "use_singularity_kwargs" in one_step._record
    extra = one_step._record["use_singularity_kwargs"]["singularity_extra_args"]
    extra_str = " ".join(extra)
    assert "--bind" in extra
    assert "/usr/local/freesurfer/.license:ro" in extra_str
    assert "--no-mount" in extra and "hostfs" in extra
    assert "-v" not in extra  # singularity must not get docker-style -v


def test_setup_runner_container_requires_license(one_step):
    """Container runners without --license raise SystemExit."""
    with pytest.raises(SystemExit, match="--license is required"):
        one_step.setup_runner("docker")
    with pytest.raises(SystemExit, match="--license is required"):
        one_step.setup_runner("singularity")
    with pytest.raises(SystemExit, match="--license is required"):
        one_step.setup_runner("podman")


def test_setup_runner_auto_no_license(one_step):
    """runner='auto' without license is fine (auto-detects, may pick local)."""
    one_step.setup_runner("auto")
    assert "use_auto_kwargs" in one_step._record


# --------------------------------------------------------------------------- #
# natural_key                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "labels, expected",
    [
        (["10", "2", "1"], ["1", "2", "10"]),                 # numeric, not lexicographic
        (["2", "1a", "1b"], ["1a", "1b", "2"]),               # numeric prefix then letter
        (["m12", "m00", "m06"], ["m00", "m06", "m12"]),       # letter-leading + embedded number
        (["followup2", "baseline", "followup"],
         ["baseline", "followup", "followup2"]),              # word labels
        (["m06", "2", "1a"], ["1a", "2", "m06"]),             # MIXED leading types must not crash
        (["baseline", "2", "1"], ["1", "2", "baseline"]),     # mixed: numbers before words
        (["010", "001", "002"], ["001", "002", "010"]),       # zero-padded compares numerically
    ],
)
def test_natural_key_sorting(one_step, labels, expected):
    assert sorted(labels, key=one_step.natural_key) == expected


# --------------------------------------------------------------------------- #
# bids_subject                                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "given, expected",
    [
        ("MIRIAD188", "sub-MIRIAD188"),
        ("sub-MIRIAD188", "sub-MIRIAD188"),
        ("01", "sub-01"),
    ],
)
def test_bids_subject_prefix_tolerance(one_step, given, expected):
    assert one_step.bids_subject(given) == expected


# --------------------------------------------------------------------------- #
# discover_bids_sessions                                                       #
# --------------------------------------------------------------------------- #
def _labels(discovered):
    return [s for s, _ in discovered]


def test_discover_bids_sessions_sorted(one_step, make_bids):
    bids = make_bids("X", ["2", "1b", "1a"])           # created out of order
    assert _labels(one_step.discover_bids_sessions(bids, "sub-X")) == ["1a", "1b", "2"]


def test_discover_bids_sessions_accepts_nii_and_gz(one_step, make_bids):
    bids = make_bids("X", [("1a", ".nii"), ("1b", ".nii.gz")])   # mixed extensions
    discovered = one_step.discover_bids_sessions(bids, "sub-X")
    assert _labels(discovered) == ["1a", "1b"]
    assert [os.path.basename(p) for _, p in discovered] == [
        "sub-X_ses-1a_T1w.nii",
        "sub-X_ses-1b_T1w.nii.gz",
    ]


def test_discover_bids_sessions_ignores_other_subjects(one_step, make_bids):
    bids = make_bids("X", ["1a", "1b"])
    make_bids("Y", ["1a", "1b", "1c"], root_name="bids")  # same tree, different subject
    assert _labels(one_step.discover_bids_sessions(bids, "sub-X")) == ["1a", "1b"]


def test_discover_bids_sessions_missing_dir_is_empty(one_step, tmp_path):
    assert one_step.discover_bids_sessions(tmp_path / "nope", "sub-X") == []


# --------------------------------------------------------------------------- #
# main() flow (with the fake niwrap stack recording calls)                     #
# --------------------------------------------------------------------------- #
def test_main_builds_expected_names_and_command(one_step, make_bids, tmp_path):
    bids = make_bids("X", ["1a", "1b"])
    out = tmp_path / "out"

    one_step.main(str(bids), str(out), "sub-X")

    rec = one_step._record

    # Step 1: mri_robust_template called once with the expected naming
    assert len(rec["mrt_calls"]) == 1
    kw = rec["mrt_calls"][0]
    assert kw["satit"] is True
    assert [os.path.basename(p) for p in kw["mov"]] == [
        "sub-X_ses-1a_T1w.nii.gz",
        "sub-X_ses-1b_T1w.nii.gz",
    ]
    assert os.path.basename(kw["template"]) == "sub-X_longTemplate1a.1b.mgz"
    # mri_robust_template outputs are grouped under mri_robust_template/<sub>/
    assert os.path.dirname(kw["template"]).endswith(
        os.path.join("mri_robust_template", "sub-X"))
    assert [os.path.basename(p) for p in kw["mapmov"]] == [
        "sub-X_ses-1a_space-longTemplate1a.1b_T1w.nii.gz",
        "sub-X_ses-1b_space-longTemplate1a.1b_T1w.nii.gz",
    ]
    assert [os.path.basename(p) for p in kw["lta"]] == [
        "sub-X_ses-1a_from-native_to-space-longTemplate1a.1b_xfm.lta",
        "sub-X_ses-1b_from-native_to-space-longTemplate1a.1b_xfm.lta",
    ]

    # Step 2: run_samseg_long command with ONE --timepoint per registered image
    cargs = rec["cargs"]
    assert cargs is not None
    assert cargs[0] == "run_samseg_long"
    assert cargs.count("--timepoint") == 2
    tp_files = [cargs[i + 1] for i, a in enumerate(cargs) if a == "--timepoint"]
    assert tp_files == kw["mapmov"]          # timepoints are the step-1 registered images
    assert "--save-warp" in cargs and "--save-mesh" in cargs and "--save-posteriors" in cargs
    out_idx = cargs.index("--output") + 1
    assert cargs[out_idx].endswith(f"samseg_long{os.sep}sub-X{os.sep}")


def test_main_mirrors_nii_extension(one_step, make_bids, tmp_path):
    bids = make_bids("X", ["1a", "1b"], ext=".nii")     # uncompressed inputs
    one_step.main(str(bids), str(tmp_path / "out"), "sub-X")
    kw = one_step._record["mrt_calls"][0]
    assert [os.path.basename(p) for p in kw["mov"]] == [
        "sub-X_ses-1a_T1w.nii",
        "sub-X_ses-1b_T1w.nii",
    ]
    assert [os.path.basename(p) for p in kw["mapmov"]] == [
        "sub-X_ses-1a_space-longTemplate1a.1b_T1w.nii",
        "sub-X_ses-1b_space-longTemplate1a.1b_T1w.nii",
    ]
    # timepoints handed to run_samseg_long are those .nii registered images
    cargs = one_step._record["cargs"]
    tp_files = [cargs[i + 1] for i, a in enumerate(cargs) if a == "--timepoint"]
    assert tp_files == kw["mapmov"]


def test_main_caps_threads_before_samseg(one_step, make_bids, tmp_path):
    bids = make_bids("X", ["1a", "1b"])
    one_step.main(str(bids), str(tmp_path / "out"), "sub-X")
    assert os.environ["OPENBLAS_NUM_THREADS"] == "1"
    assert os.environ["OMP_NUM_THREADS"] == "2"


def test_main_respects_preset_omp(one_step, make_bids, tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    bids = make_bids("X", ["1a", "1b"])
    one_step.main(str(bids), str(tmp_path / "out"), "sub-X")
    assert os.environ["OMP_NUM_THREADS"] == "4"      # not overridden
    assert os.environ["OPENBLAS_NUM_THREADS"] == "1"


@pytest.mark.parametrize("sessions", [[], ["1a"]])
def test_main_skips_when_fewer_than_two_sessions(one_step, make_bids, tmp_path, sessions):
    bids = make_bids("X", sessions) if sessions else (tmp_path / "bids")
    one_step.main(str(bids), str(tmp_path / "out"), "sub-X")
    assert one_step._record["mrt_calls"] == []      # tools never invoked
    assert one_step._record["cargs"] is None
