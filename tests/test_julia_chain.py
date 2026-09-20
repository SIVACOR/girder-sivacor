"""The chain a Julia stage produces, and the numbering it must not break.

A stage used to mean exactly one performance, so the builder could derive every
arrangement number from ``enumerate``. A Julia stage produces two -- the
dependency resolve and the run -- and the interesting failure is not that a step
is missing but that the *numbers* drift: a declaration with misnumbered
arrangements still validates against the schema and still signs, and says
something false about what accessed what.
"""

import pytest
from girder.models.user import User

from girder_sivacor.rest import build_submission_chain
from girder_sivacor.worker_plugin.lib import PHASE_ANALYSIS, PHASE_RESOLVE

R_STAGE = {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
JULIA_STAGE = {
    "image_name": "ghcr.io/sivacor/julia1.11",
    "image_tag": "1.11.9-20260916",
    "main_file": "main.jl",
}


def chain_for(stages, admin):
    job = {"_id": "000000000000000000000001", "userId": admin["_id"], "meta": {}}
    file = {"_id": "000000000000000000000002", "name": "package.zip"}
    chain = build_submission_chain(job, file, stages, [])
    steps = []
    for task in chain.tasks:
        steps.append(
            (task["task"].rsplit(".", 1)[-1], list(task.args), dict(task.kwargs))
        )
    return steps


def tro_steps(steps, action):
    return [(args, kwargs) for name, args, kwargs in steps if name == "run_tro" and args[0] == action]


def labels(args):
    """``(stage_index, phase)`` for a ``run_tro`` signature, or ``(None, None)``.

    Both are passed positionally, and the tests read them positionally, because
    girder_worker cannot record a child job for a step that carries a keyword
    argument -- see the comment above the chain in ``rest.py``. Reading them out
    of ``kwargs`` here would let that regression back in silently: the chain
    would still be correct and the submission would still run, while the job
    tree quietly truncated.
    """
    padded = list(args) + [None] * (5 - len(args))
    return padded[3], padded[4]


@pytest.mark.plugin("sivacor")
def test_a_non_julia_stage_is_numbered_exactly_as_before(server, db, admin):
    """The regression guard. Every other stack must be untouched by this."""
    steps = chain_for([R_STAGE], admin)
    names = [name for name, _, _ in steps]

    assert "resolve_dependencies" not in names
    assert [args[1] for args, _ in tro_steps(steps, "add_arrangement")] == [0, 1, 2]
    assert [args[1] for args, _ in tro_steps(steps, "add_performance")] == [0]
    assert [args[1] for args, _ in tro_steps(steps, "prune_performance")] == [1]


@pytest.mark.plugin("sivacor")
def test_a_julia_stage_adds_a_resolve_before_the_run(server, db, admin):
    steps = chain_for([JULIA_STAGE], admin)
    names = [name for name, _, _ in steps]

    # Order matters as much as presence: resolving after the run would be a
    # chain that "contains" both steps and does nothing useful.
    assert names.index("resolve_dependencies") < names.index("execute_workflow")

    # Four arrangements for one stage: initial, after resolve, after run, pruned.
    assert [args[1] for args, _ in tro_steps(steps, "add_arrangement")] == [0, 1, 2, 3]

    performances = tro_steps(steps, "add_performance")
    assert [args[1] for args, _ in performances] == [0, 1]
    assert [labels(args)[1] for args, _ in performances] == [
        PHASE_RESOLVE,
        PHASE_ANALYSIS,
    ]
    # Both belong to stage 0 -- the stage index and the arrangement counter have
    # parted company, which is the whole point.
    assert [labels(args)[0] for args, _ in performances] == [0, 0]
    assert [args[1] for args, _ in tro_steps(steps, "prune_performance")] == [2]


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "stages,expected_stage_indices,expected_phases",
    [
        (
            [JULIA_STAGE, R_STAGE],
            [0, 0, 1],
            [PHASE_RESOLVE, PHASE_ANALYSIS, PHASE_ANALYSIS],
        ),
        (
            [R_STAGE, JULIA_STAGE],
            [0, 1, 1],
            [PHASE_ANALYSIS, PHASE_RESOLVE, PHASE_ANALYSIS],
        ),
        (
            [JULIA_STAGE, JULIA_STAGE],
            [0, 0, 1, 1],
            [PHASE_RESOLVE, PHASE_ANALYSIS, PHASE_RESOLVE, PHASE_ANALYSIS],
        ),
    ],
    ids=["julia-then-r", "r-then-julia", "julia-twice"],
)
def test_mixed_stages_keep_one_contiguous_arrangement_sequence(
    server, db, admin, stages, expected_stage_indices, expected_phases
):
    """A counter, not a function of the stage index.

    This is the case that breaks if anyone reintroduces ``enumerate``: the
    numbering is right up to the first Julia stage and wrong after it, so a
    single-stage test would pass and the declaration would still sign.
    """
    steps = chain_for(stages, admin)

    arrangements = [args[1] for args, _ in tro_steps(steps, "add_arrangement")]
    performances = tro_steps(steps, "add_performance")
    prune = tro_steps(steps, "prune_performance")

    # Contiguous from 0, with no repeats -- a repeated number is the signature
    # of a counter that failed to advance.
    assert arrangements == list(range(len(arrangements)))
    assert [labels(args)[0] for args, _ in performances] == expected_stage_indices
    assert [labels(args)[1] for args, _ in performances] == expected_phases

    # Every performance n accesses arrangement n and produces n+1, and the prune
    # picks up exactly where the last one left off.
    assert [args[1] for args, _ in performances] == list(range(len(performances)))
    assert prune[0][0][1] == len(performances)
    assert arrangements[-1] == len(performances) + 1


