"""Build dashboard_data.json for the Google Ads dashboard.

Runs a catalog of GAQL reports against every client account under the manager
account, so the page shows everything the Google Ads API exposes for them:
account settings and access, campaigns with budgets and bidding, daily
performance for a year, ad groups, keywords, search terms, ads and their
assets, Performance Max asset groups, Shopping products, geography, devices,
demographics, audiences, conversion actions, recommendations, the change
history and more. A report that fails is recorded with its error instead of
failing the build, so the Coverage section of the page lists what worked.

    python google-ads/dashboard/build_data.py
    python google-ads/dashboard/build_data.py --customer 8654921686 --detail-days 30
    python google-ads/dashboard/build_data.py --login-customer-id 4233688880 --out data.json

Auth is Application Default Credentials; the identity must be a user on the
manager account (see ../ads_api.py). Nothing here is a secret.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from ads_api import API_VERSION, DEFAULT_MANAGER_ID, AdsApiError, GoogleAds, explain_api_error  # noqa: E402

CORE = "metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, metrics.conversions_value"
CORE_ALL = CORE + ", metrics.all_conversions, metrics.all_conversions_value"


def catalog(w):
    """The report catalog. `w` carries the date windows as GAQL fragments.

    Each entry: key, section, title, what it answers, GAQL. Optional:
    per_campaign_type (run once per campaign of that type, {cid} substituted).
    """
    d, t, c = w["detail"], w["trend"], w["campaign_daily"]
    return [
        # ---- account ------------------------------------------------------------
        dict(key="customer", section="account", title="Account", what="Currency, time zone, status, auto-tagging, optimisation score, conversion tracking.",
             gaql="SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, customer.status, customer.manager, "
                  "customer.test_account, customer.auto_tagging_enabled, customer.optimization_score, "
                  "customer.conversion_tracking_setting.conversion_tracking_status, customer.conversion_tracking_setting.conversion_tracking_id, "
                  "customer.tracking_url_template, customer.final_url_suffix FROM customer"),
        dict(key="users", section="account", title="Users and roles", what="Who can sign in to the account and with what access level.",
             gaql="SELECT customer_user_access.user_id, customer_user_access.email_address, customer_user_access.access_role, "
                  "customer_user_access.access_creation_date_time FROM customer_user_access"),
        dict(key="invitations", section="account", title="Pending invitations", what="User invitations not yet accepted.",
             gaql="SELECT customer_user_access_invitation.email_address, customer_user_access_invitation.access_role, "
                  "customer_user_access_invitation.invitation_status, customer_user_access_invitation.creation_date_time FROM customer_user_access_invitation"),
        dict(key="managers", section="account", title="Manager accounts", what="Manager accounts linked above this account.",
             gaql="SELECT customer_manager_link.manager_customer, customer_manager_link.manager_link_id, customer_manager_link.status FROM customer_manager_link"),
        dict(key="product_links", section="account", title="Linked products", what="Merchant Center, Google Analytics and other accounts linked to Ads.",
             gaql="SELECT product_link.product_link_id, product_link.type, product_link.merchant_center.merchant_center_id FROM product_link"),
        dict(key="conversion_goals", section="conversions", title="Account conversion goals", what="Which conversion categories are biddable account-wide.",
             gaql="SELECT customer_conversion_goal.category, customer_conversion_goal.origin, customer_conversion_goal.biddable FROM customer_conversion_goal"),
        dict(key="conversion_actions", section="conversions", title="Conversion actions", what="Every conversion action, its type, status, counting and value settings.",
             gaql="SELECT conversion_action.id, conversion_action.name, conversion_action.category, conversion_action.type, conversion_action.status, "
                  "conversion_action.origin, conversion_action.primary_for_goal, conversion_action.include_in_conversions_metric, conversion_action.counting_type, "
                  "conversion_action.click_through_lookback_window_days, conversion_action.view_through_lookback_window_days, "
                  "conversion_action.attribution_model_settings.attribution_model, conversion_action.value_settings.default_value, "
                  "conversion_action.value_settings.always_use_default_value FROM conversion_action ORDER BY conversion_action.status"),
        dict(key="campaign_conversion_goals", section="conversions", title="Campaign conversion goals", what="Per-campaign overrides of the account goals.",
             gaql="SELECT campaign.name, campaign_conversion_goal.category, campaign_conversion_goal.origin, campaign_conversion_goal.biddable "
                  "FROM campaign_conversion_goal WHERE campaign.status != 'REMOVED' LIMIT 1000"),
        dict(key="offline_uploads", section="conversions", title="Offline conversion uploads", what="Health of offline conversion imports per client.",
             gaql="SELECT offline_conversion_upload_client_summary.client, offline_conversion_upload_client_summary.status, "
                  "offline_conversion_upload_client_summary.total_event_count, offline_conversion_upload_client_summary.successful_event_count, "
                  "offline_conversion_upload_client_summary.success_rate, offline_conversion_upload_client_summary.last_upload_date_time "
                  "FROM offline_conversion_upload_client_summary"),
        dict(key="auto_apply", section="account", title="Auto-apply recommendations", what="Recommendation types Google is allowed to apply automatically.",
             gaql="SELECT recommendation_subscription.type, recommendation_subscription.status, recommendation_subscription.create_date_time, "
                  "recommendation_subscription.modify_date_time FROM recommendation_subscription"),
        dict(key="account_negatives", section="account", title="Account-level exclusions", what="Placements, content labels and negative keyword lists excluded account-wide.",
             gaql="SELECT customer_negative_criterion.id, customer_negative_criterion.type, customer_negative_criterion.content_label.type, "
                  "customer_negative_criterion.placement.url, customer_negative_criterion.youtube_channel.channel_id, "
                  "customer_negative_criterion.negative_keyword_list.shared_set FROM customer_negative_criterion LIMIT 500"),
        dict(key="billing", section="account", title="Billing setup", what="Payments account and billing status.",
             gaql="SELECT billing_setup.id, billing_setup.status, billing_setup.payments_account_info.payments_account_name, "
                  "billing_setup.payments_account_info.payments_profile_name, billing_setup.start_date_time FROM billing_setup"),
        dict(key="account_budgets", section="account", title="Account budgets", what="Account-level (invoiced) budgets, if any.",
             gaql="SELECT account_budget.id, account_budget.name, account_budget.status, account_budget.approved_spending_limit_micros, "
                  "account_budget.adjusted_spending_limit_micros, account_budget.amount_served_micros, account_budget.approved_start_date_time, "
                  "account_budget.approved_end_date_time FROM account_budget"),
        dict(key="labels", section="account", title="Labels", what="Labels defined on the account.",
             gaql="SELECT label.id, label.name, label.status, label.text_label.background_color FROM label"),
        dict(key="experiments", section="account", title="Experiments", what="Campaign experiments and their status.",
             gaql="SELECT experiment.name, experiment.status, experiment.type, experiment.start_date, experiment.end_date FROM experiment"),
        dict(key="asset_sets", section="assets", title="Asset sets", what="Business Profile locations, page feeds and other asset sets.",
             gaql="SELECT asset_set.id, asset_set.name, asset_set.type, asset_set.status FROM asset_set"),
        dict(key="user_lists", section="audience", title="Audience lists", what="Remarketing and customer lists with their sizes.",
             gaql="SELECT user_list.id, user_list.name, user_list.type, user_list.membership_status, user_list.size_for_search, user_list.size_for_display, "
                  "user_list.membership_life_span, user_list.eligible_for_search, user_list.eligible_for_display FROM user_list"),
        dict(key="shared_sets", section="search", title="Shared sets", what="Negative keyword and placement lists.",
             gaql="SELECT shared_set.id, shared_set.name, shared_set.type, shared_set.status, shared_set.member_count, shared_set.reference_count FROM shared_set"),
        dict(key="shared_criteria", section="search", title="Shared list contents", what="Keywords and placements inside the shared lists.",
             gaql="SELECT shared_set.name, shared_criterion.type, shared_criterion.keyword.text, shared_criterion.keyword.match_type, shared_criterion.placement.url "
                  "FROM shared_criterion LIMIT 1000"),
        dict(key="campaign_shared_sets", section="search", title="Lists applied to campaigns", what="Which campaigns use which shared lists.",
             gaql="SELECT campaign.name, shared_set.name, shared_set.type, campaign_shared_set.status FROM campaign_shared_set WHERE campaign.status != 'REMOVED'"),
        dict(key="bidding_strategies", section="campaigns", title="Portfolio bid strategies", what="Shared bidding strategies and how many campaigns use them.",
             gaql="SELECT bidding_strategy.id, bidding_strategy.name, bidding_strategy.type, bidding_strategy.status, bidding_strategy.campaign_count FROM bidding_strategy"),
        dict(key="seasonality", section="campaigns", title="Seasonality adjustments", what="Smart Bidding seasonality adjustments.",
             gaql="SELECT bidding_seasonality_adjustment.name, bidding_seasonality_adjustment.scope, bidding_seasonality_adjustment.status, "
                  "bidding_seasonality_adjustment.start_date_time, bidding_seasonality_adjustment.end_date_time, "
                  "bidding_seasonality_adjustment.conversion_rate_modifier FROM bidding_seasonality_adjustment"),
        dict(key="data_exclusions", section="campaigns", title="Data exclusions", what="Smart Bidding data exclusions.",
             gaql="SELECT bidding_data_exclusion.name, bidding_data_exclusion.scope, bidding_data_exclusion.status, bidding_data_exclusion.start_date_time, "
                  "bidding_data_exclusion.end_date_time FROM bidding_data_exclusion"),

        # ---- performance at account level ---------------------------------------
        dict(key="account_daily", section="overview", title="Daily performance, 12 months", what="Cost, clicks, impressions, conversions and value per day.",
             gaql=f"SELECT segments.date, {CORE_ALL}, metrics.view_through_conversions, metrics.interactions FROM customer WHERE {t}"),
        dict(key="account_device", section="audience", title="By device", what="Performance split by device.",
             gaql=f"SELECT segments.device, {CORE_ALL} FROM customer WHERE {d}"),
        dict(key="account_network", section="overview", title="By network", what="Search, search partners, Display, YouTube and cross-network.",
             gaql=f"SELECT segments.ad_network_type, {CORE_ALL} FROM customer WHERE {d}"),
        dict(key="account_dow_hour", section="overview", title="By day of week and hour", what="When clicks, spend and conversions happen.",
             gaql=f"SELECT segments.day_of_week, segments.hour, {CORE} FROM customer WHERE {d}"),
        dict(key="account_click_types", section="overview", title="By click type", what="What people click: headlines, sitelinks, calls, product listings.",
             gaql=f"SELECT segments.click_type, metrics.clicks, metrics.cost_micros FROM customer WHERE {d}"),
        dict(key="account_slots", section="search", title="By ad position", what="Top of page vs other positions.",
             gaql=f"SELECT segments.slot, {CORE} FROM customer WHERE {d}"),
        dict(key="account_conversion_actions", section="conversions", title="Conversions by action", what="Which conversion actions the last 30 days' conversions came from.",
             gaql=f"SELECT segments.conversion_action_name, segments.conversion_action_category, segments.conversion_action, metrics.conversions, "
                  f"metrics.conversions_value, metrics.all_conversions, metrics.all_conversions_value FROM customer WHERE {d}"),
        dict(key="account_conversion_daily", section="conversions", title="Conversions by action and day", what="Daily conversions per action for 90 days.",
             gaql=f"SELECT segments.date, segments.conversion_action_name, metrics.conversions, metrics.conversions_value, metrics.all_conversions FROM customer WHERE {c}"),

        # ---- campaigns -------------------------------------------------------------
        dict(key="campaigns", section="campaigns", title="Campaign settings", what="Every campaign with its type, status, bidding, budget and networks.",
             gaql="SELECT campaign.id, campaign.name, campaign.status, campaign.serving_status, campaign.primary_status, campaign.primary_status_reasons, "
                  "campaign.advertising_channel_type, campaign.advertising_channel_sub_type, campaign.bidding_strategy_type, campaign.bidding_strategy, "
                  "campaign.campaign_budget, campaign.start_date_time, campaign.end_date_time, campaign.optimization_score, campaign.labels, "
                  "campaign.target_roas.target_roas, campaign.maximize_conversion_value.target_roas, campaign.maximize_conversions.target_cpa_micros, "
                  "campaign.target_cpa.target_cpa_micros, campaign.manual_cpc.enhanced_cpc_enabled, campaign.network_settings.target_google_search, "
                  "campaign.network_settings.target_search_network, campaign.network_settings.target_content_network, "
                  "campaign.shopping_setting.merchant_id, campaign.shopping_setting.campaign_priority, campaign.shopping_setting.feed_label, "
                  "campaign_budget.amount_micros, campaign_budget.delivery_method, campaign_budget.explicitly_shared, "
                  "campaign_budget.has_recommended_budget, campaign_budget.recommended_budget_amount_micros FROM campaign ORDER BY campaign.status, campaign.name"),
        dict(key="campaigns_daily", section="campaigns", title="Campaign performance by day, 90 days", what="Daily cost, clicks, impressions and conversions per campaign.",
             gaql=f"SELECT campaign.id, campaign.name, campaign.advertising_channel_type, segments.date, {CORE_ALL} FROM campaign WHERE {c}"),
        dict(key="campaigns_is", section="search", title="Impression share by campaign", what="Search impression share and what was lost to budget or rank.",
             gaql=f"SELECT campaign.id, campaign.name, campaign.advertising_channel_type, metrics.search_impression_share, metrics.search_budget_lost_impression_share, "
                  f"metrics.search_rank_lost_impression_share, metrics.search_top_impression_share, metrics.search_absolute_top_impression_share, "
                  f"metrics.search_exact_match_impression_share, metrics.impressions FROM campaign WHERE {d} AND campaign.advertising_channel_type IN ('SEARCH', 'SHOPPING')"),
        dict(key="campaigns_device", section="audience", title="Campaign by device", what="Device split per campaign.",
             gaql=f"SELECT campaign.name, segments.device, {CORE} FROM campaign WHERE {d}"),
        dict(key="budgets", section="campaigns", title="Budgets", what="Daily budgets, delivery, sharing and Google's recommended amounts.",
             gaql="SELECT campaign_budget.id, campaign_budget.name, campaign_budget.amount_micros, campaign_budget.total_amount_micros, campaign_budget.delivery_method, "
                  "campaign_budget.period, campaign_budget.explicitly_shared, campaign_budget.reference_count, campaign_budget.status, "
                  "campaign_budget.has_recommended_budget, campaign_budget.recommended_budget_amount_micros, "
                  "campaign_budget.recommended_budget_estimated_change_weekly_clicks, campaign_budget.recommended_budget_estimated_change_weekly_cost_micros "
                  "FROM campaign_budget WHERE campaign_budget.status != 'REMOVED'"),
        dict(key="campaign_criteria", section="audience", title="Campaign targeting", what="Locations, languages, ad schedules, device adjustments and campaign negatives.",
             gaql="SELECT campaign.id, campaign.name, campaign_criterion.criterion_id, campaign_criterion.type, campaign_criterion.negative, campaign_criterion.bid_modifier, "
                  "campaign_criterion.location.geo_target_constant, campaign_criterion.proximity.radius, campaign_criterion.proximity.radius_units, "
                  "campaign_criterion.language.language_constant, campaign_criterion.ad_schedule.day_of_week, campaign_criterion.ad_schedule.start_hour, "
                  "campaign_criterion.ad_schedule.end_hour, campaign_criterion.device.type, campaign_criterion.keyword.text, campaign_criterion.keyword.match_type "
                  "FROM campaign_criterion WHERE campaign_criterion.type IN ('LOCATION', 'PROXIMITY', 'LANGUAGE', 'AD_SCHEDULE', 'DEVICE', 'KEYWORD') "
                  "AND campaign.status != 'REMOVED' AND campaign_criterion.status != 'REMOVED' LIMIT 3000"),
        dict(key="campaign_simulations", section="campaigns", title="Budget and target simulations", what="Google's forecast of clicks and conversions at other budgets or targets.",
             gaql="SELECT campaign_simulation.campaign_id, campaign_simulation.type, campaign_simulation.modification_method, campaign_simulation.start_date, "
                  "campaign_simulation.end_date, campaign_simulation.budget_point_list.points, campaign_simulation.target_roas_point_list.points, "
                  "campaign_simulation.target_cpa_point_list.points FROM campaign_simulation LIMIT 100"),
        dict(key="recommendations", section="account", title="Recommendations", what="Google's open recommendations by type and campaign (v25 no longer exposes the impact estimates through GAQL).",
             gaql="SELECT recommendation.resource_name, recommendation.type, recommendation.campaign, recommendation.ad_group, recommendation.dismissed "
                  "FROM recommendation LIMIT 300"),
        dict(key="change_events", section="changes", title="Change history, 28 days", what="Who changed what, from which tool.",
             gaql="SELECT change_event.change_date_time, change_event.change_resource_type, change_event.change_resource_name, change_event.client_type, "
                  "change_event.user_email, change_event.resource_change_operation, change_event.changed_fields, campaign.name, ad_group.name "
                  f"FROM change_event WHERE change_event.change_date_time BETWEEN '{w['change_start']}' AND '{w['today']}' ORDER BY change_event.change_date_time DESC LIMIT 2000"),

        # ---- ad groups, keywords, search terms, ads --------------------------------
        dict(key="ad_groups", section="search", title="Ad groups", what="Ad groups with bids and performance.",
             gaql=f"SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.type, ad_group.cpc_bid_micros, ad_group.target_cpa_micros, ad_group.target_roas, "
                  f"campaign.id, campaign.name, {CORE_ALL} FROM ad_group WHERE {d} AND ad_group.status != 'REMOVED' ORDER BY metrics.cost_micros DESC LIMIT 500"),
        dict(key="keywords", section="search", title="Keywords", what="Search keywords with match type, quality score, bids and performance.",
             gaql=f"SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
                  f"ad_group_criterion.approval_status, ad_group_criterion.quality_info.quality_score, ad_group_criterion.quality_info.creative_quality_score, "
                  f"ad_group_criterion.quality_info.post_click_quality_score, ad_group_criterion.quality_info.search_predicted_ctr, ad_group_criterion.effective_cpc_bid_micros, "
                  f"ad_group.name, campaign.name, {CORE}, metrics.average_cpc, metrics.search_impression_share, metrics.search_top_impression_share "
                  f"FROM keyword_view WHERE {d} ORDER BY metrics.impressions DESC LIMIT 1000"),
        dict(key="ad_group_negatives", section="search", title="Ad group negative keywords", what="Negatives set inside ad groups.",
             gaql="SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type FROM ad_group_criterion "
                  "WHERE ad_group_criterion.negative = TRUE AND ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status != 'REMOVED' LIMIT 1000"),
        dict(key="search_terms", section="search", title="Search terms", what="What people searched before clicking, and whether the term is added or excluded.",
             gaql=f"SELECT search_term_view.search_term, search_term_view.status, segments.search_term_match_type, segments.keyword.info.text, "
                  f"segments.keyword.info.match_type, campaign.name, ad_group.name, {CORE} FROM search_term_view WHERE {d} ORDER BY metrics.impressions DESC LIMIT 2000"),
        dict(key="pmax_search_categories", section="search", title="Performance Max search categories", what="Search themes that triggered Performance Max ads.",
             per_campaign_type="PERFORMANCE_MAX",
             gaql=f"SELECT campaign_search_term_insight.category_label, campaign_search_term_insight.id, metrics.clicks, metrics.impressions, metrics.conversions, "
                  f"metrics.conversions_value FROM campaign_search_term_insight WHERE {d} AND campaign_search_term_insight.campaign_id = {{cid}} "
                  f"ORDER BY metrics.impressions DESC LIMIT 100"),
        dict(key="ads", section="assets", title="Ads", what="Every ad with its strength, policy status, headlines, descriptions and performance.",
             gaql=f"SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.ad.type, ad_group_ad.status, ad_group_ad.ad_strength, "
                  f"ad_group_ad.policy_summary.approval_status, ad_group_ad.policy_summary.review_status, ad_group_ad.ad.final_urls, "
                  f"ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions, ad_group_ad.ad.responsive_search_ad.path1, "
                  f"ad_group_ad.ad.responsive_search_ad.path2, ad_group.name, campaign.name, {CORE}, metrics.ctr FROM ad_group_ad WHERE {d} "
                  f"AND ad_group_ad.status != 'REMOVED' ORDER BY metrics.impressions DESC LIMIT 300"),
        dict(key="ad_assets", section="assets", title="Ad headline and description performance", what="Which responsive search ad assets Google rates best.",
             gaql=f"SELECT ad_group_ad_asset_view.field_type, ad_group_ad_asset_view.performance_label, ad_group_ad_asset_view.pinned_field, ad_group_ad_asset_view.enabled, "
                  f"asset.id, asset.type, asset.text_asset.text, ad_group.name, campaign.name, metrics.impressions, metrics.clicks, metrics.conversions, metrics.cost_micros "
                  f"FROM ad_group_ad_asset_view WHERE {d} ORDER BY metrics.impressions DESC LIMIT 500"),
        dict(key="asset_groups", section="assets", title="Performance Max asset groups", what="Asset groups with ad strength and performance.",
             gaql=f"SELECT asset_group.id, asset_group.name, asset_group.status, asset_group.primary_status, asset_group.ad_strength, asset_group.final_urls, "
                  f"campaign.id, campaign.name, {CORE} FROM asset_group WHERE {d} ORDER BY metrics.cost_micros DESC LIMIT 200"),
        dict(key="asset_group_assets", section="assets", title="Performance Max assets", what="Headlines, descriptions, images and videos in each asset group.",
             gaql="SELECT asset_group.name, campaign.name, asset_group_asset.field_type, asset_group_asset.status, "
                  "asset_group_asset.primary_status, asset.id, asset.type, asset.name, asset.text_asset.text, asset.image_asset.full_size.url, "
                  "asset.image_asset.full_size.width_pixels, asset.image_asset.full_size.height_pixels, asset.youtube_video_asset.youtube_video_id, "
                  "asset.youtube_video_asset.youtube_video_title FROM asset_group_asset WHERE asset_group_asset.status != 'REMOVED' AND campaign.status != 'REMOVED' LIMIT 1500"),
        dict(key="asset_group_listing_filters", section="shopping", title="Performance Max product groups", what="How Performance Max asset groups subdivide the product feed.",
             gaql="SELECT asset_group.name, campaign.name, asset_group_listing_group_filter.id, asset_group_listing_group_filter.type, asset_group_listing_group_filter.listing_source, "
                  "asset_group_listing_group_filter.case_value.product_brand.value, asset_group_listing_group_filter.case_value.product_type.value, "
                  "asset_group_listing_group_filter.case_value.product_item_id.value, asset_group_listing_group_filter.case_value.product_custom_attribute.value, "
                  "asset_group_listing_group_filter.parent_listing_group_filter FROM asset_group_listing_group_filter WHERE campaign.status != 'REMOVED' LIMIT 500"),
        dict(key="campaign_assets", section="assets", title="Campaign assets (extensions)", what="Sitelinks, callouts, snippets, calls, promotions and images attached to campaigns.",
             gaql=f"SELECT campaign.name, campaign_asset.field_type, campaign_asset.status, campaign_asset.primary_status, asset.id, asset.type, asset.name, "
                  f"asset.sitelink_asset.link_text, asset.sitelink_asset.description1, asset.callout_asset.callout_text, asset.structured_snippet_asset.header, "
                  f"asset.structured_snippet_asset.values, asset.call_asset.phone_number, asset.promotion_asset.promotion_target, asset.image_asset.full_size.url, "
                  f"metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions FROM campaign_asset WHERE {d} ORDER BY metrics.impressions DESC LIMIT 400"),
        dict(key="customer_assets", section="assets", title="Account-level assets", what="Extensions attached at account level.",
             gaql="SELECT customer_asset.field_type, customer_asset.status, asset.id, asset.type, asset.name, asset.sitelink_asset.link_text, "
                  "asset.callout_asset.callout_text, asset.structured_snippet_asset.header, asset.structured_snippet_asset.values, asset.call_asset.phone_number "
                  "FROM customer_asset LIMIT 300"),

        # ---- shopping ---------------------------------------------------------------
        dict(key="shopping_products", section="shopping", title="Products", what="Product-level performance across Shopping and Performance Max.",
             gaql=f"SELECT segments.product_item_id, segments.product_title, segments.product_brand, segments.product_type_l1, segments.product_type_l2, "
                  f"segments.product_category_level1, segments.product_category_level2, segments.product_channel, segments.product_condition, campaign.name, "
                  f"campaign.advertising_channel_type, {CORE}, metrics.ctr FROM shopping_performance_view WHERE {d} ORDER BY metrics.cost_micros DESC LIMIT 1000"),
        dict(key="product_groups", section="shopping", title="Shopping product groups", what="Standard Shopping product partitions with bids.",
             gaql=f"SELECT campaign.name, ad_group.name, ad_group_criterion.criterion_id, ad_group_criterion.listing_group.type, "
                  f"ad_group_criterion.listing_group.case_value.product_brand.value, ad_group_criterion.listing_group.case_value.product_type.value, "
                  f"ad_group_criterion.listing_group.case_value.product_item_id.value, ad_group_criterion.cpc_bid_micros, ad_group_criterion.negative, {CORE} "
                  f"FROM product_group_view WHERE {d} ORDER BY metrics.cost_micros DESC LIMIT 300"),

        # ---- geography, audiences, demographics ------------------------------------
        dict(key="geo", section="audience", title="Geography", what="Performance by country, region and city.",
             gaql=f"SELECT geographic_view.country_criterion_id, geographic_view.location_type, segments.geo_target_region, segments.geo_target_city, {CORE} "
                  f"FROM geographic_view WHERE {d} ORDER BY metrics.cost_micros DESC LIMIT 1000"),
        dict(key="user_locations", section="audience", title="User locations", what="Where people physically were, in or outside the targeted area.",
             gaql=f"SELECT user_location_view.country_criterion_id, user_location_view.targeting_location, {CORE} FROM user_location_view WHERE {d} "
                  f"ORDER BY metrics.impressions DESC LIMIT 300"),
        dict(key="distance", section="audience", title="Distance from locations", what="Performance by distance from the business locations.",
             gaql=f"SELECT distance_view.distance_bucket, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions FROM distance_view WHERE {d}"),
        dict(key="age", section="audience", title="Age", what="Performance by age range.",
             gaql=f"SELECT campaign.name, ad_group.name, ad_group_criterion.age_range.type, ad_group_criterion.bid_modifier, {CORE} FROM age_range_view WHERE {d} LIMIT 500"),
        dict(key="gender", section="audience", title="Gender", what="Performance by gender.",
             gaql=f"SELECT campaign.name, ad_group.name, ad_group_criterion.gender.type, ad_group_criterion.bid_modifier, {CORE} FROM gender_view WHERE {d} LIMIT 500"),
        dict(key="ad_group_audiences", section="audience", title="Ad group audiences", what="Audience segments targeted or observed in ad groups.",
             gaql=f"SELECT campaign.name, ad_group.name, ad_group_criterion.type, ad_group_criterion.user_list.user_list, "
                  f"ad_group_criterion.user_interest.user_interest_category, ad_group_criterion.custom_audience.custom_audience, ad_group_criterion.bid_modifier, {CORE} "
                  f"FROM ad_group_audience_view WHERE {d} LIMIT 500"),
        dict(key="campaign_audiences", section="audience", title="Campaign audiences", what="Audience segments targeted or observed at campaign level.",
             gaql=f"SELECT campaign.name, campaign_criterion.type, campaign_criterion.user_list.user_list, campaign_criterion.user_interest.user_interest_category, "
                  f"campaign_criterion.bid_modifier, {CORE} FROM campaign_audience_view WHERE {d} LIMIT 500"),
        dict(key="landing_pages", section="assets", title="Landing pages", what="Performance by final URL.",
             gaql=f"SELECT landing_page_view.unexpanded_final_url, {CORE} FROM landing_page_view WHERE {d} ORDER BY metrics.clicks DESC LIMIT 300"),
        dict(key="pmax_placements", section="assets", title="Performance Max placements", what="Sites, apps and channels where Performance Max ads showed.",
             gaql=f"SELECT campaign.name, performance_max_placement_view.display_name, performance_max_placement_view.placement, "
                  f"performance_max_placement_view.placement_type, performance_max_placement_view.target_url, metrics.impressions FROM performance_max_placement_view "
                  f"WHERE {d} ORDER BY metrics.impressions DESC LIMIT 300"),
        dict(key="display_placements", section="assets", title="Display placements", what="Placements for Display campaigns.",
             gaql=f"SELECT campaign.name, detail_placement_view.display_name, detail_placement_view.placement_type, detail_placement_view.target_url, "
                  f"metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions FROM detail_placement_view WHERE {d} ORDER BY metrics.impressions DESC LIMIT 300"),
        dict(key="auction_insights", section="search", title="Auction insights", what="Competing domains in the Search auctions and how often they outrank us.", restricted="Google only serves these metrics to allowlisted developers.",
             gaql=f"SELECT campaign.name, segments.auction_insight_domain, metrics.auction_insight_search_impression_share, metrics.auction_insight_search_overlap_rate, "
                  f"metrics.auction_insight_search_outranking_share, metrics.auction_insight_search_position_above_rate, "
                  f"metrics.auction_insight_search_top_impression_percentage, metrics.auction_insight_search_absolute_top_impression_percentage "
                  f"FROM campaign WHERE {d} AND campaign.advertising_channel_type = 'SEARCH' LIMIT 200"),
        dict(key="paid_organic", section="search", title="Paid and organic search terms", what="Search terms where paid and organic listings both appeared (needs a Search Console link).",
             gaql=f"SELECT paid_organic_search_term_view.search_term, segments.search_engine_results_page_type, metrics.organic_clicks, metrics.organic_impressions, "
                  f"metrics.organic_queries, metrics.clicks, metrics.impressions FROM paid_organic_search_term_view WHERE {d} ORDER BY metrics.impressions DESC LIMIT 300"),
        dict(key="lead_forms", section="conversions", title="Lead form submissions", what="Leads collected through lead form assets.",
             gaql="SELECT lead_form_submission_data.submission_date_time, campaign.name, lead_form_submission_data.lead_form_submission_fields FROM lead_form_submission_data LIMIT 100"),
    ]


def run_report(api, customer_id, entry, campaigns):
    """Run one catalog entry; return the report dict, never raise."""
    started = time.time()
    rows, error = [], None
    try:
        if entry.get("per_campaign_type"):
            targets = [c for c in campaigns if c.get("campaign.advertisingChannelType") == entry["per_campaign_type"] and c.get("campaign.status") == "ENABLED"]
            for camp in targets:
                for r in api.search(customer_id, entry["gaql"].format(cid=camp["campaign.id"])):
                    r["campaign.id"], r["campaign.name"] = camp["campaign.id"], camp["campaign.name"]
                    rows.append(r)
        else:
            rows = api.search(customer_id, entry["gaql"])
    except AdsApiError as exc:
        error = {"code": exc.code, "message": exc.message, "field": exc.field, "request_id": exc.request_id}
    except Exception as exc:  # noqa: BLE001 - the coverage table reports it
        error = {"code": type(exc).__name__, "message": str(exc)[:500]}
    limit = re.search(r"LIMIT (\d+)\s*$", entry["gaql"])
    return {"key": entry["key"], "section": entry["section"], "title": entry["title"], "what": entry["what"], "gaql": entry["gaql"],
            "rows": rows, "count": len(rows), "error": error, "ms": int((time.time() - started) * 1000),
            "truncated": bool(limit and not entry.get("per_campaign_type") and len(rows) >= int(limit.group(1))),
            "restricted": entry.get("restricted")}


def lookup_constants(api, customer_id, resource, ids, fields):
    """Resolve geo_target_constant / language_constant ids to names, 500 at a time."""
    out = {}
    camel = re.sub(r"_(\w)", lambda m: m.group(1).upper(), resource)
    ids = sorted({str(i) for i in ids if i})
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        try:
            for r in api.search(customer_id, f"SELECT {', '.join(fields)} FROM {resource} WHERE {resource}.id IN ({', '.join(chunk)})"):
                out[str(r[f"{camel}.id"])] = {k.split(".")[-1]: v for k, v in r.items()}
        except AdsApiError as exc:
            print(f"  {resource} lookup failed: {exc}", file=sys.stderr)
    return out


def _ids_from(rows, key):
    for r in rows:
        v = r.get(key)
        if v:
            yield str(v).split("/")[-1]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login-customer-id", default=os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", DEFAULT_MANAGER_ID),
                    help="Manager account to log in through (default: the Super Hair Pieces manager).")
    ap.add_argument("--customer", action="append", help="Only these client customer ids (repeatable). Default: every enabled client under the manager.")
    ap.add_argument("--detail-days", type=int, default=30, help="Window for the detail reports (keywords, products, geo...). Default 30, ending yesterday.")
    ap.add_argument("--trend-days", type=int, default=365, help="Window for the daily account series. Default 365.")
    ap.add_argument("--campaign-days", type=int, default=90, help="Window for the daily campaign series. Default 90.")
    ap.add_argument("--only", nargs="+", metavar="KEY", help="Run only these report keys (for testing).")
    ap.add_argument("--out", default=os.path.join(HERE, "dashboard_data.json"))
    args = ap.parse_args(argv)

    api = GoogleAds(login_customer_id=args.login_customer_id)
    manager = args.login_customer_id.replace("-", "")
    today = dt.date.today()
    end = today - dt.timedelta(days=1)  # complete days only, like the Google Ads UI presets

    def window(days):
        start = end - dt.timedelta(days=days - 1)
        return {"start": start.isoformat(), "end": end.isoformat(), "days": days, "gaql": f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"}

    windows = {"detail": window(args.detail_days), "trend": window(args.trend_days), "campaign_daily": window(args.campaign_days)}
    frags = {k: v["gaql"] for k, v in windows.items()}
    frags["change_start"] = (today - dt.timedelta(days=28)).isoformat()
    frags["today"] = today.isoformat()
    entries = catalog(frags)
    if args.only:
        entries = [e for e in entries if e["key"] in set(args.only)]

    # -- hierarchy ---------------------------------------------------------------
    try:
        hierarchy = api.search(manager, "SELECT customer_client.id, customer_client.descriptive_name, customer_client.level, customer_client.manager, "
                                        "customer_client.currency_code, customer_client.time_zone, customer_client.status, customer_client.test_account, "
                                        "customer_client.hidden FROM customer_client ORDER BY customer_client.level, customer_client.descriptive_name")
    except AdsApiError as exc:
        sys.exit(f"error: {explain_api_error(exc, api.identity())}")
    manager_row = next((h for h in hierarchy if str(h.get("customerClient.id")) == manager), {})
    targets = [str(h["customerClient.id"]) for h in hierarchy
               if not h.get("customerClient.manager") and h.get("customerClient.status") == "ENABLED" and not h.get("customerClient.testAccount")]
    if args.customer:
        targets = [c.replace("-", "") for c in args.customer]
    print(f"manager {manager} ({manager_row.get('customerClient.descriptiveName', '?')}): {len(targets)} client account(s); "
          f"detail window {windows['detail']['start']}..{windows['detail']['end']}", flush=True)

    accounts = []
    for cid in targets:
        info = next((h for h in hierarchy if str(h.get("customerClient.id")) == cid), {})
        print(f"\n== {cid} {info.get('customerClient.descriptiveName', '')}", flush=True)
        reports = OrderedDict()
        campaigns = []
        for entry in entries:
            rep = run_report(api, cid, entry, campaigns)
            reports[entry["key"]] = rep
            if entry["key"] == "campaigns" and not rep["error"]:
                campaigns = rep["rows"]
            status = f"ERROR {rep['error']['code']}" if rep["error"] else f"{rep['count']} rows"
            print(f"  {entry['key']:<28} {status:<40} {rep['ms']} ms", flush=True)
            if rep["error"]:
                print(f"      {rep['error']['message'][:200]}", flush=True)

        geo_ids = set(_ids_from(reports.get("campaign_criteria", {}).get("rows", []), "campaignCriterion.location.geoTargetConstant"))
        for key, field in (("geo", "geographicView.countryCriterionId"), ("geo", "segments.geoTargetRegion"), ("geo", "segments.geoTargetCity"),
                           ("user_locations", "userLocationView.countryCriterionId")):
            geo_ids.update(_ids_from(reports.get(key, {}).get("rows", []), field))
        lang_ids = set(_ids_from(reports.get("campaign_criteria", {}).get("rows", []), "campaignCriterion.language.languageConstant"))
        geo_names = lookup_constants(api, cid, "geo_target_constant", geo_ids,
                                     ["geo_target_constant.id", "geo_target_constant.name", "geo_target_constant.canonical_name",
                                      "geo_target_constant.country_code", "geo_target_constant.target_type"])
        lang_names = lookup_constants(api, cid, "language_constant", lang_ids, ["language_constant.id", "language_constant.name", "language_constant.code"])

        cust = (reports.get("customer", {}).get("rows") or [{}])[0]
        accounts.append({
            "id": cid, "name": cust.get("customer.descriptiveName") or info.get("customerClient.descriptiveName"),
            "currency": cust.get("customer.currencyCode") or info.get("customerClient.currencyCode"),
            "time_zone": cust.get("customer.timeZone") or info.get("customerClient.timeZone"),
            "status": cust.get("customer.status") or info.get("customerClient.status"),
            "auto_tagging": cust.get("customer.autoTaggingEnabled"), "optimization_score": cust.get("customer.optimizationScore"),
            "conversion_tracking_status": cust.get("customer.conversionTrackingSetting.conversionTrackingStatus"),
            "reports": reports, "geo_names": geo_names, "language_names": lang_names,
        })

    ok = sum(1 for a in accounts for r in a["reports"].values() if not r["error"])
    failed = sum(1 for a in accounts for r in a["reports"].values() if r["error"])
    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "api_version": API_VERSION,
           "manager": {"id": manager, "name": manager_row.get("customerClient.descriptiveName")}, "identity": api.identity(),
           "windows": {k: {kk: vv for kk, vv in v.items() if kk != "gaql"} for k, v in windows.items()},
           "hierarchy": hierarchy, "accounts": accounts, "api_calls": api.calls}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
    print(f"\nwrote {args.out}: {len(accounts)} account(s), {ok} reports ok, {failed} failed, {api.calls} API calls, {os.path.getsize(args.out) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
