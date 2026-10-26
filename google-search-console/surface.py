"""
What the Search Console API can tell us: every dimension, metric and enum.

The dashboard's Search tab renders this table beside the live figures, so a
reader can see not just what we pulled but everything that was available to
pull. Keep the strings exactly as the API spells them - they are copied into
request bodies, and the tab matches returned values against them to show which
of a dimension's possible values this property actually has data for.
"""

# searchAnalytics.query -----------------------------------------------------

DIMENSIONS = [
    {
        "name": "query",
        "what": "The search term typed by the user.",
        "values": "open-ended",
        "note": "Rare queries are withheld for privacy, so the rows never add up to the property total.",
    },
    {
        "name": "page",
        "what": "The canonical landing page URL.",
        "values": "open-ended",
        "note": "Grouping by page makes impressions count once per page rather than once per query.",
    },
    {
        "name": "country",
        "what": "Where the search happened.",
        "values": "ISO 3166-1 alpha-3, lowercase (can, usa, nld, fra, esp, deu)",
        "note": "",
    },
    {
        "name": "device",
        "what": "The device class the search ran on.",
        "values": "DESKTOP, MOBILE, TABLET",
        "note": "",
    },
    {
        "name": "searchAppearance",
        "what": "The rich-result treatment the listing was shown with.",
        "values": "discovered per property - see the table",
        "note": "Cannot be combined with any other dimension in the same request.",
    },
    {
        "name": "date",
        "what": "The day, in the property's time zone (America/Los_Angeles).",
        "values": "YYYY-MM-DD",
        "note": "History is capped at 16 months; the last 2-3 days arrive only with dataState=all.",
    },
    {
        "name": "hour",
        "what": "Hour of the day, for the last few days only.",
        "values": "RFC 3339 timestamps",
        "note": "Needs dataState=hourly_all. Not collected here; the window is too short for a trend.",
    },
]

METRICS = [
    {"name": "clicks", "what": "Clicks through to the site."},
    {"name": "impressions", "what": "Times a link to the site appeared in results."},
    {"name": "ctr", "what": "clicks / impressions, returned as a fraction (0.0312 is 3.12%)."},
    {"name": "position", "what": "Average position, 1-based; lower is better."},
]

SEARCH_TYPES = [
    {"name": "web", "what": "The regular Search results tab."},
    {"name": "image", "what": "Google Images."},
    {"name": "video", "what": "The Videos tab."},
    {"name": "news", "what": "The News tab."},
    {"name": "discover", "what": "The Discover feed. No query dimension, by design."},
    {"name": "googleNews", "what": "news.google.com and the News app. No query dimension."},
]

DATA_STATES = [
    {"name": "final", "what": "Settled data only. The default, and what a report should quote."},
    {"name": "all", "what": "Adds the last two or three days, which Google may still revise."},
    {"name": "hourly_all", "what": "Hourly rows including partial data. Required with the hour dimension."},
]

AGGREGATION_TYPES = [
    {"name": "auto", "what": "Google picks; byProperty unless the page dimension is used."},
    {"name": "byPage", "what": "Count a property's impressions once per page."},
    {"name": "byProperty", "what": "Count once per property per query."},
    {"name": "byNewsShowcasePanel", "what": "News Showcase panels only."},
]

FILTER_OPERATORS = [
    "equals", "notEquals", "contains", "notContains",
    "includingRegex", "excludingRegex",
]

# Other resources ----------------------------------------------------------

RESOURCES = [
    {
        "name": "sites.list",
        "what": "Every property the credential can see, with its permission level.",
        "values": "siteFullUser, siteOwner, siteRestrictedUser, siteUnverifiedUser",
    },
    {
        "name": "sitemaps.list",
        "what": "Submitted sitemaps: when last read, how many URLs, errors and warnings.",
        "values": "type: sitemap, urlList, rssFeed, atomFeed, notSitemap, patternSitemap",
    },
    {
        "name": "sitemaps.list contents",
        "what": "Per content type, how many URLs were submitted. The matching `indexed` count is deprecated and Google no longer fills it in.",
        "values": "web, image, video, news, mobile, androidApp, iosApp, pattern",
    },
    {
        "name": "urlInspection.index.inspect",
        "what": "One URL at a time: whether Google has it, how it found it, when it last crawled it.",
        "values": "verdict: PASS, PARTIAL, FAIL, NEUTRAL, VERDICT_UNSPECIFIED",
    },
]

INSPECTION_FIELDS = [
    {"name": "verdict", "values": "PASS, PARTIAL, FAIL, NEUTRAL, VERDICT_UNSPECIFIED"},
    {"name": "coverageState", "values": "free text, e.g. 'Submitted and indexed', 'Crawled - currently not indexed'"},
    {"name": "robotsTxtState", "values": "ALLOWED, DISALLOWED, ROBOTS_TXT_STATE_UNSPECIFIED"},
    {"name": "indexingState", "values": "INDEXING_ALLOWED, BLOCKED_BY_META_TAG, BLOCKED_BY_HTTP_HEADER, BLOCKED_BY_ROBOTS_TXT"},
    {"name": "pageFetchState", "values": "SUCCESSFUL, SOFT_404, BLOCKED_ROBOTS_TXT, NOT_FOUND, ACCESS_DENIED, SERVER_ERROR, REDIRECT_ERROR, ACCESS_FORBIDDEN, BLOCKED_4XX, INTERNAL_CRAWL_ERROR, INVALID_URL"},
    {"name": "crawledAs", "values": "DESKTOP, MOBILE, CRAWLING_USER_AGENT_UNSPECIFIED"},
    {"name": "googleCanonical", "values": "the URL Google treats as canonical"},
    {"name": "userCanonical", "values": "the canonical the page itself declares"},
    {"name": "sitemap", "values": "which submitted sitemaps list this URL"},
    {"name": "referringUrls", "values": "pages Google followed to reach it"},
    {"name": "lastCrawlTime", "values": "RFC 3339 timestamp"},
    {"name": "richResultsResult", "values": "detected structured-data types and their verdicts"},
    {"name": "ampResult", "values": "AMP verdict; empty on a site without AMP"},
]

LIMITS = [
    "searchAnalytics.query defaults to 1,000 rows when rowLimit is omitted, which silently truncates a table.",
    "Rows are capped at 50,000 a day per search type, another reason dimension rows total less than the property.",
    "searchAnalytics.query returns at most 25,000 rows per call; page with startRow.",
    "History is 16 months. Anything older is gone, not merely unqueried.",
    "Rare queries are withheld, so per-query rows total less than the property total.",
    "searchAppearance cannot be combined with another dimension; to break one down, filter on it instead.",
    "Search Analytics allows 1,200 queries a minute per property and per user.",
    "URL Inspection allows 2,000 calls a day and 600 a minute per property.",
    "Access is granted per property; Search Console has no organisation-wide role.",
]
