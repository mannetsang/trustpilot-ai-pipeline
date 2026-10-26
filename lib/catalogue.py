"""
Which BigCommerce products are merchandise, and which are internal plumbing.

The storefronts double as an internal ordering system: office coffee, sugar,
garbage bags, thermal paper and pens sit in a "Discount Zone > Office Supplies"
category, surcharges like "Extra Charge" are products, and so are services,
catalogues and deposits. They are real rows with real order counts - "Extra
Charge" is the single best-selling item on .ca by units - so anything that
ranks or totals the catalogue has to set them aside or report nonsense.

These patterns were first written for the Merchant Center feed, where letting
them through put garbage bags in Google Shopping. `google-merchant/
sync_bigcommerce.py` still carries its own copy; fold it into this module next
time that file is touched.
"""
import re

INTERNAL_CATEGORY = re.compile(r"office supplies|services?$", re.I)
INTERNAL_NAME = re.compile(
    r"extra charge|coffee|sugar|garbage|envelope|thermal paper|batter(y|ies)|catalog|price list|handling fee|"
    r"^ou_|\bpens\b|toilet paper|paper towel|\bservices?\b|base cut|deposit|gift card|consultation|certificad|"
    r"certification|nanatest|\btest\d*\b|^hairpiece repair|specialist program|\bcourse\b",
    re.I,
)


def is_internal(name, sku=None, category_paths=()):
    """True for office stock, surcharges, services and other non-merchandise."""
    if name and INTERNAL_NAME.search(name):
        return True
    if sku and INTERNAL_NAME.search(sku):
        return True
    return any(INTERNAL_CATEGORY.search(path) for path in category_paths if path)
