"""Helpers for the accounts-payable admin pages (/admin, main.py's /api/admin routes).

Pure functions over plain lists and dicts: no Flask, no request, no main import, so they are tested
without a server. The only state is a small cache of the whole-table reads the catalogue needs (every
transaction and invoice, a few thousand rows each), kept 5 minutes per store.

Vocabulary:
- A *use case* is what a purchase was for (Marketing, Travel...): the dashboard's Use case box, stored
  in transactions.usage. A *cost centre* is where it belongs (a salon, Head office, US, EU).
- A *folder* is a vendor folder under Invoice/YYYY MM/<folder>/ in Drive; `folder_key` is how folders are
  compared (single spaces, case ignored), since "Canada Post" and "canada post" are one supplier.
- A *descriptor* is the card statement's description of a purchase ("UPS CANADA BILLING C"); its
  *prefix* is its first two words in the pattern space of normalise_pattern ("ups canada").
- The *registry* is what AP declared about vendors (store.list_vendor_registry). In Round A it only
  feeds the catalogue, proposals and coding suggestions: matching never reads it.
"""

import datetime as dt
import re
import threading
import time

import matching
import receipts
from store import StoreError, now_iso

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
CACHE_TTL = 300          # seconds the catalogue's whole-table reads are reused
MISSING_TTL = 60         # how long "the registry tables are not there yet" is believed
_cache = {}              # key -> {"value", "store", "at": time.monotonic(), "read_at": UTC ISO, "ttl"?}
_cache_lock = threading.Lock()


def cached(key, store, compute, ttl=CACHE_TTL, fresh=False):
    """compute() once per `ttl` seconds per store. An entry read from another store object (a test, or
    set_clients installing a new one) is never reused. The lock guards the dict only: two requests that
    miss at once may both compute, which is cheaper than making one wait behind the other."""
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if (entry and not fresh and entry["store"] is store
                and now - entry["at"] <= min(ttl, entry.get("ttl", ttl))):
            return entry["value"]
    read_at = now_iso()
    value = compute()
    with _cache_lock:
        _cache[key] = {"value": value, "store": store, "at": time.monotonic(), "read_at": read_at}
    return value


def cached_at(key):
    """When the cached value was read (UTC ISO), or None."""
    with _cache_lock:
        entry = _cache.get(key)
        return entry["read_at"] if entry else None


def invalidate(*keys):
    """Forget the given keys; no keys: everything."""
    with _cache_lock:
        if not keys:
            _cache.clear()
        for key in keys:
            _cache.pop(key, None)


def transaction_rows(store, fresh=False):
    """Every transaction (store.ADMIN_TXN_COLUMNS), cached. Shared between requests: do not modify."""
    return cached("transactions", store, store.admin_transaction_rows, fresh=fresh)


def invoice_rows(store, fresh=False):
    """Every invoice, removed ones too (store.ADMIN_INVOICE_COLUMNS), cached. Do not modify."""
    return cached("invoices", store, store.admin_invoice_rows, fresh=fresh)


def registry(store, fresh=False):
    """The vendor registry as a RegistryIndex, cached; None while schema.sql's tables are missing (that
    None is believed for MISSING_TTL seconds). Any other store failure is raised."""
    def compute():
        try:
            return RegistryIndex(store.list_vendor_registry())
        except StoreError as exc:
            if exc.missing:
                return None
            raise
    value = cached("registry", store, compute, fresh=fresh)
    if value is None:
        with _cache_lock:
            entry = _cache.get("registry")
            if entry is not None and entry["value"] is None:
                entry["ttl"] = MISSING_TTL
    return value


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
def norm_space(text):
    return " ".join(str(text or "").split())


def name_key(text):
    """How every name typed by a person is compared: single spaces, case ignored."""
    return norm_space(text).casefold()


folder_key = name_key

_TRAILING_DIGITS = re.compile(r"\d{4,}$")


def descriptor_tokens(text):
    """The words of a card descriptor or supplier name, lower case, punctuation as spaces. A run of 4 or
    more digits at the end of a word is a reference, not the name ("FEDEX95845920" is FedEx), and words
    that are only digits are skipped (store and order numbers)."""
    out = []
    for token in re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split():
        token = _TRAILING_DIGITS.sub("", token)
        if token and not token.isdigit():
            out.append(token)
    return out


def normalise_pattern(text):
    """The one space patterns and descriptors are compared in: "PAYPAL *FIVERR" -> "paypal fiverr"."""
    return " ".join(descriptor_tokens(text))


def descriptor_prefix(text, words=2):
    """The first `words` words: "UPS CANADA BILLING C" -> "ups canada", "CPC / SCP 123456" -> "cpc scp"."""
    return " ".join(descriptor_tokens(text)[:words])


def first_word(text):
    return descriptor_prefix(text, 1)


# A descriptor's first word that names who took the payment, not who was paid ("SQ *COFFEE", "PAYPAL
# *FIVERR", "GOOGLE *YOUTUBE"): never a vendor pattern on its own.
PROCESSOR_WORDS = frozenset(receipts.PROCESSORS | receipts.PLATFORMS)
PATTERN_KINDS = ("descriptor", "invoice_vendor")
MATCH_TYPES = ("prefix", "exact")


def pattern_problem(pattern, kind="descriptor", match_type="prefix"):
    """Why a (normalised) pattern cannot be saved, as the 400 message, or None."""
    if kind not in PATTERN_KINDS:
        return "kind: descriptor or invoice_vendor"
    if match_type not in MATCH_TYPES or (kind == "invoice_vendor" and match_type == "prefix"):
        return "match_type: prefix or exact (a supplier name on documents: exact)"
    pattern = str(pattern or "")
    if len(pattern) < 3:
        return "pattern: at least 3 letters or digits"
    if len(pattern) > 120:
        return "pattern: at most 120 characters"
    if kind == "descriptor" and match_type == "prefix" and len(pattern.split()) == 1:
        if pattern in PROCESSOR_WORDS:
            return (f"pattern: '{pattern}' is a payment processor or platform; add the word after it "
                    f"(e.g. '{pattern} coffee')")
        if len(pattern) < 4:
            return "pattern: a one-word prefix needs 4 letters or more ('air' would catch Air Canada and Airbnb)"
    return None


