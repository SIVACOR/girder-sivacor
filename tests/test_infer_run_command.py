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
    "--project=.",
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


def test_infer_run_command_julia_main_below_project(submission_dir):
    """The working directory is the Project.toml's, not the main file's.

    ``--project=.`` resolves against the working directory, so the run has to
    start where the environment is declared -- which means the command carries
    the path down to the main file rather than the working directory following
    it.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "Project.toml").touch()
    (Path(project_dir) / "code").mkdir()
    (Path(project_dir) / "code" / "main.jl").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    _, command, sub_dir, _ = _infer_run_command(submission, stage)
    assert command == "code/main.jl"
    assert sub_dir == ""


def test_infer_run_command_julia_project_beside_main_in_subdir(submission_dir):
    """A self-contained subdirectory: both move down together."""
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "analysis").mkdir()
    (Path(project_dir) / "analysis" / "Project.toml").touch()
    (Path(project_dir) / "analysis" / "main.jl").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    _, command, sub_dir, _ = _infer_run_command(submission, stage)
    assert command == "main.jl"
    assert sub_dir == "analysis"


def test_infer_run_command_julia_several_project_files_is_not_ambiguous(submission_dir):
    """Several Project.toml files is the normal Julia layout, not an error.

    ``docs/Project.toml`` and ``test/Project.toml`` are documented conventions --
    DataFrames.jl and GLM.jl ship two apiece, CSV.jl ships four. Treating them
    the way duplicate *main* files are treated would refuse packages laid out
    exactly as Julia tells people to lay them out. The nearest one above the
    main file wins, which is also what ``--project=@.`` would have chosen.
    """
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "Project.toml").touch()
    for extra in ("docs", "test"):
        (Path(project_dir) / extra).mkdir()
        (Path(project_dir) / extra / "Project.toml").touch()
    (Path(project_dir) / "src").mkdir()
    (Path(project_dir) / "src" / "main.jl").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    _, command, sub_dir, _ = _infer_run_command(submission, stage)
    assert sub_dir == ""
    assert command == "src/main.jl"


def test_infer_run_command_julia_nearest_project_wins(submission_dir):
    """With one above and one beside, the nearer one is the environment."""
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "Project.toml").touch()
    (Path(project_dir) / "paper").mkdir()
    (Path(project_dir) / "paper" / "Project.toml").touch()
    (Path(project_dir) / "paper" / "main.jl").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    _, command, sub_dir, _ = _infer_run_command(submission, stage)
    assert sub_dir == "paper"
    assert command == "main.jl"


def test_infer_run_command_julia_without_project_file(submission_dir):
    """No Project.toml at all is a refusal, and the message has to be actionable."""
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "main.jl").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    with pytest.raises(SubmissionError, match="no Project.toml") as raised:
        _infer_run_command(submission, stage)
    assert raised.value.code is FailureCode.PROJECT_FILE_MISSING
    # The researcher's filename may appear in the message, which goes to the job
    # log -- but never in the permanent record.
    assert raised.value.detail is None


def test_infer_run_command_julia_sibling_project_does_not_count(submission_dir):
    """A Project.toml in a sibling directory is not above the main file."""
    project_dir = get_project_dir({"workspace_dir": str(submission_dir)})
    (Path(project_dir) / "other").mkdir()
    (Path(project_dir) / "other" / "Project.toml").touch()
    (Path(project_dir) / "code").mkdir()
    (Path(project_dir) / "code" / "main.jl").touch()

    submission = {"workspace_dir": str(submission_dir)}
    stage = {"image_name": JULIA_IMAGE, "main_file": "main.jl"}

    with pytest.raises(SubmissionError, match="no Project.toml") as raised:
        _infer_run_command(submission, stage)
    assert raised.value.code is FailureCode.PROJECT_FILE_MISSING
