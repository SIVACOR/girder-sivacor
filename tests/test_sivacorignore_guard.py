"""A ``.sivacorignore`` may not delete a stage's main file, or itself.

Pruning is the last step of the pipeline, so a rule that matches either would
otherwise be found after the whole run had been paid for. The worker checks
the extracted package at the end of ``create_workspace``, before any image is
pulled, and once more right before pruning, in case a stage wrote a
``.sivacorignore`` of its own.
"""

import hashlib
import os
import pathlib
import shutil
import socket
import subprocess
import threading
import zipfile

import pytest
from girder.models.file import File
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatusOk

from girder_sivacor import sivacorignore
from girder_sivacor.errors import FailureCode, SubmissionError
from girder_sivacor.models.execution_record import ExecutionRecord
from girder_sivacor.sivacorignore import (
    IGNORE_FILE_READ_LIMIT,
    Rules,
    choose_ignore_file,
    find_violations,
)
from girder_sivacor.usage import USER_PAYS
from girder_sivacor.worker_plugin import run_submission
from girder_sivacor.worker_plugin.run_submission import (
    _prune,
    protected_files_check,
    prune_rules,
    read_ignore_file,
)

from .conftest import get_submission_folder, submit_sivacor_job, upload_test_file

STAGES = [
    {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
]


def _violations(paths, ignore_text, main_files=("main.R",)):
    source = choose_ignore_file(paths)
    rules = Rules.from_text(source, ignore_text)
    found = {name: [p for p in paths if p.rpartition("/")[2] == name] for name in main_files}
    return find_violations(rules, [p[0] for p in found.values() if len(p) == 1])


# --- the rules themselves --------------------------------------------------


@pytest.mark.parametrize(
    "ignore_text",
    [
        "*.R\n",  # the usual mistake: meant generated scripts
        "main.R\n",
        "code/\n",  # a directory takes everything in it
        "code/\n!code/main.R\n",  # and a negation cannot get it back
        "*\n",
    ],
)
def test_a_rule_that_deletes_the_main_file_is_caught(ignore_text):
    paths = ["code/main.R", "data/raw.csv", ".sivacorignore"]
    violations = _violations(paths, ignore_text)
    assert "code/main.R" in [v.path for v in violations]


@pytest.mark.parametrize("ignore_text", [".*\n", ".sivacorignore\n", "*\n"])
def test_a_rule_that_deletes_the_ignore_file_is_caught(ignore_text):
    paths = ["main.R", ".sivacorignore"]
    violations = _violations(paths, ignore_text)
    assert [v.path for v in violations if v.what == "ignore file"] == [
        ".sivacorignore"
    ]


@pytest.mark.parametrize(
    "ignore_text",
    [
        "data/raw/\n*.tmp\n",
        "*.R\n!main.R\n",  # a negation does rescue a file matched by name
        "# main.R\n",  # a comment is not a rule
    ],
)
def test_a_rule_that_spares_both_is_allowed(ignore_text):
    paths = ["main.R", "data/raw/secret.csv", "out.tmp", ".sivacorignore"]
    assert _violations(paths, ignore_text) == []


def test_files_outside_the_ignore_files_directory_are_never_pruned():
    """Patterns are relative to the .sivacorignore, as the pruning walk is."""
    paths = ["main.R", "pkg/.sivacorignore", "pkg/helper.R"]
    assert _violations(paths, "*.R\n") == []
    assert _violations(paths, "*.R\n", main_files=("helper.R",)) != []


def test_a_duplicated_main_file_is_left_to_the_ambiguity_check():
    """The worker fails this as MAIN_FILE_AMBIGUOUS before running anything.

    Refusing it here would blame the ignore rules for a copy the researcher
    meant to drop, which names the wrong problem.
    """
    paths = ["main.R", "old/main.R", ".sivacorignore"]
    assert _violations(paths, "old/\n") == []


def test_only_the_governing_ignore_file_is_protected():
    """Another .sivacorignore is ignored by the worker and records nothing.

    A folder kept only as an archive is the common case, and refusing it
    would push the researcher into ignoring the folder's contents one by one.
    """
    paths = ["main.R", ".sivacorignore", "old/.sivacorignore", "old/analysis_v1.R"]
    assert _violations(paths, "old/\n") == []


def test_the_shallowest_ignore_file_governs():
    """Independent of the order the filesystem lists them in."""
    paths = ["z/.sivacorignore", "a/b/.sivacorignore", ".sivacorignore"]
    assert choose_ignore_file(paths) == ".sivacorignore"
    assert choose_ignore_file(reversed(paths[:2])) == "z/.sivacorignore"
    assert choose_ignore_file(["main.R"]) is None


def test_the_message_names_the_file_and_the_rule():
    [violation] = _violations(["main.R", ".sivacorignore"], "data/\n*.R\n")
    assert str(violation) == (
        "main.R (main file) is matched by '*.R' on line 2 of .sivacorignore"
    )


# --- the worker's check ---------------------------------------------------


def _write(root, files):
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)