def pattern_hits(pattern_row, text):
    """Does the pattern name this descriptor (or supplier name)? Whole words only: "ups" does not hit
    "upstart co"."""
    pattern = pattern_row.get("pattern") or ""
    t = normalise_pattern(text)
    if not pattern or not t:
        return False
    if pattern_row.get("match_type") == "exact":
        return t == pattern
    return t == pattern or t.startswith(pattern + " ")


def snap_name(value, names):
    """The entry of `names` that is `value` once case and spaces are ignored, else None."""
    key = name_key(value)
    if not key:
        return None
    return next((n for n in names if name_key(n) == key), None)


def card_groups(cards):
    """The groups cards are in (Hair, Beauty): one spelling per name, the first card's, sorted."""
    seen = {}
    for c in cards:
        group = norm_space(c.get("group_name"))
        if group:
            seen.setdefault(name_key(group), group)
    return sorted(seen.values(), key=lambda g: (g.casefold(), g))


def owner_emails(value):
    """A card's owners: one address or several, separated by commas, semicolons or spaces (as main.py)."""
    return [e for e in re.split(r"[\s,;]+", (value or "").lower()) if e]


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------
FLAGS = ("no_owner", "no_group", "holder_not_person", "no_webhook", "inactive_90d", "never_used", "never_reviewed",
         "default_retired", "no_default_primary_has")
FLAG_LABELS = {"no_owner": "No owner", "no_group": "No group", "holder_not_person": "Holder not linked to a person",
               "no_webhook": "No Chat webhook of its own", "inactive_90d": "No activity in 90 days",
               "never_used": "Never used", "never_reviewed": "Never reviewed",
               "default_retired": "Default cost centre retired",
               "no_default_primary_has": "No default cost centre (its primary card has one)"}


