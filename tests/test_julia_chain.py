"""The chain a Julia stage produces: exactly the chain every other stack produces.

SIVACOR briefly injected a dependency-resolution step ahead of any Julia stage,
which made a stage worth two performances and the arrangement numbers a running
counter rather than the stage index. It was removed: a stage that installs
dependencies is a stage the researcher wrote, and the chain is once again a pure
function of the stages they submitted.

The interesting failure here is not that a step is missing but that the
*numbers* drift -- a declaration with misnumbered arrangements still validates
against the schema and still signs, and says something false about what accessed
what. So these assert the numbering, not merely the shape.
"""

import pytest
from girder.models.user import User

from girder_sivacor.rest import build_submission_chain

R_STAGE = {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
JULIA_STAGE = {
    "image_name": "ghcr.io/sivacor/julia1.11",
    "image_tag": "1.11.9-20260916",
    "main_file": "main.jl",
}
#: What replaced the injected resolve phase: an ordinary stage, written by the
#: researcher, that happens to install things. Nothing in the builder knows that.
JULIA_SETUP_STAGE = {**JULIA_STAGE, "main_file": "setup.jl"}


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
    return [
        (args, kwargs)
        for name, args, kwargs in steps
        if name == "run_tro" and args[0] == action
    ]


def stage_index(args):
    """The ``stage_index`` of a ``run_tro`` signature, or ``None``.

    Read positionally, because girder_worker cannot record a child job for a
    step that carries a keyword argument -- see the comment above the chain in
    ``rest.py``. Reading it out of ``kwargs`` here would let that regression back
    in silently: the chain would still be correct and the submission would still
    run, while the job tree quietly truncated.
    """
    padded = list(args) + [None] * (4 - len(args))
    return padded[3]


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("stage", [R_STAGE, JULIA_STAGE], ids=["r", "julia"])
def test_one_stage_is_one_run_whatever_the_stack(server, db, admin, stage):
    """No stack gets a step the researcher did not ask for."""
    steps = chain_for([stage], admin)
    names = [name for name, _, _ in steps]

    assert names.count("execute_workflow") == 1
    assert "resolve_dependencies" not in names
    assert [args[1] for args, _ in tro_steps(steps, "add_arrangement")] == [0, 1, 2]
    assert [args[1] for args, _ in tro_steps(steps, "add_performance")] == [0]
    assert [args[1] for args, _ in tro_steps(steps, "prune_performance")] == [1]


@pytest.mark.plugin("sivacor")
def test_a_setup_stage_is_just_another_stage(server, db, admin):
    """Two stages in, two runs and two performances out -- in submitted order.

    This is the whole replacement for the resolve phase, and the reason it needs
    no machinery: the researcher's setup script is a stage, so it is numbered,
    logged, snapshotted and certified by the same code as every other stage.
    """
    steps = chain_for([JULIA_SETUP_STAGE, JULIA_STAGE], admin)
    names = [name for name, _, _ in steps]

    assert names.count("execute_workflow") == 2
    assert [args[1] for args, _ in tro_steps(steps, "add_arrangement")] == [0, 1, 2, 3]
    performances = tro_steps(steps, "add_performance")
    assert [args[1] for args, _ in performances] == [0, 1]
    assert [stage_index(args) for args, _ in performances] == [0, 1]
    assert [args[1] for args, _ in tro_steps(steps, "prune_performance")] == [2]


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "stages",
    [
        [JULIA_STAGE, R_STAGE],
        [R_STAGE, JULIA_STAGE],
        [JULIA_SETUP_STAGE, JULIA_STAGE, R_STAGE],
    ],
    ids=["julia-then-r", "r-then-julia", "setup-julia-r"],
)
def test_arrangements_are_one_contiguous_sequence(server, db, admin, stages):
    """Every performance n accesses arrangement n and produces n+1."""
    steps = chain_for(stages, admin)

    arrangements = [args[1] for args, _ in tro_steps(steps, "add_arrangement")]
    performances = tro_steps(steps, "add_performance")
    prune = tro_steps(steps, "prune_performance")

    # Contiguous from 0, with no repeats -- a repeated number is the signature
    # of a counter that failed to advance.
    assert arrangements == list(range(len(arrangements)))
    assert [args[1] for args, _ in performances] == list(range(len(stages)))
    assert [stage_index(args) for args, _ in performances] == list(range(len(stages)))
    assert prune[0][0][1] == len(stages)
    assert arrangements[-1] == len(stages) + 1


@pytest.mark.plugin("sivacor")
def test_arrangements_are_labelled_by_stage(server, db, admin):
    """The label is the stage index, passed explicitly, not the arrangement counter.

    They are equal again now that a stage means one performance, so deriving one
    from the other would pass every test here and be wrong the next time they
    diverge. A wrong comment in a signed document is worth no less care than a
    wrong number.
    """
    steps = chain_for([JULIA_SETUP_STAGE, JULIA_STAGE], admin)
    arrangements = [
        (args[1], stage_index(args)) for args, _ in tro_steps(steps, "add_arrangement")
    ]
    # initial (no stage), after stage 0, after stage 1, pruned (no stage).
    assert arrangements == [(0, None), (1, 0), (2, 1), (3, None)]


@pytest.mark.plugin("sivacor")
def test_every_step_carries_the_same_credentials(server, db, admin):
    """girder_worker copies headers from a *running* task; a chain is built in one go."""
    chain = build_submission_chain(
        {"_id": "000000000000000000000001", "userId": admin["_id"], "meta": {}},
        {"_id": "000000000000000000000002", "name": "package.zip"},
        [JULIA_SETUP_STAGE, JULIA_STAGE],
        [],
    )
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
    [[R_STAGE], [JULIA_STAGE], [JULIA_SETUP_STAGE, JULIA_STAGE], [R_STAGE, JULIA_STAGE]],
    ids=["r", "julia", "setup-then-julia", "r-then-julia"],
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

    This first bit when `stage_index` arrived with Julia support, which is why it
    is parametrized across stacks -- the next keyword argument is as likely to be
    added to a shared step.
    """
    for name, _, kwargs in chain_for(stages, admin):
        assert kwargs == {}, f"{name} must pass its arguments positionally"