def test_the_worker_refuses_an_extracted_package_that_prunes_its_main_file(tmp_path):
    _write(tmp_path, {"proj/main.R": b"", "proj/.sivacorignore": b"*.R\n"})
    with pytest.raises(SubmissionError) as raised:
        protected_files_check(tmp_path, STAGES)
    assert raised.value.code is FailureCode.IGNORE_FILE_INVALID
    assert "proj/main.R" in str(raised.value)


def test_files_a_run_created_are_not_protected_at_prune_time(tmp_path):
    """Only the main file the stage resolved, not every file of that name.

    An R package restored into renv/library can ship a demo/run.R of its own.
    Matching by name after the run would fail a submission that succeeded,
    over a file the researcher never wrote -- and the rule pruning it is the
    one the R docs tell them to use.
    """
    _write(
        tmp_path,
        {
            "run.R": b"renv::restore()\n",
            ".sivacorignore": b"renv/library/\n",
            "renv/library/R-4.4/x86_64/somepkg/demo/run.R": b"",
        },
    )
    stages = [{"image_name": "rocker/r-ver", "image_tag": "4.4.1", "main_file": "run.R"}]
    pin = protected_files_check(tmp_path, stages)
    rules = prune_rules(tmp_path, resolved=["run.R"], pinned=pin)
    assert rules.pruned_by("renv/library", is_dir=True)


def test_the_resolved_main_file_is_still_protected_at_prune_time(tmp_path):
    _write(tmp_path, {"code/run.R": b"", ".sivacorignore": b"code/\n"})
    pin = {"path": ".sivacorignore", "sha256": hashlib.sha256(b"code/\n").hexdigest()}
    with pytest.raises(SubmissionError) as raised:
        prune_rules(tmp_path, resolved=["code/run.R"], pinned=pin)
    assert raised.value.code is FailureCode.IGNORE_FILE_INVALID
    assert "code/run.R (main file)" in str(raised.value)


def test_the_worker_pins_the_rules_it_will_prune_with(tmp_path):
    _write(tmp_path, {"proj/main.R": b"", "proj/.sivacorignore": b"data/\n"})
    pin = protected_files_check(tmp_path, STAGES)
    assert pin == {
        "path": "proj/.sivacorignore",
        "sha256": hashlib.sha256(b"data/\n").hexdigest(),
    }
    rules = prune_rules(tmp_path, resolved=["proj/main.R"], pinned=pin)
    assert (rules.source, rules.base) == ("proj/.sivacorignore", "proj")
    assert rules.pruned_by("proj/data", is_dir=True)


# --- the run cannot change the rules ----------------------------------------
#
# Each of these used to drop the researcher's rules without a word: pruning
# fell back to the defaults (or to the run's own file) and restricted/ was
# published in the signed package.


def _changed(project, pinned):
    with pytest.raises(SubmissionError) as raised:
        prune_rules(project, resolved=["main.R"], pinned=pinned)
    assert raised.value.code is FailureCode.IGNORE_FILE_INVALID
    return str(raised.value)


def _uploaded(project):
    _write(
        project,
        {
            "main.R": b"",
            ".sivacorignore": b"restricted/\n",
            "restricted/data.csv": b"1,2\n",
        },
    )
    return protected_files_check(project, STAGES)


def test_an_unchanged_ignore_file_is_pruned_with(tmp_path):
    pin = _uploaded(tmp_path)
    rules = prune_rules(tmp_path, resolved=["main.R"], pinned=pin)
    assert _prune(tmp_path, rules) == ["restricted/"]
    assert not (tmp_path / "restricted").exists()


def test_a_deleted_ignore_file_is_refused(tmp_path):
    pin = _uploaded(tmp_path)
    (tmp_path / ".sivacorignore").unlink()
    assert ".sivacorignore was removed, moved or replaced" in _changed(tmp_path, pin)


def test_an_ignore_file_replaced_by_a_link_to_a_directory_is_refused(tmp_path):
    """os.walk lists the link as a directory, so no ignore file is found."""
    pin = _uploaded(tmp_path)
    (tmp_path / ".sivacorignore").unlink()
    (tmp_path / "empty").mkdir()
    (tmp_path / ".sivacorignore").symlink_to(tmp_path / "empty")
    _changed(tmp_path, pin)


def test_an_edited_ignore_file_is_refused(tmp_path):
    pin = _uploaded(tmp_path)
    (tmp_path / ".sivacorignore").write_bytes(b"# nothing\n")
    assert ".sivacorignore was modified" in _changed(tmp_path, pin)