def recent_months(today, n=3):
    """{(year, month)} of today's month and the n-1 before it."""
    out = set()
    for back in range(n):
        m = today.month - 1 - back
        out.add((today.year + m // 12, m % 12 + 1))
    return out


def _ym(year, month):
    return f"{int(year):04d}-{int(month):02d}"


def card_activity(summary_rows, today):
    """{last4: {"transactions", "recent", "missing", "first_month", "last_month", "missing_last_month",
    "missing_newest", "missing_months"}} from month_summary rows. recent: the transactions of the last 3 months
    (about 90 days, counted in whole statement months). missing: every month's missing invoices added up.
    missing_last_month: the newest month with an invoice missing (None when none is), the month the Cards
    gallery's link opens on the dashboard (one month: the dashboard shows a month at a time); missing_newest:
    how many are missing in that month; missing_months: how many months have one missing."""
    recent = recent_months(today)
    out, missing_by_month = {}, {}
    for r in summary_rows:
        last4 = r.get("card_last4")
        if not last4:
            continue
        a = out.setdefault(last4, {"transactions": 0, "recent": 0, "missing": 0, "first_month": None, "last_month": None,
                                   "missing_last_month": None, "missing_newest": 0, "missing_months": 0})
        n = int(r.get("transactions") or 0)
        missing = int(r.get("missing") or 0)
        a["transactions"] += n
        a["missing"] += missing
        if (int(r["year"]), int(r["month"])) in recent:
            a["recent"] += n
        ym = _ym(r["year"], r["month"])
        a["first_month"] = ym if a["first_month"] is None else min(a["first_month"], ym)
        a["last_month"] = ym if a["last_month"] is None else max(a["last_month"], ym)
        if missing > 0:
            months = missing_by_month.setdefault(last4, {})
            months[ym] = months.get(ym, 0) + missing
    for last4, months in missing_by_month.items():
        newest = max(months)
        out[last4].update(missing_last_month=newest, missing_newest=months[newest], missing_months=len(months))
    return out


def card_flags(view, activity, updated_at, active_cost_centers=(), primary_default=None):
    """What needs AP's attention on a card, in FLAGS order. view: main.card_view(c)."""
    activity = activity or {}
    default = view.get("default_cost_center") or ""
    checks = {
        "no_owner": not view.get("owner_email"),
        "no_group": not view.get("group_name"),
        "holder_not_person": bool(view.get("holder_name")) and not view.get("person_id"),
        "no_webhook": not view.get("webhook_configured"),
        "inactive_90d": not activity.get("recent"),
        "never_used": not activity.get("transactions"),
        "never_reviewed": updated_at is None,
        "default_retired": bool(default) and default not in set(active_cost_centers or ()),
        # A supplementary card does not inherit its primary's default (it may be used elsewhere): flagged
        # instead, so AP decides.
        "no_default_primary_has": bool(view.get("primary_last4")) and not default and bool(primary_default),
    }
    return [f for f in FLAGS if checks[f]]


def owner_list(owner_email, allowed_domains):
    """[{"email", "outside_domain"}]: an owner outside the company's domains cannot sign in to edit."""
    allowed = {d.lower() for d in allowed_domains or ()}
    return [{"email": e, "outside_domain": "@" not in e or e.rpartition("@")[2] not in allowed}
            for e in owner_emails(owner_email)]


def owners_index(cards):
    """{owner e-mail: [last4]}: who owns which cards, e-mails and cards sorted."""
    out = {}
    for c in cards:
        for e in owner_emails(c.get("owner_email")):
            out.setdefault(e, set()).add(c["last4"])
    return {e: sorted(out[e]) for e in sorted(out)}


def _live(invoices):
    return [i for i in invoices if not i.get("removed_at")]


def _date(value):
    """An ISO date (or datetime) string or date -> datetime.date, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _most_common(counts):
    """The key with the highest count; a tie goes to the first in sorted order."""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if counts else None


def _folder_spellings(invoices):
    """{folder_key: the most common stored spelling} over invoices with a folder."""
    counts = {}
    for i in invoices:
        folder = (i.get("vendor_folder") or "").strip()
        if folder:
            per = counts.setdefault(folder_key(folder), {})
            per[folder] = per.get(folder, 0) + 1
    return {k: _most_common(v) for k, v in counts.items()}


def unclaimed_numbers(invoices, cards, ignored=None):
    """The card numbers receipts show that no card claims: not a card's last four, not a number entered
    as printed on a card, not a reissue number of one. Most receipts first."""
    ignored = ignored or {}
    claimed = {c["last4"] for c in cards} | set(matching.all_card_aliases(cards))
    found = {}
    for i in _live(invoices):
        number = str(i.get("card_last4") or "")
        if not re.fullmatch(r"\d{4}", number) or number in claimed:
            continue
        found.setdefault(number, []).append(i)
    out = []
    for number, docs in found.items():
        folders = {}
        for i in docs:
            folder = (i.get("vendor_folder") or "").strip()
            if folder:
                folders.setdefault(folder_key(folder), []).append(folder)
        top = sorted(((len(v), _most_common({s: v.count(s) for s in v})) for v in folders.values()),
                     key=lambda p: (-p[0], p[1].casefold()))[:3]
        dates = sorted(str(i["invoice_date"])[:10] for i in docs if i.get("invoice_date"))
        out.append({"number": number, "invoices": len(docs), "folders": [{"folder": f, "count": n} for n, f in top],
                    "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
                    "ignored": ignored.get(number) or None})
    return sorted(out, key=lambda r: (-r["invoices"], r["number"]))


def _cents(value):
    return int(round(float(value) * 100))


def _near(a, b):
    return a is not None and b is not None and abs(float(a) - float(b)) <= 0.0101   # within a cent


def likely_cards(number, invoices, transactions, cards, days=21):
    """Which card the receipts showing an unclaimed number probably belong to: for each receipt, the
    cards with a purchase of its total (as billed or foreign) 0 to `days` days after its date."""
    docs = [i for i in _live(invoices) if str(i.get("card_last4") or "") == number]
    docs.sort(key=lambda i: (str(i.get("invoice_date") or ""), str(i.get("id") or "")), reverse=True)
    docs = docs[:200]
    by_cents = {}               # purchases by amount, both as billed and foreign, for a quick lookup
    for t in transactions:
        if t.get("type") != "purchase":
            continue
        for value in {_cents(v) for v in (t.get("amount"), t.get("source_amount")) if v is not None}:
            by_cents.setdefault(value, []).append(t)
    by_last4 = {c["last4"]: c for c in cards}
    hits, months, considered, listed = {}, {}, 0, []
    for i in docs:
        total, when = i.get("total"), _date(i.get("invoice_date"))
        candidates = set()
        if total is not None and when is not None:
            considered += 1
            cents = _cents(total)
            for t in {id(t): t for c in (cents - 1, cents, cents + 1) for t in by_cents.get(c, ())}.values():
                txn_date = _date(t.get("txn_date"))
                if (txn_date is not None and 0 <= (txn_date - when).days <= days
                        and (_near(t.get("amount"), total) or _near(t.get("source_amount"), total))):
                    candidates.add(t["card_last4"])
                    months.setdefault(t["card_last4"], set()).add(str(t["txn_date"])[:7])
        for last4 in candidates:
            hits[last4] = hits.get(last4, 0) + 1
        listed.append({"id": i.get("id"), "file_name": i.get("file_name"), "vendor_folder": i.get("vendor_folder"),
                       "vendor": i.get("vendor"), "invoice_date": i.get("invoice_date"), "total": total,
                       "currency": i.get("currency"), "web_view_link": i.get("web_view_link"),
                       "month_folder": i.get("month_folder"), "candidates": sorted(candidates)})
    receipts_showing = {}
    for i in _live(invoices):
        n = str(i.get("card_last4") or "")
        if n:
            receipts_showing[n] = receipts_showing.get(n, 0) + 1
    likely = []
    for last4, n in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))[:5]:
        card = by_last4.get(last4) or {}
        entered = card.get("card_number") or None
        likely.append({"last4": last4, "label": card.get("label") or f"Card ending {last4}", "hits": n,
                       "share": round(n / considered, 2) if considered else 0,
                       "card_number": entered,
                       # the receipts that stop counting as this card's if its number is replaced
                       "card_number_receipts": receipts_showing.get(entered, 0) if entered else 0,
                       "months": sorted(months.get(last4, ()))})
    return {"number": number, "invoices": listed, "likely": likely, "considered": considered}


def alias_changes(before_cards, after_cards, subject):
    """[{"number", "before", "after"}]: the receipt numbers whose card changes when `subject` is deleted
    (in before_cards only) or created (in after_cards only), leaving out the subject's own expected
    moves. A known card is never itself a reissue alias, so adding one can take receipt numbers from
    another card, and deleting one can hand its numbers to another."""
    before, after = matching.all_card_aliases(before_cards), matching.all_card_aliases(after_cards)
    deleting = any(c.get("last4") == subject for c in before_cards) and not any(c.get("last4") == subject for c in after_cards)
    creating = any(c.get("last4") == subject for c in after_cards) and not any(c.get("last4") == subject for c in before_cards)
    out = []
    for number in sorted(set(before) | set(after)):
        b, a = before.get(number), after.get(number)
        if b == a:
            continue
        if deleting and b == subject and a is None:
            continue
        if creating and b is None and a == subject:
            continue
        out.append({"number": number, "before": b, "after": a})
    return out


# ---------------------------------------------------------------------------
# Coding: use cases and cost centres
# ---------------------------------------------------------------------------
def uses(name, values):
    """How many purchases carry `name`, spelled any way (values: {value: n})."""
    key = name_key(name)
    return sum(n for value, n in (values or {}).items() if name_key(value) == key)


def _by_key(values):
    """{name_key: (most-used spelling, total uses)}."""
    groups = {}
    for value, n in (values or {}).items():
        groups.setdefault(name_key(value), {})[value] = n
    return {k: (_most_common(v), sum(v.values())) for k, v in groups.items() if k}


def _by_uses(rows):
    return sorted(rows, key=lambda r: (-r["uses"], r["value"]))


def coding_overview(values_in_use, cost_center_rows, use_case_rows, cards):
    """What the Use cases & cost centres tab shows next to the lists: how much each entry is used, the
    values typed that are on no list, and the cards defaulting to each cost centre."""
    cc_values = (values_in_use or {}).get("cost_center") or {}
    usage_values = (values_in_use or {}).get("usage") or {}
    cc_keys = {name_key(r["name"]) for r in cost_center_rows}
    uc_names = {name_key(r["name"]): r["name"] for r in use_case_rows}
    cc_groups, usage_groups = _by_key(cc_values), _by_key(usage_values)
    card_defaults = {}
    for c in cards:
        if c.get("default_cost_center"):
            card_defaults.setdefault(c["default_cost_center"], []).append(c["last4"])
    return {
        "cost_center_uses": {r["name"]: uses(r["name"], cc_values) for r in cost_center_rows},
        "use_case_uses": {r["id"]: uses(r["name"], usage_values) for r in use_case_rows},
        "usage_not_listed": _by_uses([{"value": v, "uses": n} for k, (v, n) in usage_groups.items() if k not in uc_names]),
        "cost_centers_not_listed": _by_uses([{"value": v, "uses": n} for k, (v, n) in cc_groups.items()
                                             if k not in cc_keys]),
        # A Cost centre box holding "Marketing" (a use case since the split) still leaves the purchase
        # without a use case, and hides under the retired Marketing cost centre otherwise.
        "cost_centers_like_use_cases": _by_uses([{"value": v, "uses": n, "use_case": uc_names[k]}
                                                 for k, (v, n) in cc_groups.items() if k in uc_names]),
        "card_defaults": {name: sorted(last4s) for name, last4s in sorted(card_defaults.items())},
    }


def new_use_case_sort(use_case_rows):
    """A new use case's sort, so it lands before "Other" (999): the highest other sort + 10, kept below
    Other's."""
    other = next((r for r in use_case_rows if name_key(r.get("name")) == "other"), None)
    s = max((int(r.get("sort") or 0) for r in use_case_rows if name_key(r.get("name")) != "other"), default=0) + 10
    if other is not None and s >= int(other.get("sort") or 0):
        s = int(other.get("sort") or 0) - 1
    return s


def use_cases_for_group(use_cases, group):
    """The active use cases a card in `group` may use: the "any group" ones and its group's own."""
    key = name_key(group)
    return [u for u in use_cases if u.get("active")
            and (not name_key(u.get("group_name")) or name_key(u.get("group_name")) == key)]


def snap_use_case(value, use_cases, group):
    """The list spelling of the use case `value` names, when it is active and allowed for the group."""
    key = name_key(value)
    if not key:
        return None
    return next((u["name"] for u in use_cases_for_group(use_cases, group) if name_key(u["name"]) == key), None)


# ---------------------------------------------------------------------------
# Vendors: the folder catalogue, the registry, proposals and suggestions
# ---------------------------------------------------------------------------
ZERO_SIM = "vendor similarity 0.00"


def _purchases(transactions):
    return [t for t in transactions if t.get("type") == "purchase"]


class _Links:
    """What the catalogue, the drill-down and the proposals read from the documents and the links."""

    def __init__(self, invoices, transactions):
        self.docs = [i for i in _live(invoices) if (i.get("vendor_folder") or "").strip()]
        self.doc_folder = {i["id"]: folder_key(i["vendor_folder"]) for i in self.docs}
        self.spelling = _folder_spellings(self.docs)
        self.used = set()
        for t in transactions:
            if t.get("invoice_id"):
                self.used.add(t["invoice_id"])
            self.used.update(t.get("also_invoice_ids") or ())
        self.purchases = _purchases(transactions)
        self.prefix = {t["id"]: descriptor_prefix(t.get("description")) for t in self.purchases}
        # Which folder each prefix's matched purchases are linked to: the majority is "its" folder.
        self.prefix_folders = {}
        for t in self.purchases:
            folder = self.doc_folder.get(t.get("invoice_id"))
            p = self.prefix[t["id"]]
            if t.get("invoice_status") == "matched" and folder and p:
                per = self.prefix_folders.setdefault(p, {})
                per[folder] = per.get(folder, 0) + 1
        self.majority = {p: _most_common(per) for p, per in self.prefix_folders.items()}
        # Indexed once: the catalogue asks for every folder (about 230 over 6,000 purchases).
        self._linked, self._missing = {}, {}
        for t in self.purchases:
            folder = self.doc_folder.get(t.get("invoice_id"))
            if folder:
                self._linked.setdefault(folder, []).append(t)
            home = self.majority.get(self.prefix[t["id"]]) if t.get("invoice_status") == "missing" else None
            if home:
                self._missing.setdefault(home, []).append(t)

    def linked(self, key):
        """The purchases linked (invoice_id) to a document in the folder."""
        return self._linked.get(key, [])

    def missing_here(self, key):
        """The purchases without a receipt whose descriptor prefix is usually linked to this folder."""
        return self._missing.get(key, [])


def _vendor_brief(vendor, fields=("id", "name", "kind")):
    return {k: vendor.get(k) for k in fields} if vendor else None


def _spellings(docs, limit=5):
    groups = {}
    for i in docs:
        v = norm_space(i.get("vendor"))
        if v:
            per = groups.setdefault(name_key(v), {})
            per[v] = per.get(v, 0) + 1
    rows = [{"text": _most_common(g), "count": sum(g.values())} for g in groups.values()]
    rows.sort(key=lambda r: (-r["count"], r["text"].casefold()))
    return (rows[:limit] if limit else rows), len(rows)


def _top(counts, n, label="prefix"):
    return [{label: k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def catalogue(invoices, transactions, registry=None):
    """One row per vendor folder (case and spaces aside) over the documents not removed: how many
    documents, how the purchases link to them, and what needs attention (the comments below)."""
    links = _Links(invoices, transactions)
    by_key = {}
    for i in links.docs:
        by_key.setdefault(links.doc_folder[i["id"]], []).append(i)
    rows = []
    for key, docs in by_key.items():
        linked = links.linked(key)
        matched = [t for t in linked if t.get("invoice_status") == "matched"]
        possible = [t for t in linked if t.get("invoice_status") == "possible"]
        zero_sim = sum(1 for t in possible if ZERO_SIM in str(t.get("match_note") or ""))
        unmatched = sum(1 for i in docs if i["id"] not in links.used)
        missing = len(links.missing_here(key))
        months = sorted(str(i["month_folder"]) for i in docs if i.get("month_folder"))
        spellings, spelling_count = _spellings(docs)
        prefixes = {}
        for t in matched:
            p = links.prefix[t["id"]]
            if p:
                prefixes[p] = prefixes.get(p, 0) + 1
        vendor = registry.folder_vendor(key) if registry is not None else None
        rows.append({
            "folder": links.spelling[key], "folder_key": key, "docs": len(docs),
            "first_month": months[0] if months else None, "last_month": months[-1] if months else None,
            "spellings": spellings, "spelling_count": spelling_count,
            "matched": len(matched), "possible": len(possible), "possible_zero_sim": zero_sim,
            "unmatched_docs": unmatched, "missing_by_prefix": missing,
            # Possibles the matcher paired on amount alone, plus the purchases a document here could
            # still answer: what an hour of AP's time fixes most.
            "attention": zero_sim + min(missing, unmatched),
            # Far more purchases missing a receipt than receipts waiting: the documents were never filed.
            "filing_gap": missing >= 5 and missing > 2 * unmatched,
            "top_prefixes": _top(prefixes, 3),
            "vendor": _vendor_brief(vendor),
        })
    return sorted(rows, key=lambda r: r["folder_key"])


CATALOGUE_SORTS = {
    "attention": lambda r: (-r["attention"], -r["docs"], r["folder_key"]),
    "volume": lambda r: (-r["docs"], r["folder_key"]),
    "name": lambda r: r["folder_key"],
}


def catalogue_page(rows, q="", sort="attention", page=1, per=50, registered="all"):
    """A page of the catalogue: filtered by `q` (folder, spellings, prefixes or vendor name) and by
    whether the folder is registered, sorted, clamped."""
    needle = name_key(q)
    out = []
    for r in rows:
        if registered == "yes" and not r.get("vendor"):
            continue
        if registered == "no" and r.get("vendor"):
            continue
        if needle:
            hay = [r.get("folder")] + [s["text"] for s in r.get("spellings") or ()]
            hay += [p["prefix"] for p in r.get("top_prefixes") or ()]
            hay.append((r.get("vendor") or {}).get("name"))
            if not any(needle in name_key(h) for h in hay if h):
                continue
        out.append(r)
    out.sort(key=CATALOGUE_SORTS.get(sort, CATALOGUE_SORTS["attention"]))
    try:
        per = int(per)
    except (TypeError, ValueError):
        per = 50
    per = max(1, min(200, per))
    pages = max(1, -(-len(out) // per))
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(pages, page))
    return {"rows": out[(page - 1) * per: page * per], "total": len(out), "page": page, "per": per, "pages": pages}


def folder_detail(name, invoices, transactions, registry=None):
    """The drill-down of one folder, or None when no document is filed under that name."""
    key = folder_key(name)
    links = _Links(invoices, transactions)
    docs = [i for i in links.docs if links.doc_folder[i["id"]] == key]
    if not key or not docs:
        return None

    def owner(kind, pattern):
        v = registry.pattern_owner(kind, pattern) if registry is not None else None
        return _vendor_brief(v, ("id", "name"))

    spellings, _ = _spellings(docs, limit=None)
    for s in spellings:
        s["pattern"] = normalise_pattern(s["text"])
        s["problem"] = pattern_problem(s["pattern"], "invoice_vendor", "exact")
        s["owner"] = owner("invoice_vendor", s["pattern"])
    stats = {}

    def stat(p):
        return stats.setdefault(p, {"prefix": p, "first_word": p.split()[0], "matched": 0, "possible": 0,
                                    "missing": 0, "possible_zero_sim": 0})
    for t in links.linked(key):
        p = links.prefix[t["id"]]
        if not p:
            continue
        s = stat(p)
        if t.get("invoice_status") in ("matched", "possible"):
            s[t["invoice_status"]] += 1
        if t.get("invoice_status") == "possible" and ZERO_SIM in str(t.get("match_note") or ""):
            s["possible_zero_sim"] += 1
    for t in links.missing_here(key):
        stat(links.prefix[t["id"]])["missing"] += 1
    prefixes = []
    for p, s in stats.items():
        s["other_folders"] = len(set(links.prefix_folders.get(p, {})) - {key})
        s["problem"] = pattern_problem(p)
        s["owner"] = owner("descriptor", p)
        prefixes.append(s)
    prefixes.sort(key=lambda s: (-(s["matched"] + s["possible"] + s["missing"]), s["prefix"]))
    unmatched = [i for i in docs if i["id"] not in links.used]
    unmatched.sort(key=lambda i: (str(i.get("month_folder") or ""), str(i.get("invoice_date") or ""), str(i["id"])),
                   reverse=True)
    vendor = registry.folder_vendor(key) if registry is not None else None
    return {"folder": links.spelling[key], "folder_key": key, "docs": len(docs), "spellings": spellings,
            "vendor": _vendor_brief(vendor), "prefixes": prefixes,
            "documents_unmatched": [{k: i.get(k) for k in ("id", "file_name", "month_folder", "invoice_date", "total",
                                                           "currency", "web_view_link", "vendor")}
                                    for i in unmatched[:100]]}


def folder_names(invoices):
    """Every folder once (its most common spelling), sorted: the vendor editor's folder list."""
    return sorted(_folder_spellings(_live(invoices)).values(), key=lambda f: (f.casefold(), f))


class RegistryIndex:
    """store.list_vendor_registry() indexed for lookups: which vendor a folder, a descriptor or a
    supplier name belongs to, and its default use case per card group."""

    def __init__(self, reg):
        reg = reg or {}
        self.vendors = {v["id"]: v for v in reg.get("vendors") or ()}
        self.folders = list(reg.get("folders") or ())
        self.patterns = list(reg.get("patterns") or ())
        self.coding = list(reg.get("coding") or ())
        self._folder = {folder_key(f.get("folder_key") or f.get("folder")): f["vendor_id"] for f in self.folders}
        self._pattern = {(p["kind"], p["pattern"]): p for p in self.patterns}
        # The patterns that may name a vendor: on (not off), of an active vendor.
        live = [p for p in self.patterns if p.get("status") != "off"
                and (self.vendors.get(p["vendor_id"]) or {}).get("active", True) is not False
                and p["vendor_id"] in self.vendors]
        self._exact = {p["pattern"]: p for p in live if p["kind"] == "descriptor" and p.get("match_type") == "exact"}
        self._prefix = {p["pattern"]: p for p in live if p["kind"] == "descriptor" and p.get("match_type") != "exact"}
        self._supplier = {p["pattern"]: p for p in live if p["kind"] == "invoice_vendor"}

    def folder_vendor(self, key):
        return self.vendors.get(self._folder.get(folder_key(key)))

    def folder_owner(self, key):
        """The vendor already holding the folder (the 409 says who)."""
        return self.folder_vendor(key)

    def pattern_owner(self, kind, pattern):
        p = self._pattern.get((kind, pattern))
        return self.vendors.get(p["vendor_id"]) if p else None

    def resolve(self, description, folder=None, invoice_vendor=None):
        """(vendor, via, text) or (None, None, None): the descriptor's patterns first (an exact one, then
        the longest prefix), then the document's folder, then the supplier name read on it."""
        tokens = descriptor_tokens(description)
        t = " ".join(tokens)
        if t:
            p = self._exact.get(t)
            if p is None:
                p = next((self._prefix[c] for c in (" ".join(tokens[:n]) for n in range(len(tokens), 0, -1))
                          if c in self._prefix), None)
            if p is not None:
                return self.vendors[p["vendor_id"]], "pattern", p["pattern"]
        if norm_space(folder):
            v = self.folder_vendor(folder)
            if v is not None and v.get("active", True) is not False:
                return v, "folder", norm_space(folder)
        supplier = normalise_pattern(invoice_vendor)
        if supplier and supplier in self._supplier:
            p = self._supplier[supplier]
            return self.vendors[p["vendor_id"]], "invoice_vendor", supplier
        return None, None, None

    def coding_row(self, vendor_id, group):
        """The coding row for the card's group, else the vendor's "any group" row, else None."""
        rows = [r for r in self.coding if r["vendor_id"] == vendor_id]
        key = name_key(group)
        if key:
            row = next((r for r in rows if name_key(r.get("group_name")) == key), None)
            if row is not None:
                return row
        return next((r for r in rows if not name_key(r.get("group_name"))), None)

    def coding_for(self, vendor_id, group):
        row = self.coding_row(vendor_id, group)
        return row["use_case_id"] if row else None

    def nested(self):
        """The vendors sorted by name, each with its folders, patterns and coding rows."""
        out = []
        for v in sorted(self.vendors.values(), key=lambda v: (str(v.get("name")).casefold(), v["id"])):
            out.append(dict(v, folders=sorted((dict(f) for f in self.folders if f["vendor_id"] == v["id"]),
                                              key=lambda f: f.get("folder_key") or ""),
                            patterns=sorted((dict(p) for p in self.patterns if p["vendor_id"] == v["id"]),
                                            key=lambda p: (p["pattern"], p["kind"])),
                            coding=sorted((dict(c) for c in self.coding if c["vendor_id"] == v["id"]),
                                          key=lambda c: name_key(c.get("group_name")))))
        return out


def _resolve_txn(registry, t, inv_by_id):
    inv = inv_by_id.get(t.get("invoice_id")) or {}
    return registry.resolve(t.get("description"), inv.get("vendor_folder"), inv.get("vendor"))


def vendor_resolves(registry, invoices, transactions):
    """{vendor_id: {"purchases", "uncoded", "missing"}}: the purchases each vendor names, through its
    patterns, its folders (the linked document's) or its supplier names."""
    out = {vid: {"purchases": 0, "uncoded": 0, "missing": 0} for vid in registry.vendors}
    inv_by_id = {i["id"]: i for i in invoices}
    for t in _purchases(transactions):
        vendor = _resolve_txn(registry, t, inv_by_id)[0]
        if vendor is None:
            continue
        c = out.setdefault(vendor["id"], {"purchases": 0, "uncoded": 0, "missing": 0})
        c["purchases"] += 1
        c["uncoded"] += 0 if str(t.get("usage") or "").strip() else 1
        c["missing"] += 1 if t.get("invoice_status") == "missing" else 0
    return out


def _codable(vendor):
    """A vendor whose purchases may get a default use case: an active supplier not coded each time."""
    return (vendor is not None and vendor.get("kind") == "supplier" and not vendor.get("code_each")
            and vendor.get("active", True) is not False)


def suggest_coding(txn, card, registry, use_cases, active_cost_centers):
    """The use case (and the card's default cost centre) to suggest for a purchase without a use case,
    from its vendor's default for the card's group, or None. A suggestion only: nothing is written."""
    if registry is None or txn.get("type") != "purchase" or str(txn.get("usage") or "").strip():
        return None
    inv = txn.get("invoice") or {}
    vendor, via, text = registry.resolve(txn.get("description"), inv.get("vendor_folder"), inv.get("vendor"))
    if not _codable(vendor):
        return None
    group = norm_space((card or {}).get("group_name"))
    row = registry.coding_row(vendor["id"], group)
    if row is None:
        return None
    use_case = next((u for u in use_cases_for_group(use_cases, group) if u["id"] == row["use_case_id"]), None)
    if use_case is None:                       # retired, or for another group's cards
        return None
    default = (card or {}).get("default_cost_center")
    cost_center = (default if default and not str(txn.get("cost_center") or "").strip()
                   and default in set(active_cost_centers or ()) else None)
    by = {"pattern": f"pattern '{text}'", "folder": "folder", "invoice_vendor": "supplier name"}[via]
    return {"usage": use_case["name"], "cost_center": cost_center, "vendor": vendor["name"],
            "source": f"{vendor['name']} · {norm_space(row.get('group_name')) or 'any group'} default (by {by})"}


SYSTEM_ACTORS = ("reread", "scheduler")


def _person_actor(updated_by):
    """Was the row last changed by a person (not a job, a re-read or schema.sql)?"""
    who = str(updated_by or "").strip()
    return bool(who) and who not in SYSTEM_ACTORS and not who.startswith(("job:", "schema:"))


def _effect(rows):
    return {"purchases": len(rows), "missing": sum(1 for t in rows if t.get("invoice_status") == "missing"),
            "uncoded": sum(1 for t in rows if not str(t.get("usage") or "").strip())}


def proposals(invoices, transactions, registry, use_cases, cards, dismissed=()):
    """What the link history suggests registering, computed on read: aliases (a descriptor prefix always
    linked to one folder), folders (one supplier filing in several folders) and coding (a vendor's
    purchases coded one way by people). Each carries the request that accepts it."""
    registry = registry if registry is not None else RegistryIndex({})
    dismissed = set(dismissed or ())
    links = _Links(invoices, transactions)
    history = [t for t in links.purchases
               if t.get("invoice_status") == "matched" and t.get("match_method") in ("rule", "manual")
               and links.doc_folder.get(t.get("invoice_id")) and links.prefix[t["id"]]]
    starts = {}                 # every whole-word start of every descriptor -> its purchases (pattern_hits)
    for t in links.purchases:
        tokens = descriptor_tokens(t.get("description"))
        for n in range(1, len(tokens) + 1):
            starts.setdefault(" ".join(tokens[:n]), []).append(t)

    def hit(pattern):
        return starts.get(pattern, [])

    def folder_of(t):
        return links.doc_folder[t["invoice_id"]]

    out = {"alias": [], "folders": [], "coding": []}

    def alias(key, pattern, n):
        folder = links.spelling[key]
        vendor = registry.folder_vendor(key)
        if vendor is not None:
            accept = {"method": "POST", "path": f"/api/admin/vendors/{vendor['id']}/patterns",
                      "body": {"pattern": pattern, "kind": "descriptor", "match_type": "prefix", "source": "proposal"}}
        else:
            accept = {"method": "POST", "path": "/api/admin/vendors",
                      "body": {"name": folder, "folders": [folder], "patterns": [{"pattern": pattern}],
                               "source": "proposal"}}
        out["alias"].append({"id": f"alias:{key}:{pattern}", "type": "alias",
                             "text": (f'Purchases starting "{pattern}" were linked to {folder} {n} times, '
                                      "never to another folder."),
                             "evidence": {"folder": folder, "pattern": pattern, "links": n},
                             "effect": _effect(hit(pattern)), "accept": accept, "_rank": n})

    by_prefix, by_word = {}, {}
    for t in history:
        by_prefix.setdefault(links.prefix[t["id"]], []).append(t)
        by_word.setdefault(links.prefix[t["id"]].split()[0], []).append(t)
    for p, rows in by_prefix.items():
        folders = {folder_of(t) for t in rows}
        if (len(folders) == 1 and len(rows) >= 3 and pattern_problem(p) is None
                and registry.pattern_owner("descriptor", p) is None):
            alias(next(iter(folders)), p, len(rows))
    for w, rows in by_word.items():
        folders = {folder_of(t) for t in rows}
        if (len(w) >= 4 and w not in PROCESSOR_WORDS and len(folders) == 1 and len(rows) >= 3
                and len({links.prefix[t["id"]] for t in rows}) >= 2 and registry.pattern_owner("descriptor", w) is None):
            alias(next(iter(folders)), w, len(rows))

    # folders: one prefix linked (at least twice each) to 2-4 folders that are not one vendor already
    merged = {}
    for p, rows in by_prefix.items():
        if p.split()[0] in PROCESSOR_WORDS:
            continue
        counts = {}
        for t in rows:
            counts[folder_of(t)] = counts.get(folder_of(t), 0) + 1
        counts = {k: n for k, n in counts.items() if n >= 2}
        if not 2 <= len(counts) <= 4:
            continue
        held = {k: registry.folder_vendor(k) for k in counts}
        vendor_ids = {v["id"] for v in held.values() if v is not None}
        if len(vendor_ids) > 1 or (len(vendor_ids) == 1 and all(v is not None for v in held.values())):
            continue                       # two vendors hold them (AP decides), or they are one vendor already
        m = merged.setdefault(tuple(sorted(counts)), {"prefixes": [], "links": {}, "vendor_ids": vendor_ids})
        m["prefixes"].append(p)
        for k, n in counts.items():
            m["links"][k] = m["links"].get(k, 0) + n
    for keys, m in merged.items():
        spell = [links.spelling[k] for k in keys]
        links_by = {links.spelling[k]: n for k, n in m["links"].items()}
        if m["vendor_ids"]:
            vid = next(iter(m["vendor_ids"]))
            others = [links.spelling[k] for k in keys if registry.folder_vendor(k) is None]
            accept = {"method": "POST", "path": f"/api/admin/vendors/{vid}/folders", "body": {"folders": others}}
        else:
            top = sorted(m["links"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            accept = {"method": "POST", "path": "/api/admin/vendors",
                      "body": {"name": links.spelling[top], "folders": spell, "source": "proposal"}}
        prefixes = sorted(m["prefixes"])
        listed = " and ".join(f"{links.spelling[k]} ({m['links'][k]})" for k in keys)
        quoted = ", ".join(f'"{p}"' for p in prefixes)
        out["folders"].append({
            "id": "folders:" + "|".join(keys), "type": "folders",
            "text": f"Purchases starting {quoted} were linked to {listed}: are these folders one vendor?",
            "evidence": {"folders": spell, "prefixes": prefixes, "links": links_by},
            "effect": _effect([t for t in links.purchases if links.prefix[t["id"]] in set(prefixes)]),
            "accept": accept, "_rank": sum(m["links"].values())})

    # coding: a registered supplier's purchases that people coded one way, per card group and overall
    groups = {c["last4"]: norm_space(c.get("group_name")) for c in cards}
    inv_by_id = {i["id"]: i for i in invoices}
    per_vendor = {}
    for t in links.purchases:
        vendor = _resolve_txn(registry, t, inv_by_id)[0]
        if not _codable(vendor):
            continue
        group = groups.get(t.get("card_last4"), "")
        per_vendor.setdefault(vendor["id"], {}).setdefault(name_key(group), (group, []))[1].append(t)
        if group:
            per_vendor[vendor["id"]].setdefault("", ("", []))[1].append(t)
    for vid, by_group in per_vendor.items():
        vendor = registry.vendors[vid]
        for gkey, (group, rows) in by_group.items():
            coded = [t for t in rows if str(t.get("usage") or "").strip() and _person_actor(t.get("updated_by"))]
            if len(coded) < 3:
                continue
            counts = {}
            for t in coded:
                k = name_key(t["usage"])
                counts[k] = counts.get(k, 0) + 1
            top_key = _most_common(counts)
            if counts[top_key] < 0.75 * len(coded):
                continue
            spelled = next(t["usage"] for t in coded if name_key(t["usage"]) == top_key)
            name = snap_use_case(spelled, use_cases, group)
            if name is None:
                continue
            use_case = next(u for u in use_cases if u["name"] == name)
            if registry.coding_for(vid, group) == use_case["id"]:
                continue
            on = f"{group} cards" if group else "all cards"
            out["coding"].append({
                "id": f"coding:{vid}:{gkey}:{use_case['id']}", "type": "coding",
                "text": f"{counts[top_key]} of {len(coded)} purchases from {vendor['name']} on {on} were coded {name} by people.",
                "evidence": {"vendor": vendor["name"], "group": group, "use_case": name, "rows": len(coded),
                             "top": counts[top_key]},
                "effect": _effect(rows),
                "accept": {"method": "PUT", "path": f"/api/admin/vendors/{vid}/coding",
                           "body": {"group_name": group, "use_case_id": use_case["id"]}},
                "_rank": len(coded)})

    result, seen = [], set()
    for kind in ("alias", "folders", "coding"):
        for p in sorted(out[kind], key=lambda p: (-p["_rank"], p["id"])):
            if p["id"] in dismissed or p["id"] in seen:
                continue
            seen.add(p["id"])
            result.append({k: v for k, v in p.items() if k != "_rank"})
    return result


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
PERSON_STOPWORDS = {"limited", "ltd", "inc", "llc", "corp", "co", "company", "the"}


def person_tokens(name):
    """The words of a name that tell people apart: "NEW FRONTIER GLOBAL LIMITED" -> (new, frontier, global)."""
    return tuple(t for t in re.findall(r"[a-z0-9]+", str(name or "").lower()) if t not in PERSON_STOPWORDS)


def likely_duplicate_people(people):
    """[[id_a, id_b]]: people who are probably the same, to offer a merge. Same words, or the words of one
    inside the other's when the shorter has at least two ("New Frontier Global" and "NEW FRONTIER GLOBAL
    LIMITED"; never "Amy" inside "Amy Chen")."""
    rows = sorted(people, key=lambda p: (str(p.get("name") or "").casefold(), str(p.get("id"))))
    tokens = [(p, person_tokens(p.get("name"))) for p in rows]
    out = []
    for i, (a, ta) in enumerate(tokens):
        for b, tb in tokens[i + 1:]:
            if not ta or not tb:
                continue
            small, big = sorted((set(ta), set(tb)), key=len)
            if ta == tb or (len(small) >= 2 and small <= big):
                out.append([a["id"], b["id"]])
    return out
