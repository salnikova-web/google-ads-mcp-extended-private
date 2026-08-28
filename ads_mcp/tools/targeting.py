# Copyright 2026 the google-ads-mcp-extended contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Campaign targeting tools: locations (geo), languages, ad schedule.

Campaign criteria work for all campaign types (Search, PMax, Demand Gen,
Display, Video).

Safety model: identical to ads_mcp.tools.mutate — every write tool accepts
``confirm`` (default ``False`` = validate_only dry-run preview).
"""

import re
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

targeting_mcp = FastMCP("targeting")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_READ = ToolAnnotations(readOnlyHint=True)

_DAYS = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)


@targeting_mcp.tool(annotations=_READ)
def geo_lookup(
    location_names: List[str],
    country_code: Optional[str] = None,
    locale: str = "en",
) -> List[Dict[str, Any]]:
    """Find geo target constant ids by location name.

    WHEN TO USE: before targeting_set_locations or
    targeting_set_locations_ad_group, which take ids, not names.
    PRECONDITIONS: none — read-only and account-independent (no
    customer_id).
    SIDE EFFECTS: none. An ambiguous name returns several rows, so check
    canonical_name before using an id.
    UNITS & IDS: "id" is a geo target constant id (2276 = Germany), not an
    account id.

    Args:
        location_names: Names to look up, e.g. ["Germany", "Berlin",
            "United States"].
        country_code: 2-letter country filter, e.g. "DE" (recommended when
            looking up cities).
        locale: Language the names are written in (default "en").
    """
    client = utils.get_googleads_client()
    geo_service = utils.get_googleads_service("GeoTargetConstantService")

    request = client.get_type("SuggestGeoTargetConstantsRequest")
    request.locale = locale
    if country_code:
        request.country_code = country_code.upper()
    request.location_names.names.extend(location_names)

    try:
        response = geo_service.suggest_geo_target_constants(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    out = []
    for suggestion in response.geo_target_constant_suggestions:
        gtc = suggestion.geo_target_constant
        out.append(
            {
                "id": str(gtc.id),
                "name": gtc.name,
                "canonical_name": gtc.canonical_name,
                "country_code": gtc.country_code,
                "target_type": gtc.target_type,
                "reach": int(suggestion.reach),
                "searched_for": suggestion.search_term,
            }
        )
    return out


@targeting_mcp.tool(annotations=_WRITE)
def set_locations(
    customer_id: str,
    campaign_id: str,
    location_ids: List[str],
    negative: bool = False,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add location targeting (or exclusions) at CAMPAIGN level.

    WHEN TO USE: any campaign type except Demand Gen with
    upgraded_targeting=true, where geo lives on the ad group
    (targeting_set_locations_ad_group).
    PRECONDITIONS: ids from targeting_geo_lookup; the campaign must exist.
    SIDE EFFECTS: ADDS criteria, never replaces the set — the campaign
    targets the union; drop old ones with targeting_remove_criterion. The
    FIRST positive location narrows it from "everywhere" to that one.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: geo target constant ids (2276 = Germany), not account ids.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        location_ids: Geo target constant ids from targeting_geo_lookup.
        negative: True to EXCLUDE these locations instead of targeting them.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not location_ids:
        raise ToolError("location_ids list is empty")

    client = utils.get_googleads_client()
    cc_service = utils.get_googleads_service("CampaignCriterionService")

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for loc_id in location_ids:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
        criterion.location.geo_target_constant = f"geoTargetConstants/{loc_id}"
        if negative:
            criterion.negative = True
        request.operations.append(operation)

    try:
        response = cc_service.mutate_campaign_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "location_ids": location_ids,
        "negative": negative,
        "count": len(location_ids),
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(confirm, "targeting_set_locations", details)


# Shape of language_constant.code: a two- or three-letter language, with an
# optional region suffix ("en", "de", "zh_CN"). The codes go straight into a
# GAQL string literal, so anything outside this shape is rejected instead of
# escaped — no legitimate code needs a quote or a backslash.
_LANGUAGE_CODE = re.compile(r"\A[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?\Z")


def _language_code(code: str) -> str:
    """Validates a language code by shape and returns its canonical form."""
    text = str(code).strip()
    if not _LANGUAGE_CODE.match(text):
        raise ToolError(
            f"Invalid language code: {code!r} "
            "(expected e.g. 'en', 'de', 'zh_CN')"
        )
    for separator in ("_", "-"):
        if separator in text:
            language, region = text.split(separator, 1)
            return f"{language.lower()}{separator}{region.upper()}"
    return text.lower()


@targeting_mcp.tool(annotations=_WRITE)
def set_languages(
    customer_id: str,
    campaign_id: str,
    language_codes: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add language targeting to a campaign.

    WHEN TO USE: restricting to users whose Google interface language
    matches. It does NOT translate ads or filter by query language.
    PRECONDITIONS: codes are resolved to language constants first, so an
    unknown code fails the call before anything is written.
    SIDE EFFECTS: ADDS criteria, never replaces the set — drop old ones
    with targeting_remove_criterion.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        language_codes: Codes as Google Ads spells them, e.g. ["en", "de",
            "uk", "zh_CN"].
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not language_codes:
        raise ToolError("language_codes list is empty")
    codes = [_language_code(c) for c in language_codes]

    ga_service = utils.get_googleads_service("GoogleAdsService")
    codes_str = ", ".join(f"'{c}'" for c in codes)
    query = (
        "SELECT language_constant.id, language_constant.code, "
        "language_constant.name FROM language_constant "
        f"WHERE language_constant.code IN ({codes_str})"
    )
    try:
        rows = list(ga_service.search(customer_id=customer_id, query=query))
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    found = {
        row.language_constant.code: row.language_constant.id for row in rows
    }
    missing = [c for c in codes if c not in found]
    if missing:
        raise ToolError(f"Unknown language codes: {missing}")

    client = utils.get_googleads_client()
    cc_service = utils.get_googleads_service("CampaignCriterionService")

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for code in codes:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
        criterion.language.language_constant = (
            f"languageConstants/{found[code]}"
        )
        request.operations.append(operation)

    try:
        response = cc_service.mutate_campaign_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "languages": codes,
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(confirm, "targeting_set_languages", details)


@targeting_mcp.tool(annotations=_WRITE)
def set_ad_schedule(
    customer_id: str,
    campaign_id: str,
    schedule: List[Dict[str, Any]],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add an ad schedule (dayparting) to a campaign.

    WHEN TO USE: limiting when a campaign serves. Skip it while Smart
    Bidding is learning — every hour cut is data lost.
    PRECONDITIONS: the campaign must exist (mutate_list_campaigns).
    SIDE EFFECTS: once ANY schedule exists the campaign serves ONLY inside
    the listed windows, so the first call silently ends 24/7 serving.
    Windows are ADDED, never replaced (targeting_remove_criterion).
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: hours 0-24 in the ACCOUNT timezone, full hours only.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        schedule: Windows shaped
            {"day": "MONDAY", "start_hour": 8, "end_hour": 22}; day must be
            MONDAY..SUNDAY and 0 <= start_hour < end_hour <= 24.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not schedule:
        raise ToolError("schedule list is empty")
    for w in schedule:
        day = str(w.get("day", "")).upper()
        if day not in _DAYS:
            raise ToolError(f"day must be one of {_DAYS}, got: {w}")
        sh, eh = int(w.get("start_hour", -1)), int(w.get("end_hour", -1))
        if not (0 <= sh < eh <= 24):
            raise ToolError(
                f"Invalid hours in {w}: need 0 <= start_hour < end_hour <= 24"
            )

    client = utils.get_googleads_client()
    cc_service = utils.get_googleads_service("CampaignCriterionService")

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for w in schedule:
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
        criterion.ad_schedule.day_of_week = client.enums.DayOfWeekEnum[
            str(w["day"]).upper()
        ]
        criterion.ad_schedule.start_hour = int(w["start_hour"])
        criterion.ad_schedule.end_hour = int(w["end_hour"])
        criterion.ad_schedule.start_minute = client.enums.MinuteOfHourEnum.ZERO
        criterion.ad_schedule.end_minute = client.enums.MinuteOfHourEnum.ZERO
        request.operations.append(operation)

    try:
        response = cc_service.mutate_campaign_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "schedule": schedule,
        "windows": len(schedule),
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(confirm, "targeting_set_ad_schedule", details)


@targeting_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True)
)
def remove_criterion(
    customer_id: str,
    campaign_id: str,
    criterion_ids: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Remove campaign criteria (locations, languages, schedule windows).

    WHEN TO USE: replacing a targeting set — the set_* tools only ADD.
    Ad-group keywords: mutate_keywords_remove.
    PRECONDITIONS: criterion ids from targeting_list_criteria; they are per
    campaign, and a truncated list is not proof one is gone.
    SIDE EFFECTS: IRREVERSIBLE. Removing the LAST positive location widens
    the campaign back to "everywhere", so add the replacement first.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        criterion_ids: Numeric criterion ids to remove.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not criterion_ids:
        raise ToolError("criterion_ids list is empty")

    client = utils.get_googleads_client()
    cc_service = utils.get_googleads_service("CampaignCriterionService")

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for crit_id in criterion_ids:
        operation = client.get_type("CampaignCriterionOperation")
        operation.remove = (
            f"customers/{customer_id}/campaignCriteria/"
            f"{campaign_id}~{crit_id}"
        )
        request.operations.append(operation)

    try:
        response = cc_service.mutate_campaign_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "criterion_ids": criterion_ids,
    }
    if confirm:
        details["removed_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(confirm, "targeting_remove_criterion", details)


@targeting_mcp.tool(annotations=_READ)
def list_criteria(
    customer_id: str,
    campaign_id: str,
    limit: int = 500,
) -> Dict[str, Any]:
    """List campaign targeting criteria: locations, languages, schedule.

    WHEN TO USE: before targeting_remove_criterion (it needs criterion ids)
    or to check what a campaign already targets. Only LOCATION, LANGUAGE
    and AD_SCHEDULE rows are returned.
    Returns {"items": [...], "returned": n, "truncated": bool}. When
    truncated is true the campaign has more criteria than limit, so an id
    missing from items means "not listed", NOT "does not exist" — raise
    limit before concluding a criterion is already gone.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        limit: Max criteria returned (default 500).
    """
    customer_id = _clean_customer_id(customer_id)
    cap = int(limit)
    ga_service = utils.get_googleads_service("GoogleAdsService")
    query = (
        "SELECT campaign_criterion.criterion_id, campaign_criterion.type, "
        "campaign_criterion.negative, "
        "campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.language.language_constant, "
        "campaign_criterion.ad_schedule.day_of_week, "
        "campaign_criterion.ad_schedule.start_hour, "
        "campaign_criterion.ad_schedule.end_hour "
        "FROM campaign_criterion "
        f"WHERE campaign.id = {int(campaign_id)} "
        "AND campaign_criterion.type IN ('LOCATION', 'LANGUAGE', "
        "'AD_SCHEDULE') "
        "ORDER BY campaign_criterion.criterion_id "
        # One row past the cap: reading it back is how truncation is
        # detected, so the cut is reported instead of silently applied.
        f"LIMIT {cap + 1}"
    )
    try:
        rows = ga_service.search(customer_id=customer_id, query=query)
        out = []
        for row in rows:
            cc = row.campaign_criterion
            item: Dict[str, Any] = {
                "criterion_id": str(cc.criterion_id),
                "type": cc.type_.name,
                "negative": cc.negative,
            }
            if cc.type_.name == "LOCATION":
                item["geo_target"] = cc.location.geo_target_constant
            elif cc.type_.name == "LANGUAGE":
                item["language"] = cc.language.language_constant
            elif cc.type_.name == "AD_SCHEDULE":
                item["schedule"] = (
                    f"{cc.ad_schedule.day_of_week.name} "
                    f"{cc.ad_schedule.start_hour}:00-"
                    f"{cc.ad_schedule.end_hour}:00"
                )
            out.append(item)
        truncated = len(out) > cap
        items = out[:cap]
        return {
            "items": items,
            "returned": len(items),
            "truncated": truncated,
        }
    except GoogleAdsException as ex:
        _raise_tool_error(ex)


_AGE_RANGES = {
    "18_24": "AGE_RANGE_18_24",
    "25_34": "AGE_RANGE_25_34",
    "35_44": "AGE_RANGE_35_44",
    "45_54": "AGE_RANGE_45_54",
    "55_64": "AGE_RANGE_55_64",
    "65_UP": "AGE_RANGE_65_UP",
    "UNDETERMINED": "AGE_RANGE_UNDETERMINED",
}
_GENDERS = ("MALE", "FEMALE", "UNDETERMINED")
_DEVICES = ("MOBILE", "TABLET", "DESKTOP", "CONNECTED_TV")
_TARGETING_DIMENSIONS = (
    "KEYWORD",
    "AUDIENCE",
    "TOPIC",
    "GENDER",
    "AGE_RANGE",
    "PLACEMENT",
    "PARENTAL_STATUS",
    "INCOME_RANGE",
)


@targeting_mcp.tool(annotations=_WRITE)
def set_demographics(
    customer_id: str,
    ad_group_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    exclude_age_ranges: List[str] = [],
    exclude_genders: List[str] = [],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Exclude age ranges and/or genders at ad-group OR campaign level.

    WHEN TO USE: EXCLUSIONS only — it cannot target a demographic
    positively. Pass ad_group_id for Search/Display or campaign_id for
    PMax (its exclusions are campaign-level); exactly one is required.
    PRECONDITIONS: pass at least one of exclude_age_ranges /
    exclude_genders; re-excluding is a duplicate.
    SIDE EFFECTS: cuts reach. UNDETERMINED covers everyone Google could not
    classify, so excluding it removes real traffic.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group (Search/Display).
        campaign_id: The numeric id of the campaign (Performance Max).
        exclude_age_ranges: Age ranges to EXCLUDE. Valid: 18_24, 25_34,
            35_44, 45_54, 55_64, 65_UP, UNDETERMINED.
        exclude_genders: Genders to EXCLUDE. Valid: MALE, FEMALE,
            UNDETERMINED.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if (ad_group_id is None) == (campaign_id is None):
        raise ToolError("Pass exactly one of ad_group_id or campaign_id")
    if not exclude_age_ranges and not exclude_genders:
        raise ToolError("Pass exclude_age_ranges and/or exclude_genders")

    client = utils.get_googleads_client()
    at_campaign = campaign_id is not None

    if at_campaign:
        svc = utils.get_googleads_service("CampaignCriterionService")
        request = client.get_type("MutateCampaignCriteriaRequest")
    else:
        svc = utils.get_googleads_service("AdGroupCriterionService")
        request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    def _new_op():
        if at_campaign:
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = (
                f"customers/{customer_id}/campaigns/{campaign_id}"
            )
        else:
            op = client.get_type("AdGroupCriterionOperation")
            op.create.ad_group = (
                f"customers/{customer_id}/adGroups/{ad_group_id}"
            )
        op.create.negative = True
        return op

    for age in exclude_age_ranges or []:
        key = str(age).upper().replace("-", "_")
        if key not in _AGE_RANGES:
            raise ToolError(
                f"Unknown age range '{age}'; valid: {list(_AGE_RANGES)}"
            )
        op = _new_op()
        op.create.age_range.type_ = client.enums.AgeRangeTypeEnum[
            _AGE_RANGES[key]
        ]
        request.operations.append(op)

    for gender in exclude_genders or []:
        g = str(gender).upper()
        if g not in _GENDERS:
            raise ToolError(f"Unknown gender '{gender}'; valid: {_GENDERS}")
        op = _new_op()
        op.create.gender.type_ = client.enums.GenderTypeEnum[g]
        request.operations.append(op)

    try:
        if at_campaign:
            response = svc.mutate_campaign_criteria(request=request)
        else:
            response = svc.mutate_ad_group_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id) if ad_group_id else None,
        "campaign_id": str(campaign_id) if campaign_id else None,
        "excluded_age_ranges": exclude_age_ranges or [],
        "excluded_genders": exclude_genders or [],
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(confirm, "targeting_set_demographics", details)


@targeting_mcp.tool(annotations=_WRITE)
def set_device_bid_modifiers(
    customer_id: str,
    campaign_id: str,
    modifiers: Dict[str, float],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Set device bid modifiers on a campaign.

    WHEN TO USE: shifting spend between devices on a campaign that bids
    per click. Smart Bidding mostly ignores them except the 0 exclusion.
    PRECONDITIONS: the campaign must exist; re-sending a device that
    already has a modifier is a duplicate.
    SIDE EFFECTS: a modifier of 0 EXCLUDES the device outright, it does not
    mean "bid nothing".
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: a multiplier — 1.0 none, 1.2 = +20%, 0 = exclude.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        modifiers: Dict device -> modifier, e.g.
            {"MOBILE": 1.2, "DESKTOP": 0.9, "TABLET": 0}.
            Valid devices: MOBILE, TABLET, DESKTOP, CONNECTED_TV.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not modifiers:
        raise ToolError("modifiers dict is empty")

    client = utils.get_googleads_client()
    cc_service = utils.get_googleads_service("CampaignCriterionService")

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for device, modifier in modifiers.items():
        d = str(device).upper()
        if d not in _DEVICES:
            raise ToolError(f"Unknown device '{device}'; valid: {_DEVICES}")
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
        criterion.device.type_ = client.enums.DeviceEnum[d]
        criterion.bid_modifier = float(modifier)
        request.operations.append(operation)

    try:
        response = cc_service.mutate_campaign_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "modifiers": modifiers,
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(
        confirm, "targeting_set_device_bid_modifiers", details
    )


@targeting_mcp.tool(annotations=_WRITE)
def set_frequency_cap(
    customer_id: str,
    campaign_id: str,
    impressions: int,
    time_unit: str = "DAY",
    confirm: bool = False,
) -> Dict[str, Any]:
    """Set a campaign-level frequency cap (Video / Display / Demand Gen).

    WHEN TO USE: capping how often one user sees the ads; Search
    campaigns do not support caps.
    PRECONDITIONS: the campaign must exist and be a channel that supports
    caps, or the API rejects the update.
    SIDE EFFECTS: REPLACES the whole frequency_caps list — an existing cap
    is dropped, not merged. The period is always 1 unit long.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        impressions: Max impressions per user per period (>= 1).
        time_unit: DAY, WEEK or MONTH.
        confirm: False = dry-run preview (default), True = apply.
    """
    from google.protobuf import field_mask_pb2

    customer_id = _clean_customer_id(customer_id)
    time_unit = time_unit.upper()
    if time_unit not in ("DAY", "WEEK", "MONTH"):
        raise ToolError("time_unit must be DAY, WEEK or MONTH")
    if impressions < 1:
        raise ToolError("impressions must be >= 1")

    client = utils.get_googleads_client()
    campaign_service = utils.get_googleads_service("CampaignService")

    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
    entry = client.get_type("FrequencyCapEntry")
    entry.key.level = client.enums.FrequencyCapLevelEnum.CAMPAIGN
    entry.key.event_type = client.enums.FrequencyCapEventTypeEnum.IMPRESSION
    entry.key.time_unit = client.enums.FrequencyCapTimeUnitEnum[time_unit]
    entry.key.time_length = 1
    entry.cap = int(impressions)
    campaign.frequency_caps.append(entry)

    fm = field_mask_pb2.FieldMask(paths=["frequency_caps"])
    client.copy_from(operation.update_mask, fm)

    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = campaign_service.mutate_campaigns(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "cap": f"{impressions} impressions / {time_unit}",
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(confirm, "targeting_set_frequency_cap", details)


@targeting_mcp.tool(annotations=_WRITE)
def set_content_exclusions(
    customer_id: str,
    campaign_id: str,
    content_labels: List[str],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Exclude content categories (brand safety) on a campaign.

    WHEN TO USE: Display / Video / Demand Gen inventory filtering; Search
    campaigns have no content categories.
    PRECONDITIONS: the campaign must exist; an unknown label is refused
    before anything is sent.
    SIDE EFFECTS: ADDS exclusions, never removes any
    (targeting_remove_criterion does that); each one shrinks inventory.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        campaign_id: The numeric id of the campaign.
        content_labels: Categories to exclude, e.g. ["SEXUALLY_SUGGESTIVE",
            "PROFANITY", "TRAGEDY", "JUVENILE", "SOCIAL_ISSUES",
            "PARKED_DOMAIN", "BELOW_THE_FOLD", "LIVE_STREAMING_VIDEO"].
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not content_labels:
        raise ToolError("content_labels list is empty")

    client = utils.get_googleads_client()
    cc_service = utils.get_googleads_service("CampaignCriterionService")

    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for label in content_labels:
        key = str(label).upper()
        try:
            enum_val = client.enums.ContentLabelTypeEnum[key]
        except KeyError:
            raise ToolError(f"Unknown content label: {label}")
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = f"customers/{customer_id}/campaigns/{campaign_id}"
        criterion.negative = True
        criterion.content_label.type_ = enum_val
        request.operations.append(operation)

    try:
        response = cc_service.mutate_campaign_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "excluded_content": [c.upper() for c in content_labels],
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(
        confirm, "targeting_set_content_exclusions", details
    )


@targeting_mcp.tool(annotations=_WRITE)
def set_locations_ad_group(
    customer_id: str,
    ad_group_id: str,
    location_ids: List[str],
    negative: bool = False,
    confirm: bool = False,
) -> Dict[str, Any]:
    """Add location targeting (or exclusions) at AD GROUP level.

    WHEN TO USE: Demand Gen with upgraded_targeting=true (the default for
    API-created DG campaigns), where geo lives on the ad group and
    campaign geo is ignored. Everywhere else: targeting_set_locations.
    PRECONDITIONS: ids from targeting_geo_lookup; the ad group must exist.
    SIDE EFFECTS: ADDS criteria, never replaces the set. Removing AD-GROUP
    criteria is not exposed (targeting_remove_criterion is
    campaign-level) — drop them in the UI.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.
    UNITS & IDS: geo target constant ids (2840 = US), not account ids.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        location_ids: Geo target constant ids from targeting_geo_lookup.
        negative: True to EXCLUDE these locations instead of targeting them.
        confirm: False = dry-run preview (default), True = apply.
    """
    customer_id = _clean_customer_id(customer_id)
    if not location_ids:
        raise ToolError("location_ids list is empty")

    client = utils.get_googleads_client()
    agc_service = utils.get_googleads_service("AdGroupCriterionService")

    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = customer_id
    request.validate_only = not confirm

    for loc_id in location_ids:
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
        criterion.location.geo_target_constant = f"geoTargetConstants/{loc_id}"
        if negative:
            criterion.negative = True
        request.operations.append(operation)

    try:
        response = agc_service.mutate_ad_group_criteria(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "location_ids": location_ids,
        "negative": negative,
    }
    if confirm:
        details["created_resources"] = [
            r.resource_name for r in response.results
        ]
    return _preview_or_done(
        confirm, "targeting_set_locations_ad_group", details
    )


@targeting_mcp.tool(annotations=_WRITE)
def set_ad_group_target_restrictions(
    customer_id: str,
    ad_group_id: str,
    targeting_dimensions: List[str],
    observation_dimensions: List[str] = [],
    confirm: bool = False,
) -> Dict[str, Any]:
    """Set which ad group dimensions RESTRICT reach vs only observe.

    WHEN TO USE: switching a dimension between "Targeting"
    (bid_only=false, restricts who sees the ads) and "Observation"
    (bid_only=true, reporting and bid adjustments only).
    PRECONDITIONS: pass at least one dimension. Valid: KEYWORD, AUDIENCE,
    TOPIC, GENDER, AGE_RANGE, PLACEMENT, PARENTAL_STATUS, INCOME_RANGE.
    SIDE EFFECTS: REPLACES the whole target_restrictions list — anything
    left out reverts to its default, so send the complete set. Moving a
    dimension to Targeting can cut reach sharply.
    DRY-RUN: confirm=false (default) validates remotely, changes nothing;
    confirm=true applies.

    Args:
        customer_id: The client account id (digits only, no hyphens).
        ad_group_id: The numeric id of the ad group.
        targeting_dimensions: Dimensions in "Targeting" mode
            (bid_only=false), e.g. ["AUDIENCE", "TOPIC", "PLACEMENT"].
        observation_dimensions: Dimensions in "Observation" mode
            (bid_only=true), e.g. ["GENDER", "AGE_RANGE"].
        confirm: False = dry-run preview (default), True = apply.
    """
    from google.protobuf import field_mask_pb2

    customer_id = _clean_customer_id(customer_id)
    if not targeting_dimensions and not observation_dimensions:
        raise ToolError(
            "Pass targeting_dimensions and/or observation_dimensions"
        )

    client = utils.get_googleads_client()
    ad_group_service = utils.get_googleads_service("AdGroupService")

    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = f"customers/{customer_id}/adGroups/{ad_group_id}"

    def _restriction(dim: str, bid_only: bool):
        key = str(dim).upper()
        if key not in _TARGETING_DIMENSIONS:
            raise ToolError(
                f"Unknown targeting dimension '{dim}'; valid: "
                f"{_TARGETING_DIMENSIONS}"
            )
        r = client.get_type("TargetRestriction")
        r.targeting_dimension = client.enums.TargetingDimensionEnum[key]
        r.bid_only = bid_only
        return r

    for dim in targeting_dimensions:
        ad_group.targeting_setting.target_restrictions.append(
            _restriction(dim, False)
        )
    for dim in observation_dimensions or []:
        ad_group.targeting_setting.target_restrictions.append(
            _restriction(dim, True)
        )
    fm = field_mask_pb2.FieldMask(
        paths=["targeting_setting.target_restrictions"]
    )
    client.copy_from(operation.update_mask, fm)

    request = client.get_type("MutateAdGroupsRequest")
    request.customer_id = customer_id
    request.operations.append(operation)
    request.validate_only = not confirm

    try:
        response = ad_group_service.mutate_ad_groups(request=request)
    except GoogleAdsException as ex:
        _raise_tool_error(ex)

    details: Dict[str, Any] = {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "targeting": [d.upper() for d in targeting_dimensions],
        "observation": [d.upper() for d in observation_dimensions or []],
    }
    if confirm:
        details["updated_resource"] = response.results[0].resource_name
    return _preview_or_done(
        confirm, "targeting_set_ad_group_target_restrictions", details
    )