def test_a_shallower_ignore_file_written_by_the_run_is_refused(tmp_path):
    _uploaded(tmp_path / "proj")
    pin = protected_files_check(tmp_path, STAGES)
    assert pin["path"] == "proj/.sivacorignore"
    (tmp_path / ".sivacorignore").write_bytes(b"")
    message = _changed(tmp_path, pin)
    assert "proj/.sivacorignore was removed, moved or replaced" in message
    assert "and .sivacorignore would now take its place" in message


def test_an_ignore_file_created_by_the_run_is_refused(tmp_path):
    _write(tmp_path, {"main.R": b""})
    pin = protected_files_check(tmp_path, STAGES)
    assert pin is None
    (tmp_path / ".sivacorignore").write_bytes(b"*.R\n")
    assert "the run created .sivacorignore" in _changed(tmp_path, pin)


def test_a_dependencys_own_ignore_file_is_not_the_researchers(tmp_path):
    """renv/library/pkg/ or a cloned repository can ship a .sivacorignore.
    Only one beside or above the code that ran could be the researcher's."""
    _write(tmp_path, {"main.R": b""})
    pin = protected_files_check(tmp_path, STAGES)
    _write(
        tmp_path,
        {
            "renv/library/pkg/.sivacorignore": b"*\n",
            "deps/clone/.sivacorignore": b"*\n",
        },
    )
    rules = prune_rules(tmp_path, resolved=["main.R"], pinned=pin)
    assert rules.source is None


def test_only_files_beside_or_above_the_code_can_outrank_the_pin(tmp_path):
    """lib/ ties proj/ on depth and wins on name, but nothing in lib/ ran."""
    _uploaded(tmp_path / "proj")
    pin = protected_files_check(tmp_path, STAGES)
    _write(tmp_path, {"lib/.sivacorignore": b""})
    rules = prune_rules(tmp_path, resolved=["proj/main.R"], pinned=pin)
    assert rules.source == "proj/.sivacorignore"

    _write(tmp_path, {".sivacorignore": b""})  # above the code: it counts
    with pytest.raises(SubmissionError) as raised:
        prune_rules(tmp_path, resolved=["proj/main.R"], pinned=pin)
    assert raised.value.code is FailureCode.IGNORE_FILE_INVALID


def test_prune_does_not_walk_the_tree_to_find_its_rules(tmp_path, monkeypatch):
    """The directories a .sivacorignore could count in are listed directly;
    _prune is the only walk."""
    pin = _uploaded(tmp_path)

    def no_walk(*args, **kwargs):
        raise AssertionError("prune_rules walked the tree")

    monkeypatch.setattr(os, "walk", no_walk)
    assert prune_rules(tmp_path, resolved=["main.R"], pinned=pin).source


def test_an_ignore_file_beyond_a_symlinked_directory_is_not_read(tmp_path):
    """A stage can replace proj/ with a link out of the workspace; the walk
    would not descend into it, so neither does the direct lookup."""
    outside = tmp_path / "outside"
    _write(outside, {".sivacorignore": b"secret\n", "main.R": b""})
    project = tmp_path / "project"
    project.mkdir()
    (project / "proj").symlink_to(outside, target_is_directory=True)
    assert prune_rules(project, resolved=["proj/main.R"], pinned=None).source is None


# --- the deletion is the check's own rule -----------------------------------


def test_the_walk_judges_each_entry_once(tmp_path, monkeypatch):
    """The walk never enters a removed directory, so no entry has an excluded
    ancestor; asking pruned_by re-judged every ancestor of every file."""
    deep = "/".join(f"d{i}" for i in range(10))
    _write(tmp_path, {f"{deep}/x.csv": b"", ".sivacorignore": b"*.csv\n"})
    rules = Rules.from_text(".sivacorignore", "*.csv\n")
    judged = []
    real = Rules._excluded_by
    monkeypatch.setattr(
        Rules,
        "_excluded_by",
        lambda self, view, is_dir: judged.append(view) or real(self, view, is_dir),
    )
    assert _prune(tmp_path, rules) == [f"{deep}/x.csv"]
    assert len(judged) == len(set(judged)) == 12  # 10 dirs, 2 files


@pytest.mark.parametrize(
    "pattern, removed",
    [(b"restricted\n", ["restricted"]), (b"restricted/\n", [])],
    ids=["matched_as_a_file", "not_a_directory"],
)
def test_a_link_to_a_directory_is_judged_as_a_file(tmp_path, pattern, removed):
    """As git judges it: a dir-only pattern does not match the link. When it
    is matched, it is unlinked -- rmtree refuses a link, and with errors
    ignored the link stayed in the package while the log said it was pruned
    -- and never followed."""
    outside = tmp_path / "outside"
    _write(outside, {"keep.csv": b"1\n"})
    project = tmp_path / "project"
    _write(project, {"main.R": b"", ".sivacorignore": pattern})
    (project / "restricted").symlink_to(outside, target_is_directory=True)

    rules = prune_rules(project, ["main.R"], protected_files_check(project, STAGES))
    assert _prune(project, rules) == removed
    assert os.path.lexists(project / "restricted") == (not removed)
    assert (outside / "keep.csv").exists()  # never followed


