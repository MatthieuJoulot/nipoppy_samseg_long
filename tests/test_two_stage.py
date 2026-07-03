"""Tests for two_shot/two_stage_samseg_long.py (TOOL-selected two-step worker)."""

import os

import pytest


# --------------------------------------------------------------------------- #
# Shared pure helpers (duplicated from one_shot; test this copy too)           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "labels, expected",
    [
        (["10", "2", "1"], ["1", "2", "10"]),
        (["2", "1a", "1b"], ["1a", "1b", "2"]),
        (["m06", "2", "1a"], ["1a", "2", "m06"]),          # mixed types must not crash
        (["010", "001", "002"], ["001", "002", "010"]),
    ],
)
def test_natural_key_sorting(two_stage, labels, expected):
    assert sorted(labels, key=two_stage.natural_key) == expected


@pytest.mark.parametrize(
    "given, expected",
    [("MIRIAD188", "sub-MIRIAD188"), ("sub-MIRIAD188", "sub-MIRIAD188")],
)
def test_bids_subject_prefix_tolerance(two_stage, given, expected):
    assert two_stage.bids_subject(given) == expected


def test_discover_bids_sessions_sorted(two_stage, make_bids):
    bids = make_bids("X", ["2", "1b", "1a"])
    assert two_stage.discover_bids_sessions(bids, "sub-X") == ["1a", "1b", "2"]


# --------------------------------------------------------------------------- #
# discover_registered (stage-2 input discovery)                               #
# --------------------------------------------------------------------------- #
def test_discover_registered_sorted_paths(two_stage, make_registered):
    out = make_registered("X", ["2", "1a", "1b"], tpl="1a.1b.2")
    got = two_stage.discover_registered(out, "sub-X")
    assert [os.path.basename(p) for p in got] == [
        "sub-X_ses-1a_space-longTemplate1a.1b.2_T1w.nii.gz",
        "sub-X_ses-1b_space-longTemplate1a.1b.2_T1w.nii.gz",
        "sub-X_ses-2_space-longTemplate1a.1b.2_T1w.nii.gz",
    ]


def test_discover_registered_empty_when_missing(two_stage, tmp_path):
    assert two_stage.discover_registered(tmp_path / "out", "sub-X") == []


# --------------------------------------------------------------------------- #
# Stage 1: run_mri_robust_template                                            #
# --------------------------------------------------------------------------- #
def test_stage1_builds_expected_names(two_stage, make_bids, tmp_path):
    bids = make_bids("X", ["1a", "1b"])
    two_stage.run_mri_robust_template(str(bids), str(tmp_path / "out"), "X")

    rec = two_stage._record
    assert len(rec["mrt_calls"]) == 1
    kw = rec["mrt_calls"][0]
    assert os.path.basename(kw["template"]) == "sub-X_longTemplate1a.1b.mgz"
    assert [os.path.basename(p) for p in kw["mapmov"]] == [
        "sub-X_ses-1a_space-longTemplate1a.1b_T1w.nii.gz",
        "sub-X_ses-1b_space-longTemplate1a.1b_T1w.nii.gz",
    ]
    assert kw["satit"] is True
    # stage 1 does not touch run_samseg_long
    assert rec["cargs"] is None


@pytest.mark.parametrize("sessions", [[], ["1a"]])
def test_stage1_skips_when_fewer_than_two(two_stage, make_bids, tmp_path, sessions):
    bids = make_bids("X", sessions) if sessions else (tmp_path / "bids")
    two_stage.run_mri_robust_template(str(bids), str(tmp_path / "out"), "X")
    assert two_stage._record["mrt_calls"] == []


# --------------------------------------------------------------------------- #
# Stage 2: run_run_samseg_long                                               #
# --------------------------------------------------------------------------- #
def test_stage2_repeated_timepoint_and_thread_cap(two_stage, make_registered, tmp_path):
    in_dir = make_registered("X", ["1a", "1b"], tpl="1a.1b")
    two_stage.run_run_samseg_long(str(in_dir), str(tmp_path / "out"), "X")

    cargs = two_stage._record["cargs"]
    assert cargs is not None
    assert cargs[0] == "run_samseg_long"
    assert cargs.count("--timepoint") == 2                     # one per timepoint (the bug guard)
    assert "--save-warp" in cargs and "--save-mesh" in cargs and "--save-posteriors" in cargs
    out_idx = cargs.index("--output") + 1
    assert cargs[out_idx].endswith(f"sub-X{os.sep}samseg_long{os.sep}")
    assert os.environ["OPENBLAS_NUM_THREADS"] == "1"           # segfault fix guard
    assert os.environ["OMP_NUM_THREADS"] == "2"


@pytest.mark.parametrize("sessions", [[], ["1a"]])
def test_stage2_skips_when_fewer_than_two(two_stage, make_registered, tmp_path, sessions):
    in_dir = make_registered("X", sessions) if sessions else (tmp_path / "out")
    two_stage.run_run_samseg_long(str(in_dir), str(tmp_path / "out2"), "X")
    assert two_stage._record["cargs"] is None


# --------------------------------------------------------------------------- #
# main() dispatch                                                             #
# --------------------------------------------------------------------------- #
def test_main_dispatches_to_correct_tool(two_stage, monkeypatch):
    calls = []
    monkeypatch.setattr(two_stage, "run_mri_robust_template",
                        lambda *a: calls.append(("mrt", a)))
    monkeypatch.setattr(two_stage, "run_run_samseg_long",
                        lambda *a: calls.append(("samseg", a)))

    two_stage.main("mri_robust_template", "in", "out", "X")
    two_stage.main("run_samseg_long", "in", "out", "X")

    assert [c[0] for c in calls] == ["mrt", "samseg"]
    assert calls[0][1] == ("in", "out", "X")


def test_main_rejects_unknown_tool(two_stage):
    with pytest.raises(SystemExit):
        two_stage.main("bogus_tool", "in", "out", "X")
