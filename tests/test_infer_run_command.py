import pytest
from pathlib import Path
from girder_sivacor.errors import FailureCode, SubmissionError
from girder_sivacor.worker_plugin.lib import _infer_run_command, get_project_dir


@pytest.fixture
def submission_dir(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return tmp_path


def test_infer_run_command_unknown_image(submission_dir):
    """
    Test that _infer_run_command rejects an unknown image.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "main.do").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": "unknown/image", "main_file": "main.do"}

    with pytest.raises(
        SubmissionError, match="Cannot infer the entrypoint for submission"
    ) as raised:
        _infer_run_command(submission, stage)
    assert raised.value.code is FailureCode.NO_ENTRYPOINT


def test_infer_run_command_main_file_not_found(submission_dir):
    """
    Test that _infer_run_command reports a missing main file.
    """
    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": "dataeditors/stata", "main_file": "nonexistent.do"}

    with pytest.raises(
        SubmissionError,
        match="Cannot infer run command for submission. No nonexistent.do found.",
    ) as raised:
        _infer_run_command(submission, stage)
    assert raised.value.code is FailureCode.MAIN_FILE_MISSING
    # The filename is the researcher's, so it stays out of the permanent record.
    assert raised.value.detail is None


def test_infer_run_command_multiple_main_files_found(submission_dir):
    """
    Test that _infer_run_command reports an ambiguous main file, and says where.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "main.R").touch()
    (Path(project_dir) / "subdir").mkdir()
    (Path(project_dir) / "subdir" / "main.R").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": "rocker/r-ver", "main_file": "main.R"}

    with pytest.raises(
        SubmissionError,
        match="Cannot infer run command for submission. Multiple main.R files found",
    ) as raised:
        _infer_run_command(submission, stage)
    assert raised.value.code is FailureCode.MAIN_FILE_AMBIGUOUS
    # Which copies they are is the actionable part, and used to be emitted as
    # the literal text "{relative_paths}" -- the second half of the message was
    # not an f-string.
    assert "subdir/main.R" in str(raised.value)
    # Only the count is kept forever.
    assert raised.value.detail == 2


def test_infer_run_command_with_space_in_filename(submission_dir):
    """
    Test that _infer_run_command correctly quotes filenames with spaces.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "my submission.R").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": "rocker/r-ver", "main_file": "my submission.R"}

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    assert command == '"my submission.R"'
    assert sub_dir == ""
    assert home_dir == "/workspace"


def test_infer_run_command_with_renv_lock(submission_dir):
    """
    Test that _infer_run_command correctly handles renv.lock files.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "code").mkdir()
    (Path(project_dir) / "code" / "main.R").touch()
    (Path(project_dir) / "renv.lock").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": "rocker/r-ver", "main_file": "main.R"}

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    assert sub_dir == "."
    assert str(command) == "code/main.R"
    assert home_dir == "/workspace"


def test_infer_run_command_with_renv_lock_in_subdir(submission_dir):
    """
    Test that _infer_run_command correctly handles renv.lock files in subdirectories.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "analysis").mkdir()
    (Path(project_dir) / "analysis" / "renv.lock").touch()
    (Path(project_dir) / "analysis" / "code").mkdir()
    (Path(project_dir) / "analysis" / "code" / "main.R").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": "rocker/r-ver", "main_file": "main.R"}

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    assert sub_dir == "analysis"
    assert str(command) == "code/main.R"
    assert home_dir == "/workspace"


def test_infer_run_command_with_matlab(submission_dir):
    """
    Test that _infer_run_command correctly infers command for MATLAB image.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "script.m").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": "dynare/matlab", "main_file": "script.m"}

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    assert command == "script"
    assert sub_dir == ""
    assert home_dir == "/home/matlab"


JULIA_IMAGE = "ghcr.io/sivacor/julia1.11"
JULIA_ENTRYPOINT = [
    "/usr/local/julia/bin/julia",
    "--startup-file=no",
    "--project=@.",
]


def test_infer_run_command_julia_project_at_root(submission_dir):
    """A Project.toml beside the main file: run from the package root."""
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "main.jl").touch()
    (Path(project_dir) / "Project.toml").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    assert entrypoint == JULIA_ENTRYPOINT
    assert command == "main.jl"
    assert sub_dir == ""
    assert home_dir == "/workspace"


def test_infer_run_command_julia_runs_where_the_main_file_is(submission_dir):
    """The working directory follows the main file, as it does for every stack.

    SIVACOR used to search upward for the governing ``Project.toml`` and start
    the run there instead. ``--project=@.`` makes Julia do that search itself,
    from this working directory, so the environment is the same one without
    SIVACOR holding an opinion about the researcher's layout.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "Project.toml").touch()
    (Path(project_dir) / "code").mkdir()
    (Path(project_dir) / "code" / "main.jl").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    _, command, sub_dir, _ = _infer_run_command(submission, stage)
    assert command == "main.jl"
    assert sub_dir == "code"


def test_infer_run_command_julia_without_project_file(submission_dir):
    """No Project.toml is no longer a refusal.

    Which environment a Julia stage gets is the researcher's business now: a
    setup stage of theirs may have created one, ``Pkg.add`` may be in the script
    itself, or the package may genuinely need nothing. Refusing here would
    refuse all three.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "main.jl").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    entrypoint, command, sub_dir, _ = _infer_run_command(submission, stage)
    assert entrypoint == JULIA_ENTRYPOINT
    assert command == "main.jl"
    assert sub_dir == ""


def test_infer_run_command_julia_ignores_an_renv_lock(submission_dir):
    """An renv.lock in a Julia package is someone else's file.

    The R branch moves the working directory to the lockfile's own directory. On
    a Julia stage that would move the directory ``--project=@.`` searches from,
    which is the one thing that decides its environment.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "rpart").mkdir()
    (Path(project_dir) / "rpart" / "renv.lock").touch()
    (Path(project_dir) / "Project.toml").touch()
    (Path(project_dir) / "main.jl").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    _, command, sub_dir, _ = _infer_run_command(submission, stage)
    assert sub_dir == ""
    assert command == "main.jl"