def test_pruning_deletes_exactly_what_pruned_by_says(tmp_path):
    files = {
        "main.R": b"",
        ".sivacorignore": b"out/\n*.log\n!keep.log\nsub/x/\n",
        "out/a.csv": b"",
        "out/keep.log": b"",  # inside an ignored directory: the negation loses
        "keep.log": b"",
        "run.log": b"",
        "sub/x/y.csv": b"",
        "sub/z.csv": b"",
    }
    _write(tmp_path, files)
    rules = prune_rules(tmp_path, ["main.R"], protected_files_check(tmp_path, STAGES))
    expected = {name for name in files if rules.pruned_by(name)}
    _prune(tmp_path, rules)
    left = {
        (pathlib.Path(root) / f).relative_to(tmp_path).as_posix()
        for root, _, names in os.walk(tmp_path)
        for f in names
    }
    assert left == set(files) - expected
    assert expected == {"out/a.csv", "out/keep.log", "run.log", "sub/x/y.csv"}


# --- pruning, after a real run ---------------------------------------------
#
# The tests above call the checks directly. These go through
# the whole chain, because what they protect is a hand-off: the main-file path
# _infer_run_command resolves in execute_workflow has to reach prune_workspace
# several celery steps later.

R_STAGES = [
    {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
]


def _run_r(server, user, uploads_folder, tmp_path, files, stages=R_STAGES):
    archive = tmp_path / "package.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for name, data in files.items():
            package.writestr(name, data)
    file = upload_test_file(uploads_folder, user, os.fspath(archive))
    resp = submit_sivacor_job(server, user, file, stages)
    assertStatusOk(resp)
    # `includeLog`: Job().load omits the field unless asked.
    return Job().load(resp.json["_id"], force=True, includeLog=True)


@pytest.mark.plugin("sivacor")
def test_a_run_that_writes_a_file_named_like_its_main_file_is_pruned(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
    tmp_path,
):
    """Review item 1: an installed package's demo/main.R under an ignored lib.

    Matching every same-named file at prune time failed this run after it had
    succeeded, and charged the researcher for it.
    """
    job = _run_r(
        server,
        user,
        uploads_folder,
        tmp_path,
        {
            "main.R": b'dir.create("lib/pkg/demo", recursive = TRUE)\n'
            b'writeLines("x", "lib/pkg/demo/main.R")\n',
            ".sivacorignore": b"lib/\n",
        },
    )
    assert job["status"] == JobStatus.SUCCESS, job.get("log")

    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    folder = resp.json[0]
    replpack = File().load(folder["meta"]["replpack_file_id"], force=True)
    with File().open(replpack) as f, zipfile.ZipFile(f) as archive:
        names = archive.namelist()
    assert not any("lib/" in name for name in names)
    assert any(name.endswith("main.R") for name in names)
    assert any(name.endswith(".sivacorignore") for name in names)


@pytest.mark.plugin("sivacor")
def test_a_main_file_an_earlier_stage_wrote_is_protected_at_prune_time(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
    tmp_path,
):
    """Why the prune-time check still looks at main files, now that the rules
    cannot change: stage 2's main file does not exist until stage 1 writes it,
    so create_workspace had nothing to protect."""
    job = _run_r(
        server,
        user,
        uploads_folder,
        tmp_path,
        {
            "main.R": b'dir.create("gen")\nwriteLines("print(1)", "gen/next.R")\n',
            ".sivacorignore": b"gen/\n",
        },
        stages=R_STAGES + [{**R_STAGES[0], "main_file": "next.R"}],
    )
    assert job["status"] == JobStatus.ERROR

    log = "".join(job.get("log", []))
    assert "  - gen/next.R (main file) is matched by 'gen/' on line 1" in log

    [record] = ExecutionRecord().find({})
    assert record["error"]["step"] == "prune_workspace"
    assert record["error"]["code"] == FailureCode.IGNORE_FILE_INVALID.value


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "script",
    [
        b'file.remove(".sivacorignore")\n',
        (
            b'file.remove(".sivacorignore")\ndir.create("empty")\n'
            b'file.symlink("empty", ".sivacorignore")\n'
        ),
    ],
    ids=["removed", "link_to_directory"],
)
def test_a_stage_cannot_drop_the_uploaded_rules(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
    tmp_path,
    script,
):
    """Review's reproduction. Before, pruning fell back to the defaults and
    restricted/ was published in the signed package."""
    job = _run_r(
        server,
        user,
        uploads_folder,
        tmp_path,
        {
            "main.R": script,
            ".sivacorignore": b"restricted/\n",
            "restricted/data.csv": b"1,2\n",
        },
    )
    assert job["status"] == JobStatus.ERROR
    log = "".join(job.get("log", []))
    assert ".sivacorignore was removed, moved or replaced" in log

    [record] = ExecutionRecord().find({})
    assert record["error"]["step"] == "prune_workspace"
    assert record["error"]["code"] == FailureCode.IGNORE_FILE_INVALID.value


