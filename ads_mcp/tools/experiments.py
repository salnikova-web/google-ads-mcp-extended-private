# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Campaign experiments (A/B tests).

Flow: experiments_experiment_create (makes experiment + control/treatment
arms and schedules it — Google copies the control campaign into a treatment
campaign) -> edit the treatment campaign with the regular tools ->
experiments_experiment_end or experiments_experiment_promote.

Safety model: ``confirm=False`` = preview only (experiment scheduling has
side effects that validate_only cannot fully cover, so nothing is sent to
Google Ads and nothing is validated).
"""

import logging
from typing import Any, Dict, NoReturn, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils
from ads_mcp.tools._write_common import (
    _WRITE_ANNOTATIONS as _WRITE,
    _clean_customer_id,
    _preview_or_done,
    _raise_tool_error,
)

logger = logging.getLogger(__name__)

experiments_mcp = FastMCP("experiments")

_READ = ToolAnnotations(readOnlyHint=True)

_TYPES = ("SEARCH_CUSTOM", "DISPLAY_CUSTOM", "VIDEO_CUSTOM")

# What the caller has to know when creation got past step 1: the shell is
# real, it holds the name, and re-running the tool cannot be the fix.
_ORPHAN_CONTEXT = (
    "experiment {resource_name} was created but arms/scheduling failed — "
    "a retry with the same name will hit duplicate-name; end or reuse it "
    "(experiments_experiments_list to find it, then "
    "experiments_experiment_end)."
)


def _raise_orphaned_experiment(
    experiment_rn: str, ex: GoogleAdsException
) -> NoReturn:
    """Re-raises a post-creation failure naming the experiment left behind.

    The formatted message from the module's normal error path is kept
    verbatim and the orphan context is appended to it. The re-raise is
    bare: chaining it to the ``GoogleAdsException`` would give
    ``GoogleAdsErrorMiddleware`` a ``__cause__`` to walk, and it would
    throw this message away for a generic one (see
    ``_write_common._raise_tool_error``).
    """
    try:
        _raise_tool_error(ex)
    except ToolError as formatted:
        raise ToolError(
            f"{formatted}\n"
            + _ORPHAN_CONTEXT.format(resource_name=experiment_rn)
        )


@experiments_mcp.tool(annotations=_WRITE)
def experiment_create(
    customer_id: str,
    name: str,
    control_campaign_id: str,
    traffic_split_percent: int = 50,
    experiment_type: str = "SEARCH_CUSTOM",
    description: Optional[str] = None,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Creates and schedules a campaign experiment (A/B test).

    Google copies the control campaign into a treatment campaign and splits
    traffic. After creation, modify the treatment campaign with the regular
    tools (find its id via experiments_experiments_list), then end or
    promote.

    SAFETY: with confirm=false nothing is sent to Google Ads, so the preview
    is computed locally and nothing is validated.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        name: Experiment name (unique).
        control_campaign_id: The campaign to test against (must be ENABLED).
        traffic_split_percent: Share of traffic for the TREATMENT arm,
            1-99 (default 50).
        experiment_type: SEARCH_CUSTOM (default), DISPLAY_CUSTOM or
            VIDEO_CUSTOM — must match the campaign channel.
        description: Optional description.
        confirm: False = preview only (default), True = create + schedule.
    """
    customer_id = _clean_customer_id(customer_id)
    experiment_type = experiment_type.upper()
    if experiment_type not in _TYPES:
        raise ToolError(f"experiment_type must be one of {_TYPES}")
    if not (1 <= traffic_split_percent <= 99):
        raise ToolError("traffic_split_percent must be 1-99")

    preview = {
        "customer_id": customer_id,
        "experiment_name": name,
        "control_campaign_id": str(control_campaign_id),
        "traffic_split_percent": traffic_split_percent,
        "experiment_type": experiment_type,
    }
    if not confirm:
        return _preview_or_done(
            False, "experiments_experiment_create", preview, validated=False
        )

    client = utils.get_googleads_client()
    exp_service = utils.get_googleads_service("ExperimentService")
    arm_service = utils.get_googleads_service("ExperimentArmService")

    # 1. The experiment shell.
    exp_op = client.get_type("ExperimentOperation")
    exp = exp_op.create
    exp.name = name
    if description:
        exp.description = description
    exp.type_ = client.enums.ExperimentTypeEnum[experiment_type]
    exp.suffix = "[experiment]"
    exp.status = client.enums.ExperimentStatusEnum.SETUP

    exp_request = client.get_type("MutateExperimentsRequest")
    exp_request.customer_id = customer_id
    exp_request.operations.append(exp_op)

    try:
        exp_response = exp_service.mutate_experiments(request=exp_request)
    except GoogleAdsException as ex:
        # Step 1 failed, so nothing was created and there is no orphan.
        _raise_tool_error(ex)
    experiment_rn = exp_response.results[0].resource_name

    # Everything below runs with the experiment shell already in the
    # account, holding its (unique) name. A failure here used to surface as
    # a bare API error, and the obvious next move -- run the tool again --
    # then failed on duplicate-name with no hint as to why.
    try:
        # 2. Control + treatment arms.
        arm_request = client.get_type("MutateExperimentArmsRequest")
        arm_request.customer_id = customer_id

        control_op = client.get_type("ExperimentArmOperation")
        control = control_op.create
        control.experiment = experiment_rn
        control.name = "control"
        control.control = True
        control.traffic_split = 100 - traffic_split_percent
        control.campaigns.append(
            f"customers/{customer_id}/campaigns/{control_campaign_id}"
        )
        arm_request.operations.append(control_op)

        treatment_op = client.get_type("ExperimentArmOperation")
        treatment = treatment_op.create
        treatment.experiment = experiment_rn
        treatment.name = "treatment"
        treatment.control = False
        treatment.traffic_split = traffic_split_percent
        arm_request.operations.append(treatment_op)

        arm_service.mutate_experiment_arms(request=arm_request)

        # 3. Schedule: Google builds the treatment campaign (async LRO).
        schedule_request = client.get_type("ScheduleExperimentRequest")
        schedule_request.resource_name = experiment_rn
        exp_service.schedule_experiment(request=schedule_request)
    except GoogleAdsException as ex:
        _raise_orphaned_experiment(experiment_rn, ex)
    except Exception:
        # Broad, and only to attach context: a transport or auth failure
        # leaves the same orphan behind. The original travels on untouched
        # so GoogleAdsErrorMiddleware can still translate it -- the log
        # line is where the resource name survives in that case.
        logger.warning(
            "google-ads experiment %s was created but arms/scheduling "
            "failed; a retry with the same name will hit duplicate-name.",
            experiment_rn,
        )
        raise

    preview["experiment_resource"] = experiment_rn
    preview["note"] = (
        "Scheduled. Google is creating the treatment campaign (may take a "
        "few minutes). Use experiments_experiments_list to see it, then "
        "edit the treatment campaign with the regular tools."
    )
    return _preview_or_done(True, "experiments_experiment_create", preview)


