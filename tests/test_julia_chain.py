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
    assert [kwargs["phase"] for _, kwargs in performances] == [
        PHASE_RESOLVE,
        PHASE_ANALYSIS,
    ]
    # Both belong to stage 0 -- the stage index and the arrangement counter have
    # parted company, which is the whole point.
    assert [kwargs["stage_index"] for _, kwargs in performances] == [0, 0]
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
    assert [kwargs["stage_index"] for _, kwargs in performances] == expected_stage_indices
    assert [kwargs["phase"] for _, kwargs in performances] == expected_phases

    # Every performance n accesses arrangement n and produces n+1, and the prune
    # picks up exactly where the last one left off.
    assert [args[1] for args, _ in performances] == list(range(len(performances)))
    assert prune[0][0][1] == len(performances)
    assert arrangements[-1] == len(performances) + 1


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