# --- reading the file as git does ------------------------------------------


def test_an_invalid_line_is_refused_by_number(tmp_path):
    _write(tmp_path, {"main.R": b"", "proj/.sivacorignore": b"# c\nout/\n!\n"})
    with pytest.raises(SubmissionError) as raised:
        protected_files_check(tmp_path, STAGES)
    assert raised.value.code is FailureCode.IGNORE_FILE_INVALID
    assert "Line 3 of proj/.sivacorignore, '!', is not a valid" in str(raised.value)


@pytest.mark.parametrize("separator", ["\x0c", "\x85", "\u2028", "\x1c"])
def test_lines_are_split_on_newlines_only(separator):
    """str.splitlines broke at each of these, so a pattern split in two and
    every later line number was off from the researcher's editor."""
    text = f"a{separator}b\n!\n"
    assert sivacorignore.read_patterns(text) == [(1, f"a{separator}b"), (2, "!")]
    with pytest.raises(sivacorignore.InvalidPattern) as raised:
        Rules.from_text(".sivacorignore", text)
    assert raised.value.lineno == 2


# --- who is to blame ------------------------------------------------------


def _read_only(tmp_path):
    _write(
        tmp_path,
        {
            "main.R": b"",
            ".sivacorignore": b"restricted/\n*.csv\n",
            "restricted/sub/x.csv": b"",
            "out/y.csv": b"",
        },
    )
    return prune_rules(tmp_path, ["main.R"], protected_files_check(tmp_path, STAGES))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode")
@pytest.mark.parametrize(
    "locked, mode",
    [
        ("restricted/sub", 0o555),  # chmod -w
        ("restricted/sub", 0o000),  # a private directory, then chmod 000
        ("restricted", 0o700),  # tempfile.mkdtemp, umask 077
        ("out", 0o555),  # the directory a pruned file sits in
    ],
    ids=["read_only", "no_access", "private", "parent_of_a_file"],
)
def test_a_directory_the_run_locked_is_pruned_anyway(tmp_path, locked, mode):
    """The analysis container runs as the worker's uid, so what a stage
    creates is the worker's, and its modes can be taken back."""
    rules = _read_only(tmp_path)
    (tmp_path / locked).chmod(mode)
    assert sorted(_prune(tmp_path, rules)) == ["out/y.csv", "restricted/"]
    assert not (tmp_path / "restricted").exists()
    assert not (tmp_path / "out/y.csv").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode")
@pytest.mark.parametrize(
    "locked, named",
    [("restricted/sub", "restricted/sub/x.csv"), ("out", "out/y.csv")],
    ids=["inside_a_pruned_directory", "a_pruned_file"],
)
def test_what_the_worker_cannot_unlock_is_blocked_and_not_charged(
    tmp_path, monkeypatch, locked, named
):
    """A MATLAB stage runs as another uid: the worker owns nothing it made
    and cannot restore an ACL mask. That is the platform's to get right."""
    rules = _read_only(tmp_path)
    (tmp_path / locked).chmod(0o555)
    monkeypatch.setattr(run_submission, "_owned", lambda st: False)
    try:
        with pytest.raises(SubmissionError) as raised:
            _prune(tmp_path, rules)
    finally:
        (tmp_path / locked).chmod(0o755)
    assert raised.value.code is FailureCode.PRUNE_BLOCKED
    assert str(raised.value).startswith(f"{named} could not be removed (Permission")
    assert "not charged to you" in str(raised.value)
    assert USER_PAYS[FailureCode.PRUNE_BLOCKED] is False


# --- agreeing with git, byte for byte ---------------------------------------
#
# Each case is a .sivacorignore; git's own answer (`git ls-files -o` with that
# file and SIVACOR's defaults) is the expected one.

TREE = [
    b"main.R", b"x.dta", b"y.do", b"a/x.dta", b"a/y.do", b"a/b/c.csv", b"a/b/d.do",
    b"restricted/x.csv", b"restricted/sub/y.csv", b"data/raw/secret.csv",
    b"data/clean.csv", b"doc/readme.md", b"doc/a/readme.md", b"foo",
    b"foo.bak/inner.txt", b"lib/foo/bar.txt", b"x/foo/y/z.txt", b"sp ace.txt",
    b"trail ", b"#hash", b"!bang", b"caf\xe9.csv", b"caf\xc3\xa9.csv", b"a[b].txt",
    b"q1.txt", b"tab\t", b"__pycache__/m.pyc", b"src/__pycache__/n.pyc",
    b"notes.tmp", b".DS_Store", b"deep/a/b/c/d.txt", b"Upper.TXT", b"-dash",
    b"]bracket", b"a/b2", b"xreadme.md", b"a/bc.csv", b"data/raw/mysecret.csv",
    b"lib/xfoo",
]  # fmt: skip