@experiments_mcp.tool(annotations=_READ)
def experiments_list(
    customer_id: str,
    limit: int = 50,
) -> Dict[str, Any]:
    """Lists experiments with status and their arm campaigns.

    Returns {"experiments": [...], "returned": n, "truncated": bool} —
    ``truncated`` is true when more experiments exist than were returned.
    The arms of every returned experiment are always complete.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        limit: Max experiments to return, ordered by name (default 50).
    """
    customer_id = _clean_customer_id(customer_id)
    limit = max(int(limit), 1)
    ga_service = utils.get_googleads_service("GoogleAdsService")
    try:
        # One row over the limit only reveals that more exist; it is dropped.
        exp_rows = list(
            ga_service.search(
                customer_id=customer_id,
                query=(
                    "SELECT experiment.experiment_id, experiment.name, "
                    "experiment.status, experiment.type, "
                    "experiment.start_date, experiment.end_date "
                    "FROM experiment "
                    "WHERE experiment.status != 'REMOVED' "
                    f"ORDER BY experiment.name LIMIT {limit + 1}"
                ),
            )
        )
        truncated = len(exp_rows) > limit
        experiments = {
            str(r.experiment.experiment_id): {
                "id": str(r.experiment.experiment_id),
                "name": r.experiment.name,
                "status": r.experiment.status.name,
                "type": r.experiment.type_.name,
                "start_date": r.experiment.start_date,
                "end_date": r.experiment.end_date,
                "arms": [],
            }
            for r in exp_rows[:limit]
        }
        if experiments:
            # Scoped to the listed experiments instead of capped by rows: a
            # row cap here would silently drop arms of a listed experiment.
            wanted = ", ".join(
                f"'customers/{customer_id}/experiments/"
                f"{utils.gaql_id(exp_id)}'"
                for exp_id in experiments
            )
            arm_rows = ga_service.search(
                customer_id=customer_id,
                query=(
                    "SELECT experiment_arm.experiment, experiment_arm.name, "
                    "experiment_arm.control, experiment_arm.traffic_split, "
                    "experiment_arm.campaigns FROM experiment_arm "
                    f"WHERE experiment_arm.experiment IN ({wanted})"
                ),
            )
            for r in arm_rows:
                exp_id = r.experiment_arm.experiment.split("/")[-1]
                if exp_id in experiments:
                    experiments[exp_id]["arms"].append(
                        {
                            "name": r.experiment_arm.name,
                            "control": r.experiment_arm.control,
                            "traffic_split": r.experiment_arm.traffic_split,
                            "campaigns": list(r.experiment_arm.campaigns),
                        }
                    )
        result: Dict[str, Any] = {
            "experiments": list(experiments.values()),
            "returned": len(experiments),
            "truncated": truncated,
        }
        # Keys stay as they are (experiments/returned/truncated); the cut
        # is what gains a voice — an experiment missing from a truncated
        # list is not proof it does not exist.
        if truncated:
            result["warning"] = utils.truncation_warning(limit)
        return result
    except GoogleAdsException as ex:
        _raise_tool_error(ex)