@pytest.mark.plugin("sivacor")
def test_arrangements_are_labelled_by_stage_and_phase(server, db, admin):
    """A wrong comment in a signed document is worth no less care than a wrong number.

    The arrangement comment used to be built from the arrangement counter, which
    was the stage number only while the two could not diverge. On a Julia stage
    it labelled the after-resolve snapshot "After executing workflow step 1"
    when no workflow had executed -- the resolve had, and the very next
    arrangement was the real step 1.
    """
    steps = chain_for([JULIA_STAGE, R_STAGE], admin)
    arrangements = [
        (args[1], *labels(args))
        for name, args, _ in steps
        if name == "run_tro" and args[0] == "add_arrangement"
    ]
    # initial (no phase), after-resolve of stage 0, after-run of stage 0,
    # after-run of stage 1, pruned.
    assert [a for a, _, _ in arrangements] == [0, 1, 2, 3, 4]
    assert [(s, p) for _, s, p in arrangements[1:4]] == [
        (0, PHASE_RESOLVE),
        (0, PHASE_ANALYSIS),
        (1, PHASE_ANALYSIS),
    ]


@pytest.mark.plugin("sivacor")
def test_the_resolve_step_carries_the_same_credentials_as_every_other(server, db, admin):
    """girder_worker copies headers from a *running* task; a chain is built in one go."""
    steps = chain_for([JULIA_STAGE], admin)
    chain = build_submission_chain(
        {"_id": "000000000000000000000001", "userId": admin["_id"], "meta": {}},
        {"_id": "000000000000000000000002", "name": "package.zip"},
        [JULIA_STAGE],
        [],
    )
    assert any(name == "resolve_dependencies" for name, _, _ in steps)
    for task in chain.tasks:
        assert task.options["girder_api_url"]
        assert task.options["girder_client_token"]


@pytest.mark.plugin("sivacor")
def test_an_admin_is_required_to_build_a_chain(db, admin):
    """Guards the fixture, not the code: no admin, no worker token, no chain."""
    assert User().findOne({"admin": True}) is not None


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "stages",
    [[R_STAGE], [JULIA_STAGE], [JULIA_STAGE, R_STAGE], [R_STAGE, JULIA_STAGE]],
    ids=["r", "julia", "julia-then-r", "r-then-julia"],
)
def test_no_step_carries_a_keyword_argument(server, db, admin, stages):
    """The guard for a bug that leaves the submission working and the record wrong.

    girder_worker's girder_before_task_publish JSON-encodes a task's args but
    not its kwargs before putting them in the POST /job query string, and
    requests turns a dict query param into one repetition per *key*. A single
    keyword argument anywhere in the chain is therefore a 400 from /job, and no
    child job is recorded for that step or for any step after it -- while the
    chain itself runs to completion and signs. Nothing else here would notice:
    the arrangement numbers stay right, the declaration still validates, and the
    only symptom is a truncated job tree and a warning stranded in the last step
    that still had somewhere to log.

    This first bit when `stage_index`/`phase` arrived with Julia support, which
    is why it is parametrized over the stacks that have no resolve phase too --
    the next keyword argument is as likely to be added to a shared step.
    """
    for name, _, kwargs in chain_for(stages, admin):
        assert kwargs == {}, f"{name} must pass its arguments positionally"