GIT_CASES = {
    "except_idiom": b"*\n!*/\n!*.do\n",
    "except_idiom_keeping_itself": b"*\n!*/\n!*.do\n!.sivacorignore\n",
    "dir_only": b"restricted/\n",
    "anchored": b"/restricted\n/a/b\n",
    "double_star_middle": b"data/**/secret.csv\na/**/c.csv\n",
    "double_star_leading": b"**/readme.md\n**/foo\n",
    "double_star_trailing": b"doc/**\n!doc/a/\n",
    "double_star_alone": b"**\n!a/\n",
    "stars_inside_a_segment": b"x**\nfoo.**\na/***\n**b2\n",
    "basename": b"foo\n",
    "basename_dir_only": b"foo/\n",
    "anchored_basename": b"/foo\n",
    "path": b"lib/foo\n*/foo\nx/*/y\n",
    "negation": b"*.csv\n!clean.csv\n",
    "negation_inside_an_ignored_dir": b"data/\n!data/clean.csv\n",
    "escapes": b"\\#hash\n\\!bang\n",
    "spaces": b"sp ace.txt\ntrail\\ \nmain.R   \n",
    "tab_is_not_trimmed": b"tab\t\n",
    "brackets": b"a\\[b\\].txt\nq[0-9].txt\n[!x]*.dta\n[a-c]/*\n",
    "unescaped_bracket": b"a[b].txt\n",
    "posix_classes": b"[[:upper:]]*\n[[:digit:]]*\n",
    "bracket_edges": b"[]]bracket\n[-]dash\n[!]]*.csv\n[z-a]*\n",
    "question_mark_is_one_byte": b"q?.txt\ncaf?.csv\n",
    "latin1_pattern": b"caf\xe9.csv\n",
    "utf8_pattern": "café.csv".encode(),
    "byte_order_mark": b"\xef\xbb\xbfrestricted/\n",
    "crlf": b"restricted/\r\n*.tmp\r\n",
    "comments_and_blanks": b"# c\n\n   \n#x\nrestricted/\n",
    "overriding_the_defaults": b"!*.tmp\n!__pycache__/\n",
    "leading_space": b" foo\n",
    "dotfiles": b".*\n!.sivacorignore\n",
    "slash_in_a_bracket": b"a[/]b2\n",
    "escaped_slash": b"a\\/b2\n",
    "dir_only_and_links": b"results/\ndata/\n",
    "basename_and_links": b"results\n",
    "escapes_in_brackets": b"[\\]]bracket\n[\\-]dash\n[a-\\c]/*\n",
    "colon_without_class": b"[[:a]/*\n",
}

#: Lines git reads but that match nothing there. Refused rather than honoured.
UNMATCHABLE = [
    b"!", b"/", b"//", b"!/", b"data\\", b"[abc", b"[[:nope:]]", b"[a\\",
    b"[a-\\", b"[[:alpha:",
]  # fmt: skip


#: Links to directories, which git judges as files: ``data/`` does not match
#: one, and ``!*/`` does not re-include one.
LINKS = {"results": "a", "linked/data": "../data"}


def _make_tree(root, ignore):
    for name in TREE:
        path = os.path.join(os.fsencode(root), name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb"):
            pass
    for name, target in LINKS.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).symlink_to(target, target_is_directory=True)
    (root / ".sivacorignore").write_bytes(ignore)


def _git_kept(tmp_path, ignore):
    tree = tmp_path / "tree"
    _make_tree(tree, ignore)
    defaults = tmp_path / "defaults"
    defaults.write_text("\n".join(sivacorignore.DEFAULT_PATTERNS) + "\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.fspath(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_DIR": os.fspath(tmp_path / "git"),
        "GIT_WORK_TREE": os.fspath(tree),
    }
    subprocess.run(["git", "init", "-q"], env=env, check=True)
    listed = subprocess.run(
        [
            "git",
            "ls-files",
            "-o",
            "-z",
            f"--exclude-from={defaults}",
            "--exclude-per-directory=.sivacorignore",
        ],
        cwd=tree,
        env=env,
        capture_output=True,
        check=True,
    ).stdout
    return tree, {os.fsdecode(name) for name in listed.split(b"\0") if name}


def _files(tree):
    """Every entry git would list as a file: files, and links to directories."""
    return {
        (pathlib.Path(root) / f).relative_to(tree).as_posix()
        for root, dirs, names in os.walk(tree)
        for f in names + [d for d in dirs if (pathlib.Path(root) / d).is_symlink()]
    }