@experiments_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def experiment_end(
    customer_id: str,
    experiment_id: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Ends a running experiment (treatment campaign stops serving,
    control gets 100% traffic back).

    SAFETY: with confirm=false nothing is sent to Google Ads, so the preview
    is computed locally and nothing is validated.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        experiment_id: The numeric id of the experiment.
        confirm: False = preview only (default), True = end it.
    """
    customer_id = _clean_customer_id(customer_id)
    details = {
        "customer_id": customer_id,
        "experiment_id": str(experiment_id),
    }
    if not confirm:
        return _preview_or_done(
            False, "experiments_experiment_end", details, validated=False
        )

    client = utils.get_googleads_client()
    exp_service = utils.get_googleads_service("ExperimentService")
    request = client.get_type("EndExperimentRequest")
    request.experiment = f"customers/{customer_id}/experiments/{experiment_id}"
    try:
        exp_service.end_experiment(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)
    return _preview_or_done(True, "experiments_experiment_end", details)


@experiments_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def experiment_promote(
    customer_id: str,
    experiment_id: str,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Promotes a winning experiment: treatment settings are applied to the
    original campaign. IRREVERSIBLE.

    SAFETY: with confirm=false nothing is sent to Google Ads, so the preview
    is computed locally and nothing is validated.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        experiment_id: The numeric id of the experiment.
        confirm: False = preview only (default), True = promote.
    """
    customer_id = _clean_customer_id(customer_id)
    details = {
        "customer_id": customer_id,
        "experiment_id": str(experiment_id),
    }
    if not confirm:
        return _preview_or_done(
            False, "experiments_experiment_promote", details, validated=False
        )

    client = utils.get_googleads_client()
    exp_service = utils.get_googleads_service("ExperimentService")
    request = client.get_type("PromoteExperimentRequest")
    request.resource_name = (
        f"customers/{customer_id}/experiments/{experiment_id}"
    )
    try:
        exp_service.promote_experiment(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)
    details["note"] = "Promotion started (async). Check status in the UI."
    return _preview_or_done(True, "experiments_experiment_promote", details)
