# Copyright 2026 the google-ads-mcp-extended contributors.
# Licensed under the Apache License, Version 2.0.

"""Campaign experiments (A/B tests).

Flow: experiment_create (makes experiment + control/treatment arms and
schedules it — Google copies the control campaign into a treatment
campaign) -> edit the treatment campaign with the regular tools ->
experiment_end or experiment_promote.

Safety model: ``confirm=False`` = preview only (experiment scheduling has
side effects that validate_only cannot fully cover, so nothing is sent).
"""

from typing import Any, Dict, List, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from google.ads.googleads.errors import GoogleAdsException

import ads_mcp.utils as utils
from ads_mcp.tools.mutate import (
    _clean_customer_id,
    _preview_or_done,
    _raise_tool_error,
)

experiments_mcp = FastMCP("experiments")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_READ = ToolAnnotations(readOnlyHint=True)

_TYPES = ("SEARCH_CUSTOM", "DISPLAY_CUSTOM", "VIDEO_CUSTOM")


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
    tools (find its id via experiment_list), then end or promote.

    SAFETY: with confirm=false nothing is sent to Google.

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
        return _preview_or_done(False, "experiments_create", preview)

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
        experiment_rn = exp_response.results[0].resource_name

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
        _raise_tool_error(ex)

    preview["experiment_resource"] = experiment_rn
    preview["note"] = (
        "Scheduled. Google is creating the treatment campaign (may take a "
        "few minutes). Use experiments_list to see it, then edit the "
        "treatment campaign with the regular tools."
    )
    return _preview_or_done(True, "experiments_create", preview)


@experiments_mcp.tool(annotations=_READ)
def experiments_list(
    customer_id: str,
) -> List[Dict[str, Any]]:
    """Lists experiments with status and their arm campaigns.

    Args:
        customer_id: The client account id (digits only, no hyphens).
    """
    customer_id = _clean_customer_id(customer_id)
    ga_service = utils.get_googleads_service("GoogleAdsService")
    try:
        exp_rows = ga_service.search(
            customer_id=customer_id,
            query=(
                "SELECT experiment.experiment_id, experiment.name, "
                "experiment.status, experiment.type, experiment.start_date, "
                "experiment.end_date FROM experiment "
                "WHERE experiment.status != 'REMOVED'"
            ),
        )
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
            for r in exp_rows
        }
        arm_rows = ga_service.search(
            customer_id=customer_id,
            query=(
                "SELECT experiment_arm.experiment, experiment_arm.name, "
                "experiment_arm.control, experiment_arm.traffic_split, "
                "experiment_arm.campaigns FROM experiment_arm"
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
        return list(experiments.values())
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

    SAFETY: with confirm=false nothing is sent.

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
        return _preview_or_done(False, "experiments_end", details)

    client = utils.get_googleads_client()
    exp_service = utils.get_googleads_service("ExperimentService")
    request = client.get_type("EndExperimentRequest")
    request.experiment = (
        f"customers/{customer_id}/experiments/{experiment_id}"
    )
    try:
        exp_service.end_experiment(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)
    return _preview_or_done(True, "experiments_end", details)


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

    SAFETY: with confirm=false nothing is sent.

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
        return _preview_or_done(False, "experiments_promote", details)

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
    return _preview_or_done(True, "experiments_promote", details)
