import pytest
from pathlib import Path
from girder_sivacor.worker_plugin.lib import _infer_run_command, get_project_dir


@pytest.fixture
def submission_dir(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return tmp_path


def test_infer_run_command_unknown_image(submission_dir):
    """
    Test that _infer_run_command raises a ValueError for an unknown image.
    """
    project_dir = get_project_dir({"temp_dir": str(submission_dir)})
    (Path(project_dir) / "main.do").touch()

    submission = {"temp_dir": str(submission_dir)}
    stage = {"image_name": "unknown/image", "main_file": "main.do"}

    with pytest.raises(ValueError, match="Cannot infer the entrypoint for submission"):
        _infer_run_command(submission, stage)


def test_infer_run_command_main_file_not_found(submission_dir):
    """
    Test that _infer_run_command raises a ValueError when the main file is not found.
    """
    submission = {"temp_dir": str(submission_dir)}
    stage = {"image_name": "dataeditors/stata", "main_file": "nonexistent.do"}

    with pytest.raises(
        ValueError,
        match="Cannot infer run command for submission. No nonexistent.do found.",
    ):
        _infer_run_command(submission, stage)


def test_infer_run_command_multiple_main_files_found(submission_dir):
    """
    Test that _infer_run_command raises a ValueError when multiple main files are found.
    """
    project_dir = get_project_dir({"temp_dir": str(submission_dir)})
    (Path(project_dir) / "main.R").touch()
    (Path(project_dir) / "subdir").mkdir()
    (Path(project_dir) / "subdir" / "main.R").touch()

    submission = {"temp_dir": str(submission_dir)}
    stage = {"image_name": "rocker/r-ver", "main_file": "main.R"}

    with pytest.raises(
        ValueError,
        match="Cannot infer run command for submission. Multiple main.R files found",
    ):
        _infer_run_command(submission, stage)


def test_infer_run_command_with_space_in_filename(submission_dir):
    """
    Test that _infer_run_command correctly quotes filenames with spaces.
    """
    project_dir = get_project_dir({"temp_dir": str(submission_dir)})
    (Path(project_dir) / "my submission.R").touch()

    submission = {"temp_dir": str(submission_dir)}
    stage = {"image_name": "rocker/r-ver", "main_file": "my submission.R"}

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    assert command == '"my submission.R"'
    assert sub_dir == ""
    assert home_dir == str(submission_dir)


def test_infer_run_command_with_renv_lock(submission_dir):
    """
    Test that _infer_run_command correctly handles renv.lock files.
    """
    project_dir = get_project_dir({"temp_dir": str(submission_dir)})
    (Path(project_dir) / "code").mkdir()
    (Path(project_dir) / "code" / "main.R").touch()
    (Path(project_dir) / "renv.lock").touch()

    submission = {"temp_dir": str(submission_dir)}
    stage = {"image_name": "rocker/r-ver", "main_file": "main.R"}

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    assert sub_dir == "."
    assert str(command) == "code/main.R"
    assert home_dir == str(submission_dir)


def test_infer_run_command_with_renv_lock_in_subdir(submission_dir):
    """
    Test that _infer_run_command correctly handles renv.lock files in subdirectories.
    """
    project_dir = get_project_dir({"temp_dir": str(submission_dir)})
    (Path(project_dir) / "analysis").mkdir()
    (Path(project_dir) / "analysis" / "renv.lock").touch()
    (Path(project_dir) / "analysis" / "code").mkdir()
    (Path(project_dir) / "analysis" / "code" / "main.R").touch()

    submission = {"temp_dir": str(submission_dir)}
    stage = {"image_name": "rocker/r-ver", "main_file": "main.R"}

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    assert sub_dir == "analysis"
    assert str(command) == "code/main.R"
    assert home_dir == str(submission_dir)


def test_infer_run_command_with_matlab(submission_dir):
    """
    Test that _infer_run_command correctly infers command for MATLAB image.
    """
    project_dir = get_project_dir({"temp_dir": str(submission_dir)})
    (Path(project_dir) / "script.m").touch()

    submission = {"temp_dir": str(submission_dir)}
    stage = {"image_name": "dynare/matlab", "main_file": "script.m"}

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    assert command == "script"
    assert sub_dir == ""
    assert home_dir == "/home/matlab"
