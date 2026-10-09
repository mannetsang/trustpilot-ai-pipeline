"""Google Chat cards: one space per credit card.

Each card's Chat space has an incoming webhook; the webhook URLs are secrets
and arrive as one JSON object in CARD_WEBHOOKS_JSON ({"1610": "https://...",
"default": "https://..."}). A card without its own entry falls back to
"default" (or GCHAT_WEBHOOK_URL). Same card layout as the review pipelines.
"""

import calendar
import json

import requests


def load_webhooks(raw_json, default_url=""):
    hooks = {}
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                hooks = {str(k).strip(): str(v).strip() for k, v in parsed.items() if v}
        except json.JSONDecodeError as exc:
            print(f"CARD_WEBHOOKS_JSON is not valid JSON: {exc}")
    if default_url and "default" not in hooks:
        hooks["default"] = default_url
    return hooks


def webhook_for(hooks, last4):
    return hooks.get(last4) or hooks.get("default") or ""


def month_label(year, month):
    return f"{calendar.month_name[int(month)]} {int(year)}"


def money(amount, currency="CAD"):
    sign = "-" if float(amount) < 0 else ""
    return f"{sign}{currency} {abs(float(amount)):,.2f}"


def _lines(txns, limit=25):
    lines = []
    for t in txns[:limit]:
        day = str(t.get("txn_date") or "")[5:]
        extra = f" ({money(t['source_amount'], t['source_currency'])})" if t.get("source_amount") else ""
        lines.append(f"{day} · {t.get('description') or '-'} · <b>{money(t['amount'], t.get('currency') or 'CAD')}</b>{extra}")
    if len(txns) > limit:
        lines.append(f"… and {len(txns) - limit} more")
    return "<br>".join(lines) or "—"


def missing_invoices_card(card, year, month, missing, possible, dashboard_url="", folder_url="", uncoded=0):
    """cardsV2 payload listing a card's purchases that still have no invoice."""
    label = card.get("label") or f"Card ending {card.get('last4', '????')}"
    holder = card.get("holder_name") or ""
    if holder and holder.lower() in label.lower():
        holder = ""          # the label already names the holder
    total_missing = sum(float(t["amount"]) for t in missing)
    currency = (missing or possible or [{}])[0].get("currency") or "CAD"
    head = (f"\U0001f9fe <b>Invoices needed – {label}</b>" + (f" ({holder})" if holder else "") + "<br>"
            f"<b>Month:</b> {month_label(year, month)}<br>"
            f"<b>Missing:</b> {len(missing)} purchase{'s' if len(missing) != 1 else ''} · {money(total_missing, currency)}"
            + (f"<br><b>To confirm:</b> {len(possible)} possible match{'es' if len(possible) != 1 else ''}" if possible else "")
            + (f"<br><b>Without cost centre:</b> {uncoded}" if uncoded else ""))
    sections = [{"widgets": [{"textParagraph": {"text": head}}]}]
    if missing:
        sections.append({"header": "No invoice found", "widgets": [{"textParagraph": {"text": _lines(missing)}}]})
    if possible:
        sections.append({"header": "Possible matches – please confirm",
                         "widgets": [{"textParagraph": {"text": _lines(possible, 10)}}]})
    buttons = []
    link = f"{dashboard_url.rstrip('/')}/#/{int(year)}/{int(month):02d}?card={card.get('last4', '')}" if dashboard_url else ""
    if link:
        buttons.append({"text": "Open dashboard", "onClick": {"openLink": {"url": link}},
                        "color": {"red": 0.0, "green": 0.478, "blue": 1.0, "alpha": 1.0}})
    if folder_url:
        buttons.append({"text": "Invoice folder", "onClick": {"openLink": {"url": folder_url}}})
    footer = ("Upload the invoice or receipt to the Invoice folder for this month (a sub-folder named after the "
              "vendor), or open the dashboard to link an existing file, add the cost centre and usage, or mark "
              "the charge as having no invoice.")
    sections.append({"widgets": [{"textParagraph": {"text": footer}}] + ([{"buttonList": {"buttons": buttons}}] if buttons else [])})
    card_id = f"ap-{card.get('last4', 'x')}-{int(year)}-{int(month):02d}"
    return {"cardsV2": [{"cardId": card_id, "card": {"sections": sections}}]}


def post(webhook_url, payload, timeout=15):
    """-> (ok, detail)"""
    if not webhook_url:
        return False, "no webhook configured for this card (CARD_WEBHOOKS_JSON)"
    try:
        r = requests.post(webhook_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"chat request failed: {exc}"
    if r.status_code != 200:
        return False, f"chat webhook returned {r.status_code} {r.text[:200]}"
    return True, "sent"
