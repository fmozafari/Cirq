# Copyright 2020 The Cirq Developers
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ========================== ISOLATED NOTEBOOK TESTS ============================================
#
# In these tests are only changed notebooks are tested. It is assumed that notebooks install cirq
# conditionally if they can't import cirq. This installation path is the main focus and it is
# excercised in an isolated virtual environment for each notebook. This is also the path that is
# tested in the devsite workflows, these tests meant to provide earlier feedback.

import functools
import glob
import os
import subprocess
import sys
import warnings
from typing import Set

import pytest
from filelock import FileLock

from dev_tools import shell_tools
from dev_tools.env_tools import create_virtual_env

# these notebooks rely on features that are not released yet
# after every release we should raise a PR and empty out this list
# note that these notebooks are still tested in dev_tools/notebook_test.py
NOTEBOOKS_DEPENDING_ON_UNRELEASED_FEATURES = [
    'docs/characterization/*.ipynb',
]

# By default all notebooks should be tested, however, this list contains exceptions to the rule
# please always add a reason for skipping.
SKIP_NOTEBOOKS = [
    # skipping vendor notebooks as we don't have auth sorted out
    "**/google/*.ipynb",
    "**/pasqal/*.ipynb",
    "**/aqt/*.ipynb",
    # skipping fidelity estimation due to
    # https://github.com/quantumlib/Cirq/issues/3502
    "examples/*fidelity*",
] + NOTEBOOKS_DEPENDING_ON_UNRELEASED_FEATURES

# As these notebooks run in an isolated env, we want to minimize dependencies that are
# installed. We assume colab packages (feel free to add dependencies here that appear in colab, as
# needed by the notebooks) exist. These packages are installed into a base environment as a starting
# point, that is then cloned to a separate folder for each test.
PACKAGES = [
    # for running the notebooks
    "papermill",
    "jupyter",
    "virtualenv-clone",
    # assumed to be part of colab
    "seaborn~=0.11.1",
    # https://github.com/nteract/papermill/issues/519
    'ipykernel==5.3.4',
]


# TODO(3577): extract these out to common utilities when we rewrite bash scripts in python
def _find_base_revision():
    for rev in ['upstream/master', 'origin/master', 'master']:
        try:
            result = subprocess.run(
                f'git cat-file -t {rev}'.split(), universal_newlines=True, capture_output=True
            )
            if result.stdout == "commit\n":
                return rev
        except subprocess.CalledProcessError as e:
            print(e)
    raise ValueError("Can't find a base revision to compare the files with.")


def _list_changed_notebooks() -> Set[str]:
    rev = _find_base_revision()
    output = subprocess.check_output(f'git diff --name-only {rev}'.split())
    return set(l for l in output.decode('utf-8').splitlines() if l.endswith(".ipynb"))


def _tested_notebooks():
    changed_notebooks = set()

    # It would be nicer if we could somehow automatically skip the execution of this completely,
    # however, in order to be able to rely on parallel pytest (xdist) we need parametrization to
    # work, thus this will be executed during the collection phase even when the notebook tests
    # are not included (the "-m slow" flag is not passed to pytest). So, in order to not break the
    # complete test collection phase in other tests where there is no git history (fetch-depth:1),
    # we'll just swallow the error here with a warning.

    try:
        changed_notebooks = _list_changed_notebooks()
    except ValueError as e:
        warnings.warn(
            f"No changed notebooks are tested "
            f"(this is expected in non-notebook tests in CI): {e}"
        )

    skipped_notebooks = functools.reduce(
        lambda a, b: a.union(b), list(set(glob.glob(g, recursive=True)) for g in SKIP_NOTEBOOKS)
    )

    # sorted is important otherwise pytest-xdist will complain that
    # the workers have different parametrization:
    # https://github.com/pytest-dev/pytest-xdist/issues/432
    return sorted(os.path.abspath(n) for n in changed_notebooks.difference(skipped_notebooks))


@pytest.mark.slow
@pytest.fixture(scope="session")
def base_env(tmp_path_factory, worker_id):
    # get the temp directory shared by all workers
    root_tmp_dir = tmp_path_factory.getbasetemp().parent.parent
    proto_dir = root_tmp_dir / "proto_dir"
    with FileLock(str(proto_dir) + ".lock"):
        if proto_dir.is_dir():
            print(f"{worker_id} returning as {proto_dir} is a dir!")
            print(
                f"If all the notebooks are failing, the test framework might "
                f"have left this directory around. Try 'rm -rf {proto_dir}'"
            )
        else:
            print(f"{worker_id} creating stuff...")
            _create_base_env(proto_dir)

    return root_tmp_dir, proto_dir


def _create_base_env(proto_dir):
    create_virtual_env(str(proto_dir), [], sys.executable, True)
    pip_path = str(proto_dir / "bin" / "pip")
    shell_tools.run_cmd(pip_path, "install", *PACKAGES)


@pytest.mark.slow
@pytest.mark.parametrize("notebook_path", _tested_notebooks())
def test_notebooks_against_released_cirq(notebook_path, base_env):
    """Tests the notebooks in isolated virtual environments."""
    notebook_file = os.path.basename(notebook_path)
    notebook_rel_dir = os.path.dirname(os.path.relpath(notebook_path, "."))
    out_path = f"out/{notebook_rel_dir}/{notebook_file[:-6]}.out.ipynb"
    tmpdir, proto_dir = base_env

    notebook_file = os.path.basename(notebook_path)
    dir_name = notebook_file.rstrip(".ipynb")

    notebook_env = os.path.join(tmpdir, f"{dir_name}")
    cmd = f"""
mkdir -p out/{notebook_rel_dir}
{proto_dir}/bin/virtualenv-clone {proto_dir} {notebook_env}
cd {notebook_env}
. ./bin/activate
papermill {notebook_path} {os.getcwd()}/{out_path}"""
    _, stderr, status = shell_tools.run_shell(
        cmd=cmd,
        log_run_to_stderr=False,
        raise_on_fail=False,
        out=shell_tools.TeeCapture(),
        err=shell_tools.TeeCapture(),
    )

    if status != 0:
        print(stderr)
        pytest.fail(
            f"Notebook failure: {notebook_file}, please see {out_path} for the output "
            f"notebook (in Github Actions, you can download it from the workflow artifact"
            f" 'notebook-outputs')"
        )
