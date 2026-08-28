# google-ads-mcp-extended

## Overview

This repository extends the read-only Google Ads MCP server with **write
tools** for campaign management. The server exposes **101 tools** across 16
namespaces: 17 read-only tools (reporting, lookups and listings) and 84 write
tools, all of which default to a dry-run preview (see
[Safety model](#safety-model)).

Supported areas: Search, Demand Gen, Performance Max, Shopping, Video and
Display campaigns; ad groups and ads; keywords and negative keyword lists;
extensions (assets); targeting; tracking templates; audiences; Google
recommendations and Smart Bidding hints; labels; and campaign experiments.

### Changed and added files relative to upstream

- New tool modules under `ads_mcp/tools/`: `mutate.py`, `demand_gen.py`,
  `pmax.py`, `extensions.py`, `targeting.py`, `shopping.py`, `video.py`,
  `display.py`, `tracking.py`, `audiences.py`, `optimize.py`, `negatives.py`,
  `experiments.py`.
- New module `ads_mcp/safe_fetch.py` — hardened fetching of user-supplied
  image sources (HTTPS-only, no redirects, private addresses refused).
- New module `ads_mcp/resources/fetch_cache.py` — shared timeout, size caps
  and TTL caching for the documentation resources.
- Modified: `ads_mcp/config.py` and `ads_mcp/tools_config.yaml` (namespace
  registration and configuration semantics), `ads_mcp/utils.py` (shared
  helpers, client caching), `ads_mcp/coordinator.py` (mount-time tool
  filtering), the four `ads_mcp/resources/` modules (timeouts and caching),
  `ads_mcp/update_references.py` (console-script import fix), `README.md`,
  `Dockerfile`, `.gitignore`, plus tests.
- New file: `.dockerignore`.
- New skill: `ads_mcp/skills/account-performance-diagnostics`.

Upstream updates can still be merged normally; the write tools live in
separate modules.

## Safety model

Every write tool has a `confirm` parameter:

- `confirm=false` (default) — nothing is changed. For most write tools the
  request is sent to Google Ads with `validate_only=true`, so Google fully
  validates the operation and the tool returns a preview of what would
  happen.
- `confirm=true` — the operation is applied.

Five tools send **nothing** to Google in dry-run mode; their previews are
built locally and may still fail on apply. For
`optimize_recommendation_apply` and `optimize_recommendation_dismiss` the
underlying API requests have no `validate_only` field, so remote validation
is impossible. For `experiments_experiment_create`,
`experiments_experiment_end` and `experiments_experiment_promote` the API
does support `validate_only`, but experiment creation and scheduling cannot
be meaningfully validated end-to-end in a dry-run, so these tools skip the
call entirely. All five report `"validated": false` in their previews.

Every preview states which guarantee it carries: the result includes
`"validated": true` when Google Ads validated the request, and
`"validated": false` when no request was sent.

The tools that send several operations at once (`mutate_keywords_add` and
the `_batch` tools) have an asymmetric dry-run: the API rejects
`partial_failure` together with `validate_only`, so the preview is atomic —
one bad entry fails the whole preview — while the apply runs with
`partial_failure=true` and some operations can succeed while others fail.
Their results therefore report per-entry outcomes, and the `_batch` tools
add `requested` / `succeeded` / `failed` counts.

An accidental change is therefore a two-step mistake at minimum: the
assistant first shows a preview, and only a second call with `confirm=true`
touches the account. Tools that can remove objects are annotated with
`destructiveHint=true`. Recommendation: if your MCP client supports it, set
write tools to require confirmation before every call (**Ask first**).

## Tools by namespace

Tool names below include the default namespace prefix from
`ads_mcp/tools_config.yaml`. Tools marked *read-only* never modify the
account.

Cross-cutting parameters shared by several tools:

- `start_date` / `end_date` (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`) on all
  `*_campaign_create` tools.
- `tracking_url_template` at ad level on `mutate_ad_create_rsa`, the Demand
  Gen image/video/carousel ads, `display_ad_create_responsive` and
  `video_ad_create_responsive`.
- `negative: true` on `demandgen_audience_attach` (ad group level) and
  `audiences_campaign_audience_attach` (campaign level) to attach an audience
  as an exclusion.

### customers

| Tool | Description |
|---|---|
| `customers_list_accessible_customers` | Read-only: ids of customer accounts directly accessible to the authenticated user. |

### search

| Tool | Description |
|---|---|
| `search_search` | Read-only: fetches data from any Google Ads resource via GAQL (fields, conditions, orderings, optional limit). |

### metadata

| Tool | Description |
|---|---|
| `metadata_get_resource_metadata` | Read-only: selectable, filterable and sortable fields for a resource type, e.g. "campaign". |
| `metadata_get_field_details` | Read-only: data type, enum values and resource compatibility for specific Google Ads fields. |

### mutate

| Tool | Description |
|---|---|
| `mutate_campaign_create` | Creates a campaign plus a dedicated daily budget. Types: SEARCH, DISPLAY, SHOPPING, VIDEO, PERFORMANCE_MAX, DEMAND_GEN. Bidding: Maximize Conversions (+target CPA), Maximize Conversion Value (+target ROAS), Maximize Clicks, Manual CPC. Created PAUSED by default. |
| `mutate_campaign_update_status` | Pauses, enables or removes a campaign (ENABLED, PAUSED, REMOVED). |
| `mutate_campaign_update_status_batch` | Pauses or enables up to 100 campaigns in one request (ENABLED/PAUSED only; REMOVED stays on the single-campaign tool). |
| `mutate_campaign_set_target_roas` | Sets Target ROAS on a Maximize Conversion Value campaign. |
| `mutate_campaign_set_merchant` | Links a Merchant Center feed to an existing campaign (PMax/Shopping). |
| `mutate_campaign_update_settings` | Updates campaign network settings, geo target type and asset automation / AI Max settings. |
| `mutate_campaign_rename` | Renames an existing campaign. |
| `mutate_campaign_budget_update` | Changes the daily budget of a campaign; warns when the budget is shared. |
| `mutate_campaign_budget_update_batch` | Changes the daily budget of up to 100 campaigns in one request; collapses campaigns sharing a budget and rejects conflicting amounts. |
| `mutate_ad_group_create` | Creates a SEARCH_STANDARD ad group in an existing campaign. |
| `mutate_ad_group_update` | Updates an ad group: status, max CPC bid and/or name. |
| `mutate_keywords_ideas` | Read-only: keyword ideas from Google Keyword Planner. |
| `mutate_keywords_add` | Adds keywords or negative keywords (EXACT/PHRASE/BROAD) to an ad group. |
| `mutate_keywords_remove` | Removes keywords from an ad group by criterion id. Irreversible. |
| `mutate_ad_create_rsa` | Creates a Responsive Search Ad (3-15 headlines, 2-4 descriptions, limits validated). |
| `mutate_ad_update_status` | Pauses, enables or removes an ad of any type. |
| `mutate_list_campaigns` | Read-only: campaigns with id, name, status and budget. |
| `mutate_campaign_set_conversion_goals` | Sets campaign-specific standard conversion goal categories (e.g. only Purchases biddable). |
| `mutate_campaign_set_custom_conversion_goal` | Points a campaign at a custom conversion goal (disables category goals). |

### demandgen

| Tool | Description |
|---|---|
| `demandgen_asset_upload_image` | Uploads an image as an Asset. HTTPS URLs only (redirects are not followed); local file paths are honoured only when `GOOGLE_ADS_MCP_ALLOW_LOCAL_FILES=1` is set. JPEG/PNG, 5 MB limit. |
| `demandgen_asset_create_youtube_video` | Registers a YouTube video as an Asset. |
| `demandgen_list_assets` | Read-only: IMAGE / YOUTUBE_VIDEO assets in the account. |
| `demandgen_campaign_create` | Demand Gen campaign plus budget (Max Conversions/tCPA, Max Conversion Value/tROAS, Max Clicks). |
| `demandgen_campaign_update_bidding` | Updates tCPA/tROAS of an existing Demand Gen campaign. |
| `demandgen_campaign_set_targeting_level` | Sets the targeting level (campaign vs ad group) of an existing Demand Gen campaign. |
| `demandgen_ad_group_create` | Ad group in a Demand Gen campaign, optionally with channel controls. |
| `demandgen_ad_group_update_channels` | Changes the channel controls (placements) of an existing ad group. |
| `demandgen_audience_attach` | Attaches an existing Audience to an ad group, as targeting or as an exclusion. |
| `demandgen_ad_create_image` | Image ad (multi-asset: landscape + square + logo + texts). |
| `demandgen_ad_create_video` | Video ad: YouTube video + headlines/long headlines/descriptions + logo, optional `call_to_action` button. |
| `demandgen_ad_update_asset_optimization` | Toggles the "Asset optimization" settings of an ad. |
| `demandgen_ad_create_carousel` | Carousel ad (2-10 swipeable cards). |

### pmax

| Tool | Description |
|---|---|
| `pmax_campaign_create` | Performance Max campaign plus budget (Max Conversions/tCPA or Max Conversion Value/tROAS), optional Merchant Center feed. |
| `pmax_campaign_update_bidding` | Updates tCPA/tROAS of a PMax campaign. |
| `pmax_asset_group_create` | Complete asset group in one request: texts created inline, images/videos by asset id. Validates Google's minimums. |
| `pmax_asset_group_update` | Status, name and/or final URL of an asset group. |
| `pmax_asset_group_add_texts` | Adds headlines/descriptions to an existing asset group. |
| `pmax_asset_group_add_media` | Links existing image/video assets to an asset group. |
| `pmax_asset_group_remove_asset` | Unlinks an asset from a group (the asset itself stays in the account). |
| `pmax_signal_attach` | Audience signal or search theme for an asset group. |
| `pmax_asset_group_set_listing_filter` | Listing group filter tree subdivided by a product custom label (include values, exclude the rest). |
| `pmax_asset_group_set_all_products` | Root "All products" listing group filter for retail feeds. |
| `pmax_list_asset_groups` | Read-only: asset groups with ad strength and statuses. |

### extensions

| Tool | Description |
|---|---|
| `extensions_add_sitelinks` | Sitelinks for a campaign (text + URL + descriptions, limits validated). |
| `extensions_add_callouts` | Callouts (short USP phrases, max 25 characters). |
| `extensions_add_structured_snippets` | Structured snippets (header + 3-10 values). |
| `extensions_attach_assets` | Links EXISTING assets by id (SITELINK/CALLOUT/STRUCTURED_SNIPPET/BUSINESS_NAME/BUSINESS_LOGO) — for cloning without duplication. |
| `extensions_remove_campaign_asset` | Unlinks an extension asset from a campaign (asset kept). |
| `extensions_list_campaign_assets` | Read-only: extension assets linked to a campaign, with asset ids. |

### targeting

| Tool | Description |
|---|---|
| `targeting_geo_lookup` | Read-only: geo target ids by location names. |
| `targeting_set_locations` | Location targeting or exclusions on a campaign. |
| `targeting_set_locations_ad_group` | Location targeting or exclusions at ad group level. |
| `targeting_set_languages` | Language targeting (by codes such as en/de/fr; ids resolved automatically). |
| `targeting_set_ad_schedule` | Ad schedule (day + hours, in the account time zone). |
| `targeting_set_demographics` | Excludes age ranges and/or genders at ad group or campaign level. |
| `targeting_set_device_bid_modifiers` | Device bid modifiers on a campaign. |
| `targeting_set_frequency_cap` | Campaign-level frequency cap (Video / Display / Demand Gen). |
| `targeting_set_content_exclusions` | Excludes content categories (brand safety). |
| `targeting_set_ad_group_target_restrictions` | Sets which targeting dimensions restrict reach (Targeting) vs only report (Observation). |
| `targeting_remove_criterion` | Removes targeting criteria. |
| `targeting_list_criteria` | Read-only: current campaign targeting with criterion ids. |

### shopping

| Tool | Description |
|---|---|
| `shopping_campaign_create` | Standard Shopping campaign (Merchant Center id, feed label, priority) plus budget. |
| `shopping_ad_group_create` | SHOPPING_PRODUCT_ADS ad group. |
| `shopping_ad_create_product` | Product ad in a Shopping ad group. |
| `shopping_ad_group_set_item_listing` | Listing group tree partitioned by item id. |
| `shopping_ad_group_set_all_products` | Root "All products" listing group. |

### video

| Tool | Description |
|---|---|
| `video_campaign_create` | ALWAYS FAILS: video campaigns cannot be created via the API — use `demandgen_campaign_create` instead. |
| `video_ad_group_create` | VIDEO_RESPONSIVE ad group. |
| `video_ad_create_responsive` | Responsive video ad (YouTube video + texts). |

### display

| Tool | Description |
|---|---|
| `display_campaign_create` | Display campaign (GDN) plus budget. |
| `display_ad_group_create` | DISPLAY_STANDARD ad group. |
| `display_ad_create_responsive` | Responsive Display Ad (texts + images, optional video/logo/CTA). |

### tracking

| Tool | Description |
|---|---|
| `tracking_campaign_set_tracking` | Sets the tracking URL template and/or final URL suffix of a campaign. Passing `""` clears a field; the preview lists it under `will_clear`. |
| `tracking_account_set_tracking` | Account-level tracking template / final URL suffix; same `""`-clears semantics. |
| `tracking_list_tracking` | Read-only: account-level and per-campaign tracking templates / suffixes. |

### audiences

| Tool | Description |
|---|---|
| `audiences_create` | Combined Audience from demographic + segment dimensions. |
| `audiences_custom_segment_create` | Custom segment (custom audience) from keywords and/or URLs. |
| `audiences_user_list_create_visitors` | Rule-based remarketing list of page visitors matched by URL rule. |
| `audiences_campaign_audience_attach` | Attaches a user list or a combined Audience to a campaign, as targeting or exclusion. |
| `audiences_list_audiences` | Read-only: audiences available in the account. |

### optimize

| Tool | Description |
|---|---|
| `optimize_recommendations_list` | Read-only: Google's optimization recommendations. |
| `optimize_recommendation_apply` | Applies recommendations (with their default parameters). |
| `optimize_recommendation_dismiss` | Dismisses recommendations. |
| `optimize_change_history` | Read-only: recent account changes — who changed what and when. |
| `optimize_seasonality_adjustment_create` | Seasonality adjustment: tells Smart Bidding to expect a temporary conversion rate change. |
| `optimize_data_exclusion_create` | Data exclusion: tells Smart Bidding to ignore a date range. |
| `optimize_label_create` | Creates a label. |
| `optimize_label_apply` | Applies an existing label to campaigns and/or ad groups. |

### negatives

| Tool | Description |
|---|---|
| `negatives_add_campaign_keywords` | Campaign-level negative keywords, added directly to a campaign. |
| `negatives_shared_set_create` | Creates a shared negative keyword list. |
| `negatives_shared_set_add_keywords` | Adds negative keywords to a shared list. |
| `negatives_attach_to_campaigns` | Attaches a shared negative keyword list to campaigns. |
| `negatives_list_shared_sets` | Read-only: shared negative keyword lists with ids and usage counts. |

### experiments

| Tool | Description |
|---|---|
| `experiments_experiment_create` | Creates and schedules a campaign experiment (A/B test with traffic split). |
| `experiments_experiments_list` | Read-only: experiments with status and their arm campaigns. |
| `experiments_experiment_end` | Ends a running experiment. |
| `experiments_experiment_promote` | Promotes a winning experiment to the base campaign. |

## Context-weight profiles

Every enabled namespace's tool schemas are sent to the LLM on every
`tools/list` call. The bundled `ads_mcp/tools_config.yaml` enables all 16
namespaces by default (repo neutrality: no namespace is singled out as
optional). If your use case does not need the full surface, you can point
the server at a smaller configuration instead — see
[Configuring and Namespacing Tools](README.md#configuring-and-namespacing-tools)
for the resolution order and syntax; the short version is: save a
`namespaces:` config to a file and set the `GOOGLE_ADS_MCP_TOOLS_CONFIG`
environment variable to its path. That path is read at server startup, so
switching or editing profiles only needs a client restart, never a
reinstall — unlike edits to the bundled `ads_mcp/tools_config.yaml`, which
ships inside the installed package.

Approximate `tools/list` weight per namespace, measured on v0.2.0 (the two
batch tools and wave-2 schema work shift these slightly):

| Namespace | Tools | Approx. size |
|---|---|---|
| mutate | 17 | 23.3 KB |
| demandgen | 13 | 21.3 KB |
| pmax | 11 | 16.5 KB |
| targeting | 12 | 13.5 KB |
| optimize | 8 | 8.7 KB |
| search | 1 | 7.3 KB |
| shopping | 5 | 6.6 KB |
| extensions | 6 | 6.3 KB |
| audiences | 5 | 6.0 KB |
| display | 3 | 5.6 KB |
| negatives | 5 | 4.4 KB |
| video | 3 | 4.3 KB |
| experiments | 4 | 4.2 KB |
| tracking | 3 | 3.5 KB |
| metadata | 1 | 1.3 KB |
| customers | 1 | 0.7 KB |

[examples/tool_configs/](examples/tool_configs/) has three ready-made
profiles built from this table (each with header comments on activation and
every disabled namespace listed, commented out, for a quick edit to
re-enable it):

| Profile | Namespaces enabled | Approx. size | vs. full |
|---|---|---|---|
| `analytics-readonly.yaml` | customers, search, metadata | ≈9.3 KB | −93% |
| `analytics-plus.yaml` | + optimize | ≈18.0 KB | −87% |
| `ops-core.yaml` | customers, search, metadata, mutate, negatives, targeting, extensions, optimize | ≈65.5 KB | −51% |

`analytics-plus` trades a small size increase for change-history and
recommendations reads, which live in `optimize` — that namespace also
carries write tools (label apply, recommendation apply/dismiss, seasonality
and data-exclusion create), all gated behind the same dry-run `confirm`
parameter as every other write tool (see "Safety model" above).

## Install

1. Requirements: Python 3.10+, pipx (`python3 -m pip install --user pipx`),
   the gcloud CLI.
2. One-time authorization (requires the `client_secret.json` of an OAuth
   client from your project's Google Cloud Console):

   ```
   gcloud auth application-default login \
     --client-id-file=/path/to/client_secret.json \
     --scopes="https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform"
   ```

3. Add a block to your MCP client configuration (inside `mcpServers`):

   ```json
   "google-ads": {
     "command": "PATH_TO_PYTHON3",
     "args": [
       "-m", "pipx", "run", "--no-cache",
       "--spec", "git+https://github.com/YOUR_ORG/google-ads-mcp-extended.git",
       "google-ads-mcp"
     ],
     "env": {
       "GOOGLE_APPLICATION_CREDENTIALS": "/Users/USERNAME/.config/gcloud/application_default_credentials.json",
       "GOOGLE_PROJECT_ID": "YOUR_GCP_PROJECT_ID",
       "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN",
       "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID"
     }
   }
   ```

   `PATH_TO_PYTHON3` is the output of `which python3`. For a local test
   without GitHub, replace the `--spec` value with the path to this folder.

4. Fully restart the MCP client (not just the window).

## Limitations

- Demand Gen channel controls are supported at ad group level
  (`demandgen_ad_group_create` / `demandgen_ad_group_update_channels`:
  selected channels or a channel strategy).
- PMax listing group filters support the root "All products" node
  (`pmax_asset_group_set_all_products`) and trees subdivided by a single
  product custom label (`pmax_asset_group_set_listing_filter`); arbitrary
  multi-level subdivision trees are not supported.
- Editing the texts of an existing ad means creating a new ad — Google Ads
  API ad content is immutable.
- `demandgen_ad_create_video`: the `call_to_action` enum is validated on
  dry-run, but the CTA asset is only created and linked on `confirm=true`,
  so the dry-run validates the ad payload without the CTA link.
- The developer token must have Basic access or higher.

## Behaviour changes

Notable changes compared to earlier revisions of these tools:

- **Image upload is HTTPS-only.** `demandgen_asset_upload_image` rejects
  `http://` URLs, never follows redirects, and refuses hosts that resolve to
  private, loopback, link-local or otherwise non-public addresses. Local
  file paths only work when the server is started with
  `GOOGLE_ADS_MCP_ALLOW_LOCAL_FILES=1` (appropriate for a local stdio
  server, never for a hosted one).
- **`mutate_keywords_add` no longer auto-exempts policy violations.**
  `auto_exempt` now defaults to `false`; keywords blocked by policy are
  reported in `policy_failed`, each with an `"exemptible"` flag, instead of
  being silently re-sent with a policy exemption. Pass `auto_exempt=true`
  explicitly to assert that flagged violations are false positives.
- **List tools apply default limits and report truncation.** Dict-shaped
  results carry a `"truncated"` flag; list-shaped ones return a wrapper dict
  with the item list (key `"items"`, or `"asset_groups"` /
  `"experiments"` for the PMax and experiments listings) plus `"returned"`
  and `"truncated"`. An entry missing
  from a truncated listing means "not listed", not "does not exist" — raise
  the `limit` or narrow the query before concluding something is absent.
- **An empty `namespaces:` block enables nothing.** In `tools_config.yaml`,
  a `namespaces` key that is present but empty disables all tools; omit the
  key entirely to keep the enable-all default.
- **Tracking previews disclose clears.** When `""` is passed to the tracking
  tools, the dry-run preview lists the fields about to be wiped under
  `"will_clear"`.
- **Falsy values are no longer silently ignored.** Where passing `0` or `""`
  used to be dropped from the update, it now either performs an explicit,
  documented clear or raises a validation error: `cpc_bid` and bidding
  targets must be greater than 0, and names must be non-empty.

## Campaign cloning checklist

When recreating a campaign from a template campaign, read each layer from
the template via `search_search` first, then reproduce it. The order below
matters, and nothing should be left "at defaults" — API defaults differ from
UI defaults.

**Layer 1 — campaign:**

1. `campaign`: type, bidding (+tCPA/tROAS), budget (amount, delivery,
   shared?).
2. `campaign.tracking_url_template` + `final_url_suffix` +
   `url_custom_parameters` — critical; analytics breaks without them.
3. `conversion_goal_campaign_config` — custom goal or customer defaults. If
   custom: `mutate_campaign_set_custom_conversion_goal` (which also disables
   category goals).
4. `campaign_criterion`: LOCATION (+ `geo_target_type_setting`
   PRESENCE/INTEREST), LANGUAGE, DEVICE, ad schedule, content labels.
5. Demand Gen: `demand_gen_campaign_settings.upgraded_targeting` —
   `demandgen_campaign_create` always sets it to `false` (campaign-level
   geo, matching the UI default). The flag is IMMUTABLE: a wrong value
   cannot be fixed by an update, only by recreating the campaign.

**Layer 2 — ad groups:**

6. `ad_group.demand_gen_ad_group_settings.channel_controls` — read the
   template's mode (SELECTED_CHANNELS vs CHANNEL_STRATEGY) and its exact
   channel set instead of assuming a default; different ad groups in the
   same campaign often use different channel sets (for example, video ad
   groups vs image ad groups).
7. `ad_group_criterion` with type=AUDIENCE — the audience attachment.
   Demographic restrictions live INSIDE the Audience object; standalone
   age/gender criteria are not needed and are rejected on Demand Gen.
8. `ad_group.targeting_setting.target_restrictions` — copy each dimension's
   Targeting vs Observation setting from the template via
   `targeting_set_ad_group_target_restrictions`; do not assume which
   dimensions restrict reach.

**Layer 3 — ads:**

9. All ad types in the group with their full content: texts
   (headlines/long_headlines/descriptions/business_name/CTA), asset ids of
   ALL image formats (landscape, square, portrait, logo), videos,
   `final_urls` (check http vs https).
10. Statuses: campaign/ad groups/ads = PAUSED until manually reviewed.

**Layer 4 — verification (mandatory):**

11. Re-read what was created and diff it against the template: campaign
    fields, criteria, goals (`biddable=true` should return 0 rows when a
    custom goal is set), channel controls, targeting settings, ad counts.

**Known API pitfalls:** category conversion goals are all enabled by
default; API-created Demand Gen campaigns default to
`upgraded_targeting=true`; the REMOVED status can only be reached through a
remove operation; ads are immutable (replacing texts = new ad + pausing the
old one).

### Known cloning limits (verify manually)

- **Shared budgets**: `campaign_create` always creates a dedicated budget.
  If the template uses a shared budget, the clone will not inherit it.
- **Portfolio bid strategies**: only standard campaign bidding is
  supported. Campaigns on portfolio strategies cannot be cloned exactly.
- **Pinned headlines**: pinning headlines to positions (`pinned_field`) in
  RSA/Demand Gen ads is not carried over — check pinning manually after
  cloning.

### Retail PMax (Merchant Center) notes

1. **Merchant Center id / feed_label** can only be set at creation time
   (`pmax_campaign_create merchant_id=, feed_label=`). On an existing PMax
   campaign the field is immutable — changing it means recreating the
   campaign. `mutate_campaign_set_merchant` attempts an update, but Google
   usually rejects it.
2. **Listing group filters** are rarely "all products". Trees subdivided by
   a custom label are common: use `pmax_asset_group_set_listing_filter`
   with `custom_label_index`, `include_values` and `exclude_others`.
   API requirements: `listing_source=SHOPPING` on EVERY
   node, and every SUBDIVISION must have an "everything else" node — the
   tool creates it as UNIT_EXCLUDED with an empty value.
3. **Automatically created assets** (`asset.source = AUTOMATICALLY_CREATED`)
   cannot be linked manually
   (CANNOT_LINK_TO_AUTOMATICALLY_CREATED_ASSET). When copying media, filter
   to `ADVERTISER` assets only.
4. **Asset-group persona audiences** (`audience.scope = ASSET_GROUP`, names
   like AssetGroupPersona_*) are not reusable — recreate them via
   `audiences_create` with the same segment composition and attach as a
   signal.
5. **Conversion goals**: if
   `conversion_goal_campaign_config.custom_conversion_goal` is empty, the
   campaign uses account-default goals — do not attach a custom goal.
6. **PMax geo type** defaults to PRESENCE_OR_INTEREST — if the template
   uses PRESENCE, set it explicitly via
   `mutate_campaign_update_settings positive_geo_target_type=PRESENCE`.
7. **Asset automation** (image extraction/enhancement, video enhancement) —
   compare `campaign.asset_automation_settings` and set the
   `image_extraction`, `image_enhancement` and `video_enhancement` options
   via `mutate_campaign_update_settings`.
8. **PMax descriptions**: at least one must be 60 characters or shorter.
9. **Campaign-specific standard goals**: if
   `conversion_goal_campaign_config.goal_config_level = CAMPAIGN` and
   `custom_conversion_goal` is empty, the campaign uses standard category
   goals at campaign level (for example, only Purchases biddable). Set this
   via `mutate_campaign_set_conversion_goals` with
   `biddable_categories=["PURCHASE"]`, which switches the level to CAMPAIGN
   and makes only the listed categories biddable. Do not confuse this with
   a custom goal (a separate tool).
10. **LANDSCAPE_LOGO**: Brand Guidelines can include a LANDSCAPE_LOGO (4:1)
    in addition to LOGO (1:1). They are separate campaign asset field
    types; copy both via `extensions_attach_assets`.