def _our_kept(tree, ignore):
    rules = Rules.from_text(".sivacorignore", sivacorignore.decode(ignore))
    return {f for f in _files(tree) if not rules.pruned_by(f)}


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@needs_git
@pytest.mark.parametrize("ignore", GIT_CASES.values(), ids=GIT_CASES.keys())
def test_pruning_keeps_what_git_keeps(tmp_path, ignore):
    """Both the check's answer (pruned_by) and the walk's (what is left)."""
    tree, kept = _git_kept(tmp_path, ignore)
    assert _our_kept(tree, ignore) == kept
    _prune(tree, Rules.from_text(".sivacorignore", sivacorignore.decode(ignore)))
    assert _files(tree) == kept


@needs_git
@pytest.mark.parametrize("line", UNMATCHABLE, ids=[repr(x) for x in UNMATCHABLE])
def test_a_line_that_matches_nothing_in_git_is_refused(tmp_path, line):
    """The refusal is not stricter than git: the line really does nothing
    there, so honouring it silently would drop the rule the researcher meant."""
    _, with_line = _git_kept(tmp_path / "with", b"restricted/\n" + line + b"\n")
    _, without = _git_kept(tmp_path / "without", b"restricted/\n")
    assert with_line == without
    with pytest.raises(sivacorignore.InvalidPattern):
        Rules.from_text(".sivacorignore", sivacorignore.decode(line))


def test_a_latin1_rule_matches_the_latin1_name_and_is_printable(tmp_path):
    """errors="replace" turned the byte into U+FFFD, which matched nothing."""
    root = os.fsencode(tmp_path)
    for name, data in [(b"main.R", b""), (b".sivacorignore", b"caf\xe9.csv\n")]:
        with open(os.path.join(root, name), "wb") as f:
            f.write(data)
    open(os.path.join(root, b"caf\xe9.csv"), "wb").close()
    rules = prune_rules(tmp_path, ["main.R"], protected_files_check(tmp_path, STAGES))
    assert _prune(tmp_path, rules) == [os.fsdecode(b"caf\xe9.csv")]
    rule = rules.pruned_by(os.fsdecode(b"caf\xe9.csv"))
    assert rule == "'caf\\xe9.csv' on line 1 of .sivacorignore"
    rule.encode("utf-8")  # safe for the job log


# --- a package refused before anything runs --------------------------------


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "ignore_text",
    [b"*.R\n", b"#" * IGNORE_FILE_READ_LIMIT + b"\n", b"data/\n!\n"],
    ids=["prunes_main_file", "too_large", "invalid_pattern"],
)
def test_a_bad_ignore_file_stops_the_submission_in_create_workspace(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
    tmp_path,
    ignore_text,
):
    """Refused once extracted, before the stage's image is pulled."""
    job = _run_r(
        server,
        user,
        uploads_folder,
        tmp_path,
        {"main.R": b'writeLines("ran", "ran.txt")\n', ".sivacorignore": ignore_text},
    )
    assert job["status"] == JobStatus.ERROR

    [record] = ExecutionRecord().find({})
    assert record["error"]["step"] == "create_workspace"
    assert record["error"]["code"] == FailureCode.IGNORE_FILE_INVALID.value
    assert record.get("stages", []) == []


# --- what a stage can leave where .sivacorignore should be -----------------
#
# Read through, a symlink to a host file was read with the worker's permissions
# and matching lines quoted back into the job log, /dev/zero exhausted memory
# and a FIFO hung the step.


def _refused(project):
    with pytest.raises(SubmissionError) as raised:
        prune_rules(project, resolved=["main.R"], pinned=None)
    assert raised.value.code is FailureCode.IGNORE_FILE_UNREADABLE
    return str(raised.value)


def test_a_symlinked_ignore_file_is_not_followed(tmp_path):
    host_file = tmp_path / "host" / "secret"
    host_file.parent.mkdir()
    host_file.write_text("guessable-token\nmain.R\n")
    project = tmp_path / "project"
    _write(project, {"main.R": b""})
    (project / ".sivacorignore").symlink_to(host_file)

    message = _refused(project)
    assert "guessable-token" not in message
    assert "line 2" not in message  # what the leak looked like


def test_an_ignore_file_pointing_at_dev_zero_is_refused(tmp_path):
    _write(tmp_path, {"main.R": b""})
    (tmp_path / ".sivacorignore").symlink_to("/dev/zero")
    _refused(tmp_path)


