"""Test fixtures for the SAMSEG-long worker scripts.

The worker scripts import ``niwrap``, ``styxdefs`` and ``freesurfer`` at module
top. Those are the (FreeSurfer-bound) niwrap stack and are not needed to test the
scripts' own logic. So we install lightweight **fakes** into ``sys.modules``
before the workers are imported: the fakes record what the workers ask them to do
(the mri_robust_template kwargs, the run_samseg_long command-line) so tests can
assert on it, without running anything.

The two worker scripts are standalone files (not an installable package) living
in sibling directories, so they are loaded by path with importlib. Each test
module gets a freshly-imported copy wired to fresh fakes.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ONE_STEP = REPO_ROOT / "samseg_long_onestep" / "one_step_samseg_long.py"
TWO_STEP = REPO_ROOT / "samseg_long_twostep" / "two_step_samseg_long.py"


# --------------------------------------------------------------------------- #
# Fakes for the niwrap stack                                                   #
# --------------------------------------------------------------------------- #
class FakeExecution:
    """Records the command passed to run(); input_file() is identity."""

    def __init__(self, record):
        self._record = record

    def input_file(self, f):
        return str(f)

    def run(self, cargs):
        self._record["cargs"] = list(cargs)


class FakeRunner:
    def __init__(self, record):
        self._record = record

    def start_execution(self, metadata):
        self._record["metadata"] = metadata
        return FakeExecution(self._record)


class FakeOutputs:
    def __init__(self, template):
        self.template_output = template


class FakeMetadata:
    """Minimal stand-in for styxdefs.Metadata (a NamedTuple)."""

    def __init__(self, name="fake", container_image_tag="freesurfer/freesurfer:7.4.1"):
        self.name = name
        self.container_image_tag = container_image_tag

    def _replace(self, **kwargs):
        return FakeMetadata(
            name=kwargs.get("name", self.name),
            container_image_tag=kwargs.get("container_image_tag", self.container_image_tag),
        )


def _build_fake_modules():
    """Return (record, modules_dict). `record` captures worker->niwrap calls."""
    record = {
        "use_local_called": False,
        "mrt_calls": [],   # list of kwargs dicts passed to mri_robust_template
        "cargs": None,     # run_samseg_long command-line (list)
        "metadata": None,  # metadata handed to start_execution
    }

    # fake `niwrap`
    niwrap = types.ModuleType("niwrap")

    def use_local():
        record["use_local_called"] = True

    def use_docker(**kwargs):
        record["use_docker_kwargs"] = kwargs

    def use_singularity(**kwargs):
        record["use_singularity_kwargs"] = kwargs

    def use_podman(**kwargs):
        record["use_podman_kwargs"] = kwargs

    def use_auto(**kwargs):
        record["use_auto_kwargs"] = kwargs

    niwrap.use_local = use_local
    niwrap.use_docker = use_docker
    niwrap.use_singularity = use_singularity
    niwrap.use_podman = use_podman
    niwrap.use_auto = use_auto

    # fake `styxdefs`
    styxdefs = types.ModuleType("styxdefs")
    styxdefs.get_global_runner = lambda: FakeRunner(record)

    # fake `freesurfer`
    freesurfer = types.ModuleType("freesurfer")
    freesurfer.MRI_ROBUST_TEMPLATE_METADATA = FakeMetadata(
        "mri_robust_template", container_image_tag="freesurfer/freesurfer:7.4.1"
    )
    freesurfer.RUN_SAMSEG_LONG_METADATA = FakeMetadata(
        "run_samseg_long", container_image_tag="freesurfer/freesurfer:7.4.1"
    )

    def mri_robust_template(**kwargs):
        record["mrt_calls"].append(kwargs)
        return FakeOutputs(kwargs.get("template"))

    freesurfer.mri_robust_template = mri_robust_template

    return record, {
        "niwrap": niwrap,
        "styxdefs": styxdefs,
        "freesurfer": freesurfer,
    }


def _load_worker(path: Path, name: str, fake_modules):
    """Import a worker script by path with the fake niwrap stack in place."""
    saved = {k: sys.modules.get(k) for k in fake_modules}
    sys.modules.update(fake_modules)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        # restore any real modules we shadowed
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return module


@pytest.fixture
def one_step(monkeypatch):
    """Freshly-imported one_step worker + the record of its niwrap calls."""
    record, fake_modules = _build_fake_modules()
    # keep OMP/OPENBLAS env changes from leaking between tests
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    module = _load_worker(ONE_STEP, "one_step_worker", fake_modules)
    module._record = record
    return module


@pytest.fixture
def two_step(monkeypatch):
    """Freshly-imported two_step worker + the record of its niwrap calls."""
    record, fake_modules = _build_fake_modules()
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    module = _load_worker(TWO_STEP, "two_step_worker", fake_modules)
    module._record = record
    return module


# --------------------------------------------------------------------------- #
# Helpers to build fake BIDS / step-1 trees on disk                           #
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_bids(tmp_path):
    """Create sub-<ID>/ses-<S>/anat/sub-<ID>_ses-<S>_T1w<ext> for each session.

    `ext` may be ".nii.gz" (default) or ".nii". `sessions` may also be a list of
    (session, ext) pairs to mix extensions within one participant."""
    def _make(participant, sessions, ext=".nii.gz", root_name="bids"):
        bids = tmp_path / root_name
        for item in sessions:
            s, e = item if isinstance(item, tuple) else (item, ext)
            anat = bids / f"sub-{participant}" / f"ses-{s}" / "anat"
            anat.mkdir(parents=True, exist_ok=True)
            (anat / f"sub-{participant}_ses-{s}_T1w{e}").touch()
        return bids
    return _make


@pytest.fixture
def make_registered(tmp_path):
    """Create step-1 registered images
    sub-<ID>/sub-<ID>_ses-<S>_space-longTemplate<TPL>_T1w<ext>."""
    def _make(participant, sessions, tpl="1a.1b", ext=".nii.gz", root_name="out"):
        out = tmp_path / root_name
        # step 1 writes registered images under <out>/mri_robust_template/<sub>/
        subdir = out / "mri_robust_template" / f"sub-{participant}"
        subdir.mkdir(parents=True, exist_ok=True)
        for s in sessions:
            (subdir / f"sub-{participant}_ses-{s}_space-longTemplate{tpl}_T1w{ext}").touch()
        return out
    return _make
