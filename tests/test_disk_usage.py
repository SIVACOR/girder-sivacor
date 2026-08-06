"""Peak disk accounting for a run.

``directory_usage`` is measured against ``du`` rather than against a number
written here by hand: the point of using ``st_blocks`` is to agree with what
the filesystem actually charges, and only ``du`` knows that.
"""

import os
import subprocess

import pytest
from girder_sivacor.worker_plugin.lib import directory_usage, workspace_usage


def du_bytes(path):
    """What ``du`` says, in bytes. The reference implementation.

    ``--block-size=1`` and *not* ``-b``: the latter is a synonym for
    ``--apparent-size``, which is the number this code deliberately does not
    report.
    """
    out = subprocess.check_output(["du", "-s", "--block-size=1", path], text=True)
    return int(out.split()[0])


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "big.dta").write_bytes(b"x" * 400_000)
    (tmp_path / "data" / "small.do").write_bytes(b"y" * 12)
    (tmp_path / "nested" / "deeper").mkdir(parents=True)
    (tmp_path / "nested" / "deeper" / "out.csv").write_bytes(b"z" * 90_000)
    return tmp_path


def test_matches_du(tree):
    assert directory_usage(str(tree)) == du_bytes(str(tree))


def test_counts_blocks_not_apparent_size(tree):
    """A 12-byte file still costs a block; st_size would under-report."""
    apparent = sum(
        os.path.getsize(os.path.join(root, name))
        for root, _, names in os.walk(str(tree))
        for name in names
    )
    assert directory_usage(str(tree)) > apparent


def test_sparse_file_costs_what_it_occupies(tmp_path):
    """st_size would call this 1 GiB; it occupies almost nothing."""
    sparse = tmp_path / "sparse.bin"
    with open(sparse, "wb") as fp:
        fp.truncate(1024 ** 3)
    assert os.path.getsize(sparse) == 1024 ** 3
    assert directory_usage(str(tmp_path)) < 1024 ** 2


def test_hardlinks_counted_once(tmp_path):
    (tmp_path / "original").write_bytes(b"x" * 200_000)
    before = directory_usage(str(tmp_path))
    os.link(tmp_path / "original", tmp_path / "same-inode")
    assert directory_usage(str(tmp_path)) == before


def test_symlinks_are_not_followed(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "file").write_bytes(b"x" * 100_000)
    # A symlink pointing back up the tree: following it would not terminate.
    (tmp_path / "loop").symlink_to(tmp_path)
    assert directory_usage(str(tmp_path)) == du_bytes(str(tmp_path))


def test_missing_and_unreadable_paths_are_tolerated(tmp_path):
    """This runs inside the poll loop of a job that is otherwise fine."""
    assert directory_usage(str(tmp_path / "not-there")) == 0
    assert directory_usage(None) == 0
    assert directory_usage() == 0

    unreadable = tmp_path / "locked"
    unreadable.mkdir()
    (unreadable / "file").write_bytes(b"x" * 1000)
    os.chmod(unreadable, 0o000)
    try:
        # Must return, not raise, whatever it could not read.
        assert directory_usage(str(tmp_path)) >= 0
    finally:
        os.chmod(unreadable, 0o755)


def test_sums_several_paths(tmp_path):
    for name in ("workspace", "tmp"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f").write_bytes(b"x" * 100_000)
    combined = directory_usage(str(tmp_path / "workspace"), str(tmp_path / "tmp"))
    assert combined == du_bytes(str(tmp_path / "workspace")) + du_bytes(
        str(tmp_path / "tmp")
    )


def test_workspace_usage_covers_both_scratch_dirs(tmp_path):
    workspace, tmp_dir = tmp_path / "workspace-1", tmp_path / "tmp-1"
    workspace.mkdir()
    tmp_dir.mkdir()
    (workspace / "package.dta").write_bytes(b"x" * 300_000)
    (tmp_dir / "stata.lic").write_bytes(b"y" * 500)

    submission = {"workspace_dir": str(workspace), "tmp_dir": str(tmp_dir)}
    assert workspace_usage(submission) == du_bytes(str(workspace)) + du_bytes(
        str(tmp_dir)
    )

    # A submission that never got as far as creating them reports zero, not a
    # crash -- prepare_submission can fail before either exists.
    assert workspace_usage({}) == 0