def test_a_fifo_ignore_file_cannot_hang_the_prune(tmp_path):
    """Run in a thread so a regression fails here instead of hanging the suite."""
    _write(tmp_path, {"main.R": b""})
    os.mkfifo(tmp_path / ".sivacorignore")
    outcome = []

    def check():
        try:
            prune_rules(tmp_path, resolved=["main.R"], pinned=None)
        except SubmissionError as exc:
            outcome.append(exc.code)

    worker = threading.Thread(target=check, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "opening the FIFO blocked"
    assert outcome == [FailureCode.IGNORE_FILE_UNREADABLE]


def test_a_socket_ignore_file_is_a_special_file(tmp_path):
    _write(tmp_path, {"main.R": b""})
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(os.fspath(tmp_path / ".sivacorignore"))
        assert "is a link or a special file" in _refused(tmp_path)


def _unreadable(project, source=".sivacorignore"):
    with pytest.raises(SubmissionError) as raised:
        read_ignore_file(project, source)
    assert raised.value.code is FailureCode.IGNORE_FILE_UNREADABLE
    return str(raised.value)


def test_a_missing_ignore_file_is_not_called_a_link(tmp_path):
    message = _unreadable(tmp_path, "analysis/.sivacorignore")
    assert message.startswith("analysis/.sivacorignore could not be read (No such")
    assert "link" not in message


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_ignore_file_is_not_called_a_link(tmp_path):
    _write(tmp_path, {".sivacorignore": b"data/\n"})
    (tmp_path / ".sivacorignore").chmod(0)
    try:
        assert "could not be read (Permission denied)" in _unreadable(tmp_path)
    finally:
        (tmp_path / ".sivacorignore").chmod(0o644)


# --- a .sivacorignore over the limit is refused, never read in part --------

OVERSIZED = b"#" * IGNORE_FILE_READ_LIMIT + b"\nsecret.csv\n"


def test_the_worker_refuses_an_ignore_file_over_the_limit(tmp_path):
    at_limit = b"#" * (IGNORE_FILE_READ_LIMIT - 1) + b"\n"
    _write(tmp_path, {"main.R": b"", ".sivacorignore": at_limit})
    assert read_ignore_file(tmp_path, ".sivacorignore") == at_limit

    (tmp_path / ".sivacorignore").write_bytes(OVERSIZED)
    for check in (  # create_workspace, then prune_workspace
        lambda: protected_files_check(tmp_path, STAGES),
        lambda: prune_rules(tmp_path, resolved=["main.R"], pinned=None),
    ):
        with pytest.raises(SubmissionError) as raised:
            check()
        assert raised.value.code is FailureCode.IGNORE_FILE_INVALID
        assert ".sivacorignore is larger than 1 MB" in str(raised.value)


def test_the_size_refusal_names_the_file(tmp_path):
    _write(tmp_path, {"analysis/.sivacorignore": OVERSIZED})
    with pytest.raises(SubmissionError) as raised:
        read_ignore_file(tmp_path, "analysis/.sivacorignore")
    assert str(raised.value).startswith("analysis/.sivacorignore is larger than")


@pytest.mark.plugin("sivacor")
def test_a_stage_cannot_grow_the_ignore_file_past_the_limit(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
    tmp_path,
):
    """Review's reproduction: the rule past the cut must not just vanish.

    Read in part, the rules would have kept ``secret.csv`` and the run would
    have succeeded, publishing it.
    """
    job = _run_r(
        server,
        user,
        uploads_folder,
        tmp_path,
        {
            "main.R": b'writeLines("x", "secret.csv")\n'
            b"writeLines(c(strrep(\"#\", 1048576), \"secret.csv\"), "
            b'".sivacorignore")\n',
        },
    )
    assert job["status"] == JobStatus.ERROR
    assert ".sivacorignore is larger than 1 MB" in "".join(job.get("log", []))

    [record] = ExecutionRecord().find({})
    assert record["error"]["step"] == "prune_workspace"
    assert record["error"]["code"] == FailureCode.IGNORE_FILE_INVALID.value


@pytest.mark.plugin("sivacor")
def test_a_stage_cannot_point_the_ignore_file_at_a_host_file(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
    tmp_path,
):
    """Review's reproduction, end to end.

    The link is made inside the container, but the target is resolved by the
    worker, on the host side of the bind mount. Read through, the host file's
    second line ``main.R`` would come back in the job log as "'main.R' on line
    2 of .sivacorignore" -- one guess about a host file per submission.
    """
    host_file = tmp_path / "host-secret"
    host_file.write_text("guessable-token\nmain.R\n")
    job = _run_r(
        server,
        user,
        uploads_folder,
        tmp_path,
        {"main.R": f'file.symlink("{host_file}", ".sivacorignore")\n'.encode()},
    )
    assert job["status"] == JobStatus.ERROR

    log = "".join(job.get("log", []))
    assert ".sivacorignore could not be read (it is a link or a special file)" in log
    assert "on line 2 of .sivacorignore" not in log
    assert "guessable-token" not in log

    [record] = ExecutionRecord().find({})
    assert record["error"]["step"] == "prune_workspace"
    assert record["error"]["code"] == FailureCode.IGNORE_FILE_UNREADABLE.value
