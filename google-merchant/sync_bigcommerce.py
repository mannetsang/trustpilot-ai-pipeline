"""Sync the BigCommerce catalogue into a Merchant Center API data source.

    python google-merchant/sync_bigcommerce.py --label CA --dry-run
    python google-merchant/sync_bigcommerce.py --label CA --limit 25
    python google-merchant/sync_bigcommerce.py --label CA

One offer per BigCommerce variant. Every visible, purchasable variant with a
price and an image becomes a ProductInput in the storefront's "<storefront>
API feed" data source (create it first with data_sources.py). Variants of the
same product share an item_group_id, so Merchant Center shows them as one
product with options.

A full run (no --limit / --sku) also deletes offers that are in the data
source but no longer in the catalogue, so hidden or deleted products drop
out of Google within a day. --no-delete turns that off.

Credentials: the BigCommerce token comes from Secret Manager through
lib/secrets.py (BIGCOMMERCE_<store_hash>_ACCESS_TOKEN, or BC_ACCESS_TOKEN
locally); Merchant API auth is Application Default Credentials.

Known data-quality handling, from inspecting the store:
- upc/mpn hold Excel-mangled values such as "6.14043E+11" and gtin sometimes
  holds an import-shifted column ("Default Tax Class"). GTINs are kept only
  when they are 8/12/13/14 digits with a valid check digit; MPNs only when
  they look like part numbers. Otherwise identifier_exists=false is sent.
- Offer ids are the variant SKU when it is URL-safe, else bc-v<variant id>,
  because SKUs like TP BLUE BAG "ST" cannot sit in a resource path.
- Google's Personal Hardships policy disapproves feed text that targets hair
  loss, alopecia or medical conditions, which the storefront copy does freely.
  Those phrases are stripped from the title and description that go to Google
  (HARDSHIP / neutralize); the storefront page itself is untouched.
- The account's shipping rates are weight-based, so an offer without a
  shipping weight is disapproved outright. Products with no weight in
  BigCommerce get a per-category default in grams (DEFAULT_WEIGHT_G) until a
  real weight is entered; the run summary counts how many needed it.
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser

from google.api_core import exceptions as gexc
from google.shopping import type as gtype
from google.shopping.merchant_datasources_v1 import DataSourcesServiceClient, ListDataSourcesRequest
from google.shopping.merchant_products_v1 import (
    Availability,
    Condition,
    DeleteProductInputRequest,
    InsertProductInputRequest,
    ListProductsRequest,
    ProductAttributes,
    ProductInput,
    ProductInputsServiceClient,
    ProductsServiceClient,
    Shipping,
    ShippingWeight,
)

from merchant_api import (
    STOREFRONTS,
    account_parent,
    add_account_argument,
    display_name_for,
    explain_api_error,
    resolve_account,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.stores import by_feed_label  # noqa: E402

# BigCommerce store behind each feed label. Only the .ca store is known so far;
# add the others here once their store hashes and tokens exist.
BC_API = "https://api.bigcommerce.com/stores/{hash}"

# Google product taxonomy ids (taxonomy-with-ids.en-US.txt). Rules are tried in
# order against the product's BigCommerce category paths and name; the first
# match wins. Without this Google guesses, and it filed toupees under Hats.
GOOGLE_CATEGORIES = {
    "hair_extensions": "4057",   # Apparel & Accessories > Clothing Accessories > Hair Accessories > Hair Extensions
    "wig_glue_tape": "7306",     # ... > Hair Accessories > Wig Accessories > Wig Glue & Tape
    "wig_accessories": "7305",   # ... > Hair Accessories > Wig Accessories
    "wigs": "181",               # ... > Hair Accessories > Wigs
    "hair_care": "486",          # Health & Beauty > Personal Care > Hair Care
    "hair_loss": "4766",         # ... > Hair Care > Hair Loss Treatments
    "mannequins": "3803",        # Business & Industrial > Retail > Display Mannequins
    "hair_care_kits": "8452",    # Health & Beauty > Personal Care > Hair Care > Hair Care Kits
    "cosmetic_tools": "2619",    # Health & Beauty > Personal Care > Cosmetics > Cosmetic Tools
}
# The storefronts name the same products in five languages, and these rules used
# to speak only English: FR, NL and ES mapped barely two thirds of their
# catalogues, and an unmapped offer is one Google categorises by guesswork -
# which is how toupees first landed under Hats. Local terms sit alongside the
# English ones in each rule so the ordering and the vetoes still hold.
CATEGORY_RULES = [
    # (key, match regex, veto regex) tried in order on "category paths | name", lowercase.
    # A rule only fires when the match hits and the veto (if any) does not.
    ("mannequins", r"mannequin|styling block|wig head|wig holder|wig stand"
                   r"|t[eê]te\s+(?:de\s+)?mannequin|porte[- ]perruque"
                   r"|pruikenkop|pruikenstandaard|cabeza\s+de\s+maniqu[íi]|soporte\s+para\s+peluca"
                   r"|per[üu]ckenkopf|per[üu]ckenst[äa]nder", None),
    ("cosmetic_tools", r"microblad|microshad|lip blush|eyelash|makeup|academy|training kit"
                       r"|maquillage|formation|maquillaje|schmink|wimper|pesta[ñn]a", None),
    ("hair_care_kits", r"gift set|\bkit\b|coffret|geschenkset|set\s+de\s+regalo",
                       r"extension|toupee|hairpiece|starter|perruque|pruik|peluca|per[üu]cke"),
    ("wig_accessories", r"\bclips?\b|\bcombs?\b|wig cap|hook needle|colou?r ring|needle|hair ?net"
                        r"|pince|peigne|bonnet|anneau\s+de\s+couleur|filet\s+[àa]\s+cheveux|aiguille"
                        r"|klem|kam|pruikmuts|kleurenring|haarnet(?:je)?"
                        r"|clip|peine|gorro|anillo\s+de\s+color|redecilla|aguja"
                        r"|klammer|kamm|per[üu]ckenkappe|farbring|farbmuster|haarnetz|nadel",
                        r"topper|toupee|\bextensions?\b|hair system|\bwigs?\b"
                        r"|perruque|postiche|pruik|haarstuk|peluca|per[üu]cke|haarteil"),
    ("hair_extensions", r"\bextensions?\b|\bweft\b|tape[- ]in\b|nail[- ]tip|i-?tip|micro ?link|clip[- ]in\b|\bhalo\b|pony ?tail"
                        r"|m[èe]ches?|rajouts?|queue\s+de\s+cheval"
                        r"|haarextensies|inhaar|paardenstaart"
                        r"|extensiones|coleta"
                        r"|haarverl[äa]ngerung|tressen|pferdeschwanz",
                        r"topper|toupee|\bwigs?\b|\btabs?\b|remover|\brolls?\b|pliers|\btools?\b|beads"
                        r"|perruque|pruik|peluca|per[üu]cke"),
    ("wig_glue_tape", r"\btapes?\b|\bglue\b|adhesive|solvent|remover|scalp protector|\bliner\b|\bbond\b|knot sealer|c-22|no shine|attachment & removal"
                      r"|colle|ruban|adh[ée]sif|dissolvant|d[ée]collant"
                      r"|lijm|kleefband|plakband|verwijderaar"
                      r"|pegamento|cinta|adhesivo|disolvente|removedor"
                      r"|kleber|klebeband|haftband|l[öo]ser|entferner", None),
    ("wigs", r"toupee|topper|hair system|\bwigs?\b|sheitel|hair unit|hairpieces?\b|hair replacement"
             r"|perruques?|postiche|compl[ée]ment\s+capillaire|proth[èe]se\s+capillaire|pi[èe]ce\s+capillaire"
             r"|pruik(?:en)?|haarstuk(?:je)?|haarwerk"
             r"|pelucas?|bis[oñn]u?[ée]|pr[óo]tesis\s+capilar"
             r"|per[üu]cke(?:n)?|haarteil|toupet|haarersatz|zweitfrisur",
             r"\bclips?\b|\bcombs?\b|\bstand\b|catalog|klammer|standaard|soporte"),
    ("wig_accessories", r"scissor|brush|applicator|glove|template|swatch|spray bottle|\btools?\b"
                        r"|ciseaux|brosse|gant|gabarit|[ée]chantillon|vaporisateur"
                        r"|schaar|borstel|handschoen|sjabloon|staalkaart"
                        r"|tijeras|cepillo|guante|plantilla|muestra"
                        r"|schere|b[üu]rste|handschuh|schablone", None),
    ("hair_loss", r"thinning|hair loss|regrowth|minoxidil"
                  r"|repousse|alop[ée]cie|haargroei|crecimiento|haarwuchs", None),
    ("hair_care", r"shampoo|conditioner|mousse|\bgel\b|serum|hair care|styling|leave-in|spray|cream|\boil\b|detangl|\bperm\b|color|colour|dye"
                  r"|shampooing|apr[èe]s[- ]shampooing|soin|huile|cr[èe]me|d[ée]m[êe]l"
                  r"|conditioner|verzorging|olie|cr[èe]me"
                  r"|champ[úu]|acondicionador|aceite|crema|cuidado"
                  r"|sp[üu]lung|pflege|[öo]l|creme|haarfarbe", None),
    ("wig_accessories", r"supplies|fournitures|benodigdheden|suministros|zubeh[öo]r", None),
    ("wigs", r"\bmen\b|\bwomen\b|frontal|closure|\bbase\b|\blace\b|\bdensity\b"
             r"|homme|femme|dentelle|densit[ée]"
             r"|heren|dames|kant|dichtheid"
             r"|hombre|mujer|encaje|densidad"
             r"|herren|damen|spitze|dichte", None),
]
# Products that exist in BigCommerce for internal ordering and must never reach Google.
INTERNAL_CATEGORY = re.compile(r"office supplies|services?$", re.I)
INTERNAL_NAME = re.compile(
    r"extra charge|coffee|sugar|garbage|envelope|thermal paper|batter(y|ies)|catalog|price list|handling fee|"
    r"^ou_|\\bpens\\b|toilet paper|paper towel|\\bservices?\\b|base cut|deposit|gift card|consultation|certificad|certification|nanatest|\\btest\\d*\\b|"
    r"^hairpiece repair|specialist program|\\bcourse\\b",
    re.I,
)
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,50}$")
WEIGHT_UNITS = {"LBS": "lb", "KGS": "kg", "Ounces": "oz", "Grams": "g", "Pounds": "lb", "Kilograms": "kg"}
# Used only when /v2/store is forbidden, which is the .nl, .fr and .de tokens.
# Not a guess: .es is the one EU token broad enough to answer and it reports
# KGS, where .ca and .com report LBS, so the EU storefronts are configured
# metric and these three are assumed to match .es rather than .ca. Reading it
# backwards would misstate every shipping weight by a factor of 2.2. The feed
# follows the storefront's own setting so Google and the site agree; whether
# that setting is right is a separate question - see docs and CLAUDE.md.
DEFAULT_WEIGHT_UNIT = {"CA": "lb", "US": "lb", "NL": "kg", "FR": "kg", "ES": "kg", "DE": "kg"}
TITLE_MAX = 150
DESCRIPTION_MAX = 5000
MAX_ADDITIONAL_IMAGES = 10

# Google's Personal Hardships policy disapproves listings whose text targets hair
# loss, alopecia, chemotherapy or medical conditions (67 offers in the first
# review). The storefront may say it; the feed may not. A term, any qualifier in
# front of it ("frontal hair loss"), a leading "for"/"due to", and chained terms
# ("chemo and alopecia hair loss") are removed as one phrase.
_HARDSHIP_TERM = r"(?:hair[- ]?loss|thinning(?:[- ]hair)?|alopecia|chemo(?:therapy)?|cancer(?:\s+patients?)?|medical(?!\s+(?:grade|stainless))|receding(?:\s+hairlines?)?|balding)"
_HARDSHIP_QUAL = r"(?:(?:frontal|crown|partial|female|male|early|advanced|severe|genetic|hereditary|temporary|permanent|pattern|bald(?:ing)?)\s+)*"
HARDSHIP = re.compile(
    r"(?:\b(?:for|from|due to|with|caused by|after|during|suffering from|experiencing)\s+)?"
    + _HARDSHIP_QUAL + r"\b" + _HARDSHIP_TERM + r"\b"
    + r"(?:(?:\s*(?:and|&|,|/|or)\s*|\s+)" + _HARDSHIP_QUAL + _HARDSHIP_TERM + r"\b)*",
    re.I,
)

# Google enforces Personal Hardships in the feed's own language, and the EU
# storefronts describe the same products in their own words: a French page says
# "perte de cheveux" where the Canadian one says "hair loss". The English
# pattern above would have let all four EU feeds through untouched, which is
# how Gen'C Beauty collected 66 personal_hardships_policy_violation offers.
#
# Each storefront is matched against English *as well as* its own language,
# because imported copy routinely mixes the two inside one description.
#
# Deliberately absent from these: the local words for "medical". English can
# afford it with a lookahead for "medical grade"; "qualite medicale",
# "medizinische Qualitat" and "calidad medica" are ordinary product copy, and
# stripping them would damage good descriptions to no purpose. The policy is
# about targeting hardship, not about the word medical.
_HARDSHIP_LANG = {
    "fr": r"perte\s+de\s+cheveux|chute\s+de\s+cheveux|cheveux\s+clairsem[ée]s?|"
          r"alop[ée]cie|chimioth[ée]rapie|cancer|calvitie|d[ée]garnie?s?|"
          r"cheveux\s+fins\s+et\s+clairsem[ée]s?",
    "nl": r"haaruitval|haarverlies|dunner\s+wordend\s+haar|dunnend\s+haar|"
          r"alopecia|chemotherapie|kanker|kaalheid|kalend|kaal\s+worden",
    "es": r"p[ée]rdida\s+d(?:e|el)\s+(?:cabello|pelo)|ca[íi]da\s+d(?:e|el)\s+(?:cabello|pelo)|"
          r"cabello\s+ralo|pelo\s+ralo|alopecia|quimioterapia|c[áa]ncer|calvicie",
    "de": r"haarausfall|haarverlust|d[üu]nner\s+werdendes?\s+haar|lichtes?\s+haar|"
          r"alopezie|chemotherapie|krebs|glatze|geheimratsecken",
}
HARDSHIP_BY_LANG = {
    lang: re.compile(
        r"(?:\b(?:pour|contre|en cas de|voor|bij|para|por|f[üu]r|bei|gegen)\s+)?"
        r"\b(?:" + terms + r")\b",
        re.I,
    )
    for lang, terms in _HARDSHIP_LANG.items()
}

# Shipping weight in grams by Google product category, used only when
# BigCommerce holds no weight. The account's shipping rates are weight-based,
# so Google disapproves weightless offers (127 in the first review, including
# the Six Teeth Comb Clips). A conservative default keeps them live until real
# weights are entered; the run summary reports how many still need one.
DEFAULT_WEIGHT_G = {
    GOOGLE_CATEGORIES["wigs"]: 150,
    GOOGLE_CATEGORIES["hair_extensions"]: 150,
    GOOGLE_CATEGORIES["wig_glue_tape"]: 120,
    GOOGLE_CATEGORIES["wig_accessories"]: 60,
    GOOGLE_CATEGORIES["hair_care"]: 350,
    GOOGLE_CATEGORIES["hair_loss"]: 350,
    GOOGLE_CATEGORIES["mannequins"]: 1500,
    GOOGLE_CATEGORIES["hair_care_kits"]: 500,
    GOOGLE_CATEGORIES["cosmetic_tools"]: 200,
    None: 200,
}


# --------------------------------------------------------------------------- BigCommerce


class BigCommerce:
    def __init__(self, store_hash, token):
        self.base = BC_API.format(hash=store_hash)
        self.token = token

    def get(self, path, **params):
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-Auth-Token": self.token, "Accept": "application/json"})
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    wait = int(exc.headers.get("X-Rate-Limit-Time-Reset-Ms", "5000")) / 1000
                    time.sleep(min(wait, 60) + 0.5)
                    continue
                if exc.code >= 500 and attempt < 5:
                    time.sleep(2**attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < 5:
                    time.sleep(2**attempt)
                    continue
                raise
        raise RuntimeError(f"BigCommerce request kept failing: {path}")

    def pages(self, path, **params):
        page = 1
        while True:
            data = self.get(path, limit=250, page=page, **params)
            yield from data["data"]
            if page >= data["meta"]["pagination"]["total_pages"]:
                return
            page += 1


def load_store(bc, shop):
    """Store URL, currency and weight unit - from the API, or the registry.

    /v2/store needs a scope the .nl, .fr and .de tokens do not carry: they are
    catalogue-only and answer 403, the same way they do on /v2/orders. That is
    not a reason to refuse to build a feed, because lib/stores.py already holds
    the two facts that matter - the canonical origin and the currency - and the
    weight unit has a safe default. A storefront whose token is narrow still
    gets a correct feed; only the weight unit is assumed, and it is reported.
    """
    try:
        store = bc.get("/v2/store")
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        print(f"note: /v2/store is {exc.code} for this token (catalogue-only scope); "
              f"using lib/stores.py and assuming {DEFAULT_WEIGHT_UNIT.get(shop.market, 'kg')} weights")
        return {
            "url": shop.origin,
            "currency": shop.currency,
            "weight_unit": DEFAULT_WEIGHT_UNIT.get(shop.market, "kg"),
            "store_api": False,
        }
    return {
        "url": store["secure_url"].rstrip("/"),
        "currency": store["currency"],
        "weight_unit": WEIGHT_UNITS.get(store["weight_units"], "lb"),
        "store_api": True,
    }


def load_brands(bc):
    return {b["id"]: b["name"].strip() for b in bc.pages("/v3/catalog/brands", include_fields="name")}


def load_categories(bc):
    cats = {c["id"]: c for c in bc.pages("/v3/catalog/categories", include_fields="name,parent_id,is_visible")}
    paths = {}
    for cid, cat in cats.items():
        names, cur, seen = [], cat, set()
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            names.append(cur["name"].strip())
            cur = cats.get(cur["parent_id"]) if cur["parent_id"] else None
        paths[cid] = " > ".join(reversed(names))
    return paths, cats


# --------------------------------------------------------------------------- mapping


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append(" ")


def clean_text(raw, limit):
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(raw)
    text = html.unescape(" ".join(parser.parts))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


# Function words in all five feed languages. Removing a noun phrase from the
# middle of a clause leaves these stranded, and a run of them is the signature
# of the hole: German and Dutch park the verb at the end, so "um Haarausfall
# oder dunner werdendes Haar zu kaschieren" becomes "um oder zu kaschieren" -
# damage in the middle of a sentence that still ends perfectly well.
_FUNCTION_WORD = (
    r"and|or|for|with|to|of|the|a|an|due|from|caused|by|after|during|in|on"
    r"|et|ou|de|du|des|la|le|les|une?|aux?|pour|contre|dues?|li[ée]e?s?|en|dans"
    r"|of|om|te|voor|bij|het|een|met|naar"
    r"|y|o|del|el|los|las|para|por|con|una?"
    r"|und|oder|um|zu|von|f[üu]r|der|die|das|den|dem|bei|gegen|mit"
)
_DANGLING = re.compile(r"\b(?:" + _FUNCTION_WORD + r")[\s,;:]*[.!?]?$", re.I)
_RUN = re.compile(r"\b(?:" + _FUNCTION_WORD + r")\b(?:\s+\b(?:" + _FUNCTION_WORD + r")\b){2,}", re.I)


def _dangles(text):
    """True when the sentence now ends on a connector, article or preposition."""
    return bool(_DANGLING.search(text))


def _collapsed(before, after):
    """True when the removal pushed function words together mid-sentence.

    Three in a row is vanishingly rare in real copy and almost certain after a
    noun phrase is cut out from between them. Compared against the original so
    a sentence that genuinely reads that way is left alone.
    """
    return bool(_RUN.search(after)) and not _RUN.search(before)


def neutralize(text, lang="en"):
    """Remove Personal Hardships phrasing from feed text and tidy what is left.

    Always strips the English phrasings, then the storefront's own language on
    top. A .fr description that quotes an English spec sheet carries both.
    """
    if not text:
        return text
    patterns = [HARDSHIP]
    local = HARDSHIP_BY_LANG.get((lang or "en").lower())
    if local:
        patterns.append(local)
    if not any(p.search(text) for p in patterns):
        return text

    # Sentence by sentence, because removing a phrase mid-clause wrecks the
    # grammar around it and the tidy-up below only speaks English. Real copy:
    # "Couvre la zone clairsemee ou la calvitie due a la perte de cheveux."
    # holds two hardship terms in one clause, and stripping both leaves
    # "Couvre la zone clairsemee ou la due a la." A sentence left that short,
    # or ending on a connector, is dropped whole - which is what an editor
    # would do, and what the policy wants anyway.
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        stripped = sentence
        for pattern in patterns:
            stripped = pattern.sub(" ", stripped)
        if stripped == sentence:
            kept.append(sentence)
            continue
        tidy = re.sub(r"\s+", " ", stripped).strip()
        words = re.findall(r"\w+", tidy)
        if len(words) < 4 or _dangles(tidy) or _collapsed(sentence, tidy):
            continue
        kept.append(stripped)
    out = " ".join(kept)
    if not re.sub(r"\W+", "", out):
        # Everything was hardship. Better a bare phrase than an empty field.
        out = text
        for pattern in patterns:
            out = pattern.sub(" ", out)
    out = re.sub(r"\(\s*\)|\[\s*\]", " ", out)  # brackets emptied by the removal
    out = re.sub(r"\s+([,.;:)\]])", r"\1", out)  # space left before punctuation
    out = re.sub(r"\b(?:for|with|and|or|to|of)\s*(?=[)\]\-\u2013|,.;:]|$)", "", out, flags=re.I)  # dangling connectors
    out = re.sub(r"(\s[-\u2013|]\s*){2,}", r"\1", out)  # doubled separators
    return re.sub(r"\s+", " ", out).strip(" -\u2013|,")


def valid_gtin(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits != str(value or "").strip() or len(digits) not in (8, 12, 13, 14):
        return None
    total = 0
    for i, ch in enumerate(reversed(digits[:-1])):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    if (10 - total % 10) % 10 != int(digits[-1]):
        return None
    if set(digits) == {"0"}:
        return None
    return digits


def plausible_mpn(value):
    value = (value or "").strip()
    if not value or len(value) > 70 or "E+" in value.upper() or " " in value and len(value) > 30:
        return None
    if value.lower() in ("default tax class", "n/a", "none", "na"):
        return None
    return value


def offer_id_for(variant):
    sku = (variant.get("sku") or "").strip()
    if sku and SAFE_ID.match(sku):
        return sku
    return f"bc-v{variant['id']}"


def group_id_for(product):
    sku = (product.get("sku") or "").strip()
    if sku and SAFE_ID.match(sku):
        return sku
    return f"bc-p{product['id']}"


def google_category(product, category_paths):
    """Pick a Google product category id from the product name, then its BigCommerce categories.

    The name is tried first because category paths are noisy (a glue sits under
    "Hairpiece Tape", a topper under "Women > Wigs"); vetoes are checked against
    the name only.
    """
    name = (product["name"] or "").lower()
    paths = " | ".join(category_paths[c] for c in product.get("categories", []) if c in category_paths).lower()
    for haystack in (name, paths):
        for key, pattern, veto in CATEGORY_RULES:
            if re.search(pattern, haystack) and not (veto and re.search(veto, name)):
                return GOOGLE_CATEGORIES[key]
    return None


def is_internal(product, category_paths):
    """True for office stock, surcharges, services and other non-merchandise."""
    if any(INTERNAL_CATEGORY.search(category_paths[c]) for c in product.get("categories", []) if c in category_paths):
        return True
    if INTERNAL_NAME.search(product["name"] or ""):
        return True
    return not product.get("price") or float(product["price"]) <= 0


def money(amount, currency):
    return gtype.Price(amount_micros=int(round(float(amount) * 1_000_000)), currency_code=currency)


def availability_for(product, variant):
    if product["availability"] == "preorder":
        return Availability.PREORDER
    tracking = product["inventory_tracking"]
    if tracking == "none":
        return Availability.IN_STOCK
    level = variant["inventory_level"] if tracking == "variant" else product["inventory_level"]
    return Availability.IN_STOCK if (level or 0) > 0 else Availability.OUT_OF_STOCK


def build_offers(product, ctx, brands, category_paths, stats=None):
    """Return ([(offer_id, ProductInput)], [(offer_id, reason)]) for one product.

    stats, if given, is a Counter that receives per-offer notes such as
    default_weight and neutralized_text.
    """
    offers, skipped = [], []
    stats = stats if stats is not None else Counter()
    variants = product.get("variants") or []
    if not variants:
        return offers, [(f"bc-p{product['id']}", "no variants")]

    if product["type"] != "physical":
        return offers, [(offer_id_for(v), f"type {product['type']}") for v in variants]
    if is_internal(product, ctx["category_paths"]):
        return offers, [(offer_id_for(v), "internal item") for v in variants]
    if product["availability"] == "disabled":
        return offers, [(offer_id_for(v), "availability disabled") for v in variants]
    if product.get("is_price_hidden"):
        return offers, [(offer_id_for(v), "price hidden") for v in variants]

    images = sorted(product.get("images") or [], key=lambda i: (not i.get("is_thumbnail"), i.get("sort_order", 0)))
    image_urls = [i["url_zoom"] for i in images if i.get("url_zoom")]
    raw_name, raw_description = product["name"].strip(), clean_text(product.get("description"), DESCRIPTION_MAX)
    lang = ctx.get("language", "en")
    name, description = neutralize(raw_name, lang), neutralize(raw_description, lang)
    description = description or name
    if (name, description) != (raw_name, raw_description or raw_name):
        stats["neutralized_text"] += 1
    brand = brands.get(product.get("brand_id")) or ctx["brand_default"]
    product_types = [category_paths[c] for c in product.get("categories", []) if c in category_paths][:10]
    gpc = google_category(product, category_paths)
    multi = len(variants) > 1
    group_id = group_id_for(product) if multi else None
    base_link = ctx["url"] + product["custom_url"]["url"]
    product_mpn = plausible_mpn(product.get("mpn"))
    product_gtin = valid_gtin(product.get("gtin")) or valid_gtin(product.get("upc"))

    for v in variants:
        oid = offer_id_for(v)
        if v.get("purchasing_disabled"):
            skipped.append((oid, "purchasing disabled"))
            continue
        price = v.get("price") if v.get("price") is not None else product["price"]
        if not price or float(price) <= 0:
            skipped.append((oid, "no price"))
            continue
        image = v.get("image_url") or (image_urls[0] if image_urls else None)
        if not image:
            skipped.append((oid, "no image"))
            continue

        title = name
        if multi:
            labels = [o["label"].strip() for o in v.get("option_values", []) if o.get("label")]
            if labels:
                title = f"{title} - {', '.join(labels)}"
        title = title[:TITLE_MAX].rstrip()

        link = base_link
        if multi and v.get("sku"):
            link += ("&" if "?" in link else "?") + urllib.parse.urlencode({"sku": v["sku"]})

        attrs = ProductAttributes(
            title=title,
            description=description,
            link=link,
            image_link=image,
            additional_image_links=[u for u in image_urls if u != image][:MAX_ADDITIONAL_IMAGES],
            availability=availability_for(product, v),
            condition=Condition.NEW if product.get("condition", "New") == "New" else Condition.USED,
            price=money(price, ctx["currency"]),
            brand=brand,
            product_types=product_types,
        )
        if gpc:
            attrs.google_product_category = gpc

        sale = v.get("sale_price") if v.get("sale_price") is not None else product.get("sale_price")
        if sale and 0 < float(sale) < float(price):
            attrs.sale_price = money(sale, ctx["currency"])

        gtin = valid_gtin(v.get("gtin")) or valid_gtin(v.get("upc")) or product_gtin
        mpn = plausible_mpn(v.get("mpn")) or product_mpn
        if gtin:
            attrs.gtins = [gtin]
        if mpn and mpn != gtin:
            attrs.mpn = mpn
        if not gtin and not mpn:
            attrs.identifier_exists = False

        weight = v.get("calculated_weight") or v.get("weight") or product.get("weight")
        if weight and float(weight) > 0:
            attrs.shipping_weight = ShippingWeight(value=float(weight), unit=ctx["weight_unit"])
        else:
            attrs.shipping_weight = ShippingWeight(value=DEFAULT_WEIGHT_G.get(gpc, DEFAULT_WEIGHT_G[None]), unit="g")
            stats["default_weight"] += 1

        if product.get("is_free_shipping") or v.get("is_free_shipping"):
            attrs.shipping = [Shipping(country=ctx["country"], price=money(0, ctx["currency"]))]

        if multi:
            attrs.item_group_id = group_id
        if product.get("availability") == "preorder" and product.get("preorder_release_date"):
            attrs.availability_date = product["preorder_release_date"]

        offers.append(
            (
                oid,
                ProductInput(
                    offer_id=oid,
                    content_language=ctx["language"],
                    feed_label=ctx["feed_label"],
                    product_attributes=attrs,
                ),
            )
        )
    return offers, skipped


# --------------------------------------------------------------------------- Merchant API


def find_data_source(parent, storefront):
    wanted = display_name_for(storefront)
    for ds in DataSourcesServiceClient().list_data_sources(request=ListDataSourcesRequest(parent=parent)):
        if ds.display_name == wanted:
            return ds.name
    sys.exit(f"error: data source {wanted!r} not found; run data_sources.py create first")


def insert_with_retry(client, request, attempts=6):
    for attempt in range(attempts):
        try:
            return client.insert_product_input(request=request)
        except (gexc.ResourceExhausted, gexc.ServiceUnavailable, gexc.DeadlineExceeded, gexc.InternalServerError):
            if attempt == attempts - 1:
                raise
            time.sleep(min(2**attempt, 30) + 0.1 * attempt)


def existing_offer_ids(parent, data_source):
    ids = set()
    for p in ProductsServiceClient().list_products(request=ListProductsRequest(parent=parent, page_size=1000)):
        if p.data_source == data_source:
            ids.add(p.offer_id)
    return ids


# --------------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_account_argument(parser)
    parser.add_argument("--label", required=True, help="Feed label of the storefront to sync, e.g. CA")
    parser.add_argument("--limit", type=int, help="Stop after this many offers (testing; disables deletion)")
    parser.add_argument("--sku", nargs="+", help="Only products whose SKU is in this list (disables deletion)")
    parser.add_argument("--dry-run", action="store_true", help="Build everything, call nothing on Google")
    parser.add_argument("--no-delete", action="store_true", help="Keep offers Google has that BigCommerce no longer does")
    parser.add_argument("--workers", type=int, default=8, help="Parallel inserts (default 8)")
    parser.add_argument("--report", help="Write skipped/failed offers to this CSV")
    parser.add_argument("--sample", type=int, default=0, help="Print this many built offers as JSON (dry-run aid)")
    args = parser.parse_args(argv)

    label = args.label.upper()
    # lib/stores.py is the one registry: hash, language, market, brand and the
    # token's Secret Manager id, for all six storefronts. This script used to
    # carry a STORES dict holding only "CA", which is why it could sync one
    # market and died on any other.
    shop = by_feed_label(label)
    storefront = next((s for s in STOREFRONTS if s[1] == label), None)
    if not storefront:
        sys.exit(f"error: feed label {label!r} is not in STOREFRONTS")
    account = resolve_account(args)
    parent = account_parent(account)

    token = shop.token()
    if not token:
        sys.exit(f"error: no BigCommerce token stored for {shop.key} "
                 f"({shop.token_secret}); see docs/credentials.md")
    bc = BigCommerce(shop.hash, token)
    store = load_store(bc, shop)
    # The storefront's own currency is the only one its prices may be stated in.
    # A mismatch means the token points at the wrong store, and sending USD
    # prices to a EUR feed would be worse than failing. Only worth checking
    # when the store API answered; otherwise the currency came from the
    # registry and is trivially equal.
    if store.get("store_api") and store["currency"] != shop.currency:
        sys.exit(f"error: {shop.key} is {shop.currency} in lib/stores.py but the "
                 f"BigCommerce store reports {store['currency']}; refusing to sync")
    ctx = {
        **store,
        "brand_default": shop.brand,
        "feed_label": label,
        "language": shop.language,
        "country": shop.market,
    }
    print(f"store {shop.hash} ({store['url']}, {store['currency']}) -> {parent} "
          f"feed {label} ({shop.language}/{shop.market})")

    brands = load_brands(bc)
    category_paths, _ = load_categories(bc)
    ctx["category_paths"] = category_paths

    t0 = time.time()
    offers, skipped, seen_products = [], [], 0
    notes = Counter()
    params = {"is_visible": "true", "include": "variants,images,custom_fields"}
    if args.sku:
        params["sku:in"] = ",".join(args.sku)
    for product in bc.pages("/v3/catalog/products", **params):
        seen_products += 1
        built, skips = build_offers(product, ctx, brands, category_paths, notes)
        offers.extend(built)
        skipped.extend(skips)
        if args.limit and len(offers) >= args.limit:
            offers = offers[: args.limit]
            break

    dupes = [oid for oid, n in Counter(o for o, _ in offers).items() if n > 1]
    if dupes:
        print(f"warning: {len(dupes)} duplicate offer ids, keeping the first of each: {dupes[:5]}")
        seen, unique = set(), []
        for oid, pi in offers:
            if oid not in seen:
                seen.add(oid)
                unique.append((oid, pi))
        offers = unique

    print(
        f"built {len(offers)} offers from {seen_products} products in {time.time() - t0:.0f}s; "
        f"skipped {len(skipped)}: {dict(Counter(r for _, r in skipped))}"
    )
    stats = Counter()
    for _, pi in offers:
        a = pi.product_attributes
        stats["with_gtin" if a.gtins else "without_gtin"] += 1
        stats["with_weight" if "shipping_weight" in a else "without_weight"] += 1
        stats[Availability(a.availability).name.lower()] += 1
        if "sale_price" in a:
            stats["on_sale"] += 1
    print(f"offer stats: {dict(stats)}")
    print(f"notes: {dict(notes)} (default_weight = offers sent with a category default because BigCommerce has no weight; "
          f"neutralized_text = products whose title/description lost policy wording)")
    names = {v: k for k, v in GOOGLE_CATEGORIES.items()}
    gpc_counts = Counter(names.get(pi.product_attributes.google_product_category, "unmapped") for _, pi in offers)
    print(f"google categories: {dict(gpc_counts.most_common())}")

    for _, pi in offers[: args.sample]:
        print(json.dumps(json.loads(ProductInput.to_json(pi)), indent=1)[:2500])

    if args.dry_run:
        _write_report(args.report, skipped, [])
        print("dry run: nothing sent to Google")
        return 0

    data_source = find_data_source(parent, storefront[0])
    client = ProductInputsServiceClient()
    done, failed = 0, []
    lock = threading.Lock()
    t1 = time.time()

    def push(item):
        oid, pi = item
        try:
            insert_with_retry(client, InsertProductInputRequest(parent=parent, product_input=pi, data_source=data_source))
            return oid, None
        except gexc.GoogleAPICallError as exc:
            return oid, f"{type(exc).__name__}: {exc.message[:300] if hasattr(exc, 'message') else str(exc)[:300]}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for oid, err in (f.result() for f in as_completed(pool.submit(push, o) for o in offers)):
            with lock:
                if err:
                    failed.append((oid, err))
                else:
                    done += 1
                if (done + len(failed)) % 500 == 0:
                    print(f"  {done + len(failed)}/{len(offers)} sent ({time.time() - t1:.0f}s)")
    print(f"inserted {done}, failed {len(failed)} in {time.time() - t1:.0f}s")
    for oid, err in failed[:10]:
        print(f"  FAILED {oid}: {err}")

    deleted = 0
    full_run = not args.limit and not args.sku
    if full_run and not args.no_delete:
        current = {oid for oid, _ in offers} | {oid for oid, _ in failed}
        stale = sorted(existing_offer_ids(parent, data_source) - current)
        if stale and len(stale) > max(50, len(offers) // 4):
            print(f"refusing to delete {len(stale)} stale offers (more than a quarter of the catalogue); pass --no-delete or check the feed")
        else:
            for oid in stale:
                name = f"{parent}/productInputs/{ctx['language']}~{label}~{oid}"
                try:
                    client.delete_product_input(request=DeleteProductInputRequest(name=name, data_source=data_source))
                    deleted += 1
                except gexc.GoogleAPICallError as exc:
                    failed.append((oid, f"delete: {exc}"))
            print(f"deleted {deleted} stale offers")
    elif not full_run:
        print("partial run: stale-offer deletion skipped")

    _write_report(args.report, skipped, failed)
    return 1 if failed else 0


def _write_report(path, skipped, failed):
    if not path:
        return
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["offer_id", "outcome", "detail"])
        for oid, reason in skipped:
            writer.writerow([oid, "skipped", reason])
        for oid, err in failed:
            writer.writerow([oid, "failed", err])
    print(f"report written to {path}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except gexc.GoogleAPICallError as exc:
        sys.exit(f"error: {explain_api_error(exc)}")
