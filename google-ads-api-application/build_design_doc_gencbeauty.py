"""Build the Google Ads API design document PDF for Gen'C Beauty's Basic Access application.

Version 2.0 (15 September 2026). Supersedes the Superhairpieces v1.0 document
(build_design_doc.py), which was submitted for the same Google Ads account in
error: the account belongs to Gen'C Beauty (gencbeauty.com). Same tool, same
API services and guardrails; company, channels, users and mockups rewritten.
"""
import math
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
                                Table, TableStyle, PageBreak, CondPageBreak, ListFlowable, ListItem,
                                KeepTogether)
from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon

OUT = "GenC_Beauty_Google_Ads_API_Design_Document.pdf"
W, H = letter
M = 0.85 * inch

INK = colors.HexColor("#1B1B2F")
NAVY = colors.HexColor("#3B2A4A")      # primary (deep plum)
ACCENT = colors.HexColor("#9C5B45")    # accent (terracotta)
MUTED = colors.HexColor("#6B7280")
RULE = colors.HexColor("#DDD6E3")
PANEL = colors.HexColor("#F6F3F7")
GREEN = colors.HexColor("#2E7D5B")
RED = colors.HexColor("#B4453C")

COMPANY = "Gen'C Beauty"
TOOL = "Gen'C Ads Console"
VERSION_LINE = "Version 2.0  |  15 September 2026"


def st(name, **kw):
    base = dict(fontName="Helvetica", fontSize=10, leading=14.5, textColor=INK, alignment=TA_LEFT)
    base.update(kw)
    return ParagraphStyle(name, **base)


S = {
    "cover_kicker": st("ck", fontSize=10, textColor=ACCENT, leading=14, spaceAfter=10),
    "cover_title": st("ct", fontName="Helvetica-Bold", fontSize=30, leading=36, textColor=INK, spaceAfter=8),
    "cover_sub": st("cs", fontSize=13, leading=19, textColor=MUTED, spaceAfter=28),
    "h1": st("h1", fontName="Helvetica-Bold", fontSize=17, leading=22, textColor=NAVY, spaceBefore=6, spaceAfter=10, keepWithNext=1),
    "h2": st("h2", fontName="Helvetica-Bold", fontSize=12, leading=16, textColor=INK, spaceBefore=12, spaceAfter=5, keepWithNext=1),
    "body": st("body", spaceAfter=7),
    "small": st("small", fontSize=8.5, leading=11.5, textColor=MUTED),
    "cell": st("cell", fontSize=9, leading=12),
    "cellb": st("cellb", fontName="Helvetica-Bold", fontSize=9, leading=12),
    "cellh": st("cellh", fontName="Helvetica-Bold", fontSize=8.5, leading=11, textColor=colors.white),
    "callout": st("callout", fontSize=10, leading=14.5, textColor=INK),
}


def P(txt, style="body"):
    return Paragraph(txt, S[style])


def bullets(items):
    return ListFlowable(
        [ListItem(P(i), leftIndent=12) for i in items],
        bulletType="bullet", start="•", leftIndent=14, bulletFontSize=9, spaceAfter=6)


def table(rows, widths, header=True, zebra=True):
    data = []
    for r, row in enumerate(rows):
        data.append([Paragraph(c, S["cellh"] if (header and r == 0) else S["cell"]) for c in row])
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), NAVY)]
    if zebra:
        for r in range(1 if header else 0, len(rows)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), PANEL))
    t.setStyle(TableStyle(style))
    t.spaceAfter = 10
    return t


def callout(txt):
    t = Table([[Paragraph(txt, S["callout"])]], colWidths=[W - 2 * M])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
        ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    t.spaceBefore = 4
    t.spaceAfter = 12
    return t


# ---------- page furniture ----------
def on_page(canv, doc):
    canv.saveState()
    if doc.page > 1:
        canv.setFont("Helvetica", 8)
        canv.setFillColor(MUTED)
        canv.drawString(M, H - 0.55 * inch, COMPANY + "  |  Google Ads API Design Document  |  Confidential")
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        canv.line(M, H - 0.62 * inch, W - M, H - 0.62 * inch)
        canv.line(M, 0.72 * inch, W - M, 0.72 * inch)
        canv.drawString(M, 0.5 * inch, VERSION_LINE)
        canv.drawRightString(W - M, 0.5 * inch, "Page %d" % doc.page)
    else:
        canv.setFillColor(NAVY)
        canv.rect(0, H - 0.35 * inch, W, 0.35 * inch, stroke=0, fill=1)
        canv.setFillColor(ACCENT)
        canv.rect(0, H - 0.35 * inch - 4, W, 4, stroke=0, fill=1)
    canv.restoreState()


# ---------- diagrams ----------
def box(d, x, y, w, h, title, sub=None, fill=colors.white, stroke=NAVY, tc=INK):
    d.add(Rect(x, y, w, h, rx=4, ry=4, fillColor=fill, strokeColor=stroke, strokeWidth=1))
    d.add(String(x + w / 2, y + h / 2 + (3 if sub else -3), title, textAnchor="middle",
                 fontName="Helvetica-Bold", fontSize=8.5, fillColor=tc))
    if sub:
        d.add(String(x + w / 2, y + h / 2 - 8, sub, textAnchor="middle",
                     fontName="Helvetica", fontSize=7, fillColor=MUTED))


def arrow(d, x1, y1, x2, y2, label=None, color=NAVY):
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=1))
    ang = math.atan2(y2 - y1, x2 - x1)
    L = 6
    p1 = (x2 - L * math.cos(ang - 0.4), y2 - L * math.sin(ang - 0.4))
    p2 = (x2 - L * math.cos(ang + 0.4), y2 - L * math.sin(ang + 0.4))
    d.add(Polygon([x2, y2, p1[0], p1[1], p2[0], p2[1]], fillColor=color, strokeColor=color))
    if label:
        d.add(String((x1 + x2) / 2, (y1 + y2) / 2 + 4, label, textAnchor="middle",
                     fontName="Helvetica", fontSize=6.5, fillColor=MUTED))


def architecture():
    dw = W - 2 * M
    d = Drawing(dw, 250)
    box(d, 0, 165, 130, 48, "Google Ads API", "GoogleAdsService (GAQL)", fill=PANEL)
    box(d, 0, 42, 130, 48, "Google Ads API", "Mutate services", fill=PANEL)
    d.add(String(65, 225, "GOOGLE", textAnchor="middle", fontName="Helvetica-Bold", fontSize=7, fillColor=MUTED))
    d.add(Rect(160, 20, 200, 215, rx=6, ry=6, fillColor=colors.white, strokeColor=RULE, strokeWidth=1, strokeDashArray=[3, 2]))
    d.add(String(260, 225, "GEN'C BEAUTY INTERNAL (Google Cloud Run, private)", textAnchor="middle",
                 fontName="Helvetica-Bold", fontSize=7, fillColor=MUTED))
    box(d, 175, 165, 170, 42, "Reporting Sync Job", "scheduled, read-only")
    box(d, 175, 105, 170, 42, "AI Recommendation Engine", "rules + LLM, proposes changes")
    box(d, 175, 45, 170, 42, "Change Executor", "applies approved changes only")
    box(d, 390, 105, 120, 48, "Operations Database", "Postgres: ads, orders, stock", fill=PANEL)
    box(d, 390, 165, 120, 48, "Operations Dashboard", "SSO, internal team only", fill=PANEL)
    d.add(String(450, 225, "INTERNAL STAFF", textAnchor="middle", fontName="Helvetica-Bold", fontSize=7, fillColor=MUTED))
    arrow(d, 130, 186, 175, 186, "metrics")
    arrow(d, 175, 66, 130, 66, "approved mutates")
    arrow(d, 345, 172, 390, 150, "store")
    arrow(d, 390, 134, 345, 134, "read")
    arrow(d, 345, 116, 390, 116, "proposals")
    arrow(d, 450, 165, 450, 153, "view / approve")
    arrow(d, 390, 108, 345, 78, "approved")
    d.add(String(dw / 2, 4, "Figure 1. High-level data flow. All API traffic originates from one private service; no external party has access.",
                 textAnchor="middle", fontName="Helvetica-Oblique", fontSize=7.5, fillColor=MUTED))
    return d


NAV = ["Overview", "Campaigns", "Approvals", "Rules", "Audit log"]


def wireframe_dashboard():
    dw = W - 2 * M
    d = Drawing(dw, 300)
    d.add(Rect(0, 20, dw, 270, rx=6, ry=6, fillColor=colors.white, strokeColor=NAVY, strokeWidth=1.2))
    d.add(Rect(0, 262, dw, 28, fillColor=NAVY, strokeColor=NAVY))
    d.add(String(12, 272, TOOL, fontName="Helvetica-Bold", fontSize=9, fillColor=colors.white))
    for i, t in enumerate(NAV):
        d.add(String(130 + i * 54, 272, t, fontName="Helvetica", fontSize=7.5,
                     fillColor=colors.white if i != 1 else colors.HexColor("#E8B4A0")))
    d.add(String(dw - 12, 272, "Internal staff only", textAnchor="end",
                 fontName="Helvetica", fontSize=6.5, fillColor=colors.white))
    d.add(String(12, 246, "Account:  Gen'C Beauty - gencbeauty.com   |   Date range: Last 30 days   |   Status: Enabled",
                 fontName="Helvetica", fontSize=7.5, fillColor=MUTED))
    kpis = [("Cost", "$7,440"), ("Conversions", "1,184"), ("Conv. value", "$45,840"), ("ROAS", "6.2x"), ("CTR", "2.9%")]
    tw = (dw - 24 - 4 * 8) / 5
    for i, (k, v) in enumerate(kpis):
        x = 12 + i * (tw + 8)
        d.add(Rect(x, 196, tw, 40, rx=3, ry=3, fillColor=PANEL, strokeColor=RULE))
        d.add(String(x + 8, 222, k, fontName="Helvetica", fontSize=7, fillColor=MUTED))
        d.add(String(x + 8, 205, v, fontName="Helvetica-Bold", fontSize=11, fillColor=INK))
    cols = ["Campaign", "Type", "Budget/day", "Cost", "Conv.", "ROAS", "AI flag"]
    xs = [12, 175, 228, 282, 336, 376, 412]
    d.add(Rect(12, 172, dw - 24, 16, fillColor=PANEL, strokeColor=RULE))
    for x, c in zip(xs, cols):
        d.add(String(x + 4, 177, c, fontName="Helvetica-Bold", fontSize=7, fillColor=INK))
    rows = [
        ("Shopping - K-Beauty Skincare", "Shopping", "$90", "$2,610", "512", "7.4x", ""),
        ("PMax - Hair Care & Texture", "PMax", "$60", "$1,720", "298", "6.1x", "Budget review"),
        ("Search - Brand (Gen'C Beauty)", "Search", "$25", "$640", "171", "9.8x", ""),
        ("Search - Salon Supplies & Wax", "Search", "$40", "$1,090", "142", "5.2x", ""),
        ("Shopping - Nail Equipment", "Shopping", "$30", "$820", "38", "3.9x", ""),
        ("Search - Competitor Terms", "Search", "$20", "$560", "23", "1.6x", "Pause suggested"),
    ]
    for r, row in enumerate(rows):
        y = 152 - r * 20
        d.add(Line(12, y - 6, dw - 12, y - 6, strokeColor=RULE, strokeWidth=0.4))
        for x, c in zip(xs, row):
            col, fn = INK, "Helvetica"
            if c == "Budget review":
                col, fn = ACCENT, "Helvetica-Bold"
            if c == "Pause suggested":
                col, fn = RED, "Helvetica-Bold"
            d.add(String(x + 4, y, c, fontName=fn, fontSize=7, fillColor=col))
    d.add(String(dw / 2, 6, "Figure 2. Campaign overview (mockup). Data is read via GAQL and refreshed on a schedule; visible only to signed-in staff.",
                 textAnchor="middle", fontName="Helvetica-Oblique", fontSize=7.5, fillColor=MUTED))
    return d


def wireframe_approvals():
    dw = W - 2 * M
    d = Drawing(dw, 250)
    d.add(Rect(0, 20, dw, 220, rx=6, ry=6, fillColor=colors.white, strokeColor=NAVY, strokeWidth=1.2))
    d.add(Rect(0, 212, dw, 28, fillColor=NAVY, strokeColor=NAVY))
    d.add(String(12, 222, TOOL, fontName="Helvetica-Bold", fontSize=9, fillColor=colors.white))
    for i, t in enumerate(NAV):
        d.add(String(130 + i * 54, 222, t, fontName="Helvetica", fontSize=7.5,
                     fillColor=colors.white if i != 2 else colors.HexColor("#E8B4A0")))
    d.add(String(dw - 12, 222, "Internal staff only", textAnchor="end",
                 fontName="Helvetica", fontSize=6.5, fillColor=colors.white))
    d.add(String(12, 196, "Pending AI recommendations (3)   -   nothing is sent to Google Ads until a staff member clicks Approve",
                 fontName="Helvetica-Bold", fontSize=7.5, fillColor=INK))
    cards = [
        ("Pause ad group", "Shopping - K-Beauty Skincare  >  Anua Heartleaf Toner", "ENABLED  ->  PAUSED",
         "Reason: all 3 SKUs in the ad group have 0 units on hand and 0 inbound in our inventory system. Stock rule; reversible."),
        ("Increase daily budget", "PMax - Hair Care & Texture", "$60  ->  $72 (+20%)",
         "Reason: ROAS 6.1x over 14 days, lost impression share (budget) 38%. At the 20% daily cap."),
        ("Add negative keywords (5)", "Search - Salon Supplies & Wax", '"car wax", "candle wax", "wax museum", "ear wax", "surfboard wax"',
         "Reason: 0 conversions on 304 clicks in 30 days. Applies at campaign level."),
    ]
    for i, (title, camp, change, reason) in enumerate(cards):
        y = 140 - i * 52
        d.add(Rect(12, y, dw - 24, 46, rx=3, ry=3, fillColor=PANEL, strokeColor=RULE))
        d.add(String(22, y + 32, title, fontName="Helvetica-Bold", fontSize=8, fillColor=INK))
        d.add(String(140, y + 32, camp, fontName="Helvetica", fontSize=7.5, fillColor=MUTED))
        d.add(String(22, y + 19, change, fontName="Helvetica", fontSize=7.5, fillColor=NAVY))
        d.add(String(22, y + 7, reason, fontName="Helvetica", fontSize=6.5, fillColor=MUTED))
        bx = dw - 24 - 130
        d.add(Rect(bx, y + 14, 58, 18, rx=3, ry=3, fillColor=GREEN, strokeColor=GREEN))
        d.add(String(bx + 29, y + 20, "Approve", textAnchor="middle", fontName="Helvetica-Bold", fontSize=7, fillColor=colors.white))
        d.add(Rect(bx + 66, y + 14, 58, 18, rx=3, ry=3, fillColor=colors.white, strokeColor=RED))
        d.add(String(bx + 95, y + 20, "Dismiss", textAnchor="middle", fontName="Helvetica-Bold", fontSize=7, fillColor=RED))
    d.add(String(dw / 2, 6, "Figure 3. Approval queue (mockup). Each card maps to one mutate operation and is logged with approver, time and before/after values.",
                 textAnchor="middle", fontName="Helvetica-Oblique", fontSize=7.5, fillColor=MUTED))
    return d


# ---------- content ----------
def build():
    doc = BaseDocTemplate(OUT, pagesize=letter, leftMargin=M, rightMargin=M, topMargin=0.95 * inch, bottomMargin=0.95 * inch,
                          title="Google Ads API Design Document - Gen'C Beauty", author=COMPANY,
                          subject="Basic Access application")
    frame = Frame(M, 0.95 * inch, W - 2 * M, H - 1.9 * inch, id="f", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=on_page)])
    s = []

    # ---- cover
    s.append(Spacer(1, 1.6 * inch))
    s.append(P("GOOGLE ADS API  |  BASIC ACCESS APPLICATION", "cover_kicker"))
    s.append(P(TOOL, "cover_title"))
    s.append(P("Internal campaign reporting and AI-assisted campaign automation tool<br/>for Gen'C Beauty's own Google Ads account", "cover_sub"))
    meta = [
        ["Company", "Gen'C Beauty (online retailer of beauty, personal-care and salon-professional products)"],
        ["Website", "gencbeauty.com"],
        ["Tool type", "Internal tool - used exclusively by Gen'C Beauty employees"],
        ["Access requested", "Basic Access for Gen'C Beauty's own Google Ads account, applied for from our Google Cloud project"],
        ["API contact", "Manne Tsang, Head of Digital Transformation - manne@superhairpieces.com"],
        ["Document", "Version 2.0, 15 September 2026 (supersedes version 1.0 of 2 September 2026)"],
    ]
    t = Table([[Paragraph(a, S["cellb"]), Paragraph(b, S["cell"])] for a, b in meta], colWidths=[1.4 * inch, W - 2 * M - 1.4 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    s.append(t)
    s.append(Spacer(1, 0.5 * inch))
    s.append(P("This document is submitted in support of Gen'C Beauty's Google Ads API Basic Access application. It describes what the tool does, who uses it, "
               "which API services it calls, how data is handled, and includes interface mockups. The tool is not offered to any third party.", "small"))
    s.append(PageBreak())

    # ---- 1 summary
    s.append(P("1. Executive summary", "h1"))
    s.append(P("Gen'C Beauty is a Canadian online retailer and authorised reseller of beauty and personal-care products: Korean skincare, hair care and "
               "textured-hair products, cosmetics, and salon-professional supplies and equipment. We sell through our own BigCommerce storefront, "
               "<b>gencbeauty.com</b>, and on the Amazon and Walmart Canada marketplaces, shipping from our warehouses in the Greater Toronto Area. "
               "Google Ads is the primary paid acquisition channel for gencbeauty.com. It is not used for the marketplaces, which have their own advertising platforms."))
    s.append(P("We are building <b>" + TOOL + "</b>, a module of our existing internal operations dashboard, with two functions:"))
    s.append(bullets([
        "<b>Campaign reporting.</b> Fetch key performance data (cost, clicks, conversions, conversion value, impression share, search terms, product-level Shopping performance) "
        "from our own Google Ads account on a schedule and store it alongside the order, margin and inventory data the dashboard already holds from Amazon, Walmart "
        "and our inventory system, so staff see one consolidated view.",
        "<b>AI-assisted campaign management.</b> An automation layer that analyses that data, proposes changes (budget adjustments, pausing under-performing or out-of-stock "
        "campaigns and ad groups, negative keywords, bid-strategy target changes, ad-copy drafts) and, only after a staff member approves, applies the change through the API.",
    ]))
    s.append(callout("<b>Audience: internal staff only.</b> The tool is used by Gen'C Beauty's e-commerce team to manage Gen'C Beauty's own advertising. "
                     "It is not sold, licensed, white-labelled or exposed to any external advertiser, agency or client, and it only accesses the Google Ads account we own."))
    s.append(P("<b>Document history.</b> Version 1.0 (2 September 2026) described this tool in the context of Superhairpieces, our sister company, which shares "
               "our e-commerce team and the internal platform described here. This version describes the same tool for the Gen'C Beauty Google Ads account to "
               "which this application relates. The API services, guardrails and data handling are unchanged.", "small"))

    # ---- 2 company
    s.append(P("2. Company background", "h1"))
    s.append(P("Gen'C Beauty is a first-party retailer and authorised reseller of beauty and personal-care products and the owner of the Gen'C Beauty brand. "
               "The range covers Korean skincare (Beauty of Joseon, Anua, SOME BY MI, TIRTIR, SKIN1004, Round Lab, Torriden and others), hair care and "
               "textured-hair products (Mielle, Adore, Luster's, ORS, Shea Moisture), cosmetics, and salon-professional supplies and equipment such as wax, "
               "disposables, nail equipment, tools and massage tables. Our inventory system tracks roughly 10,600 products and 2,800 kits across our "
               "warehouses in the Greater Toronto Area. Customers are retail consumers in Canada and the United States, and salon, spa and nail "
               "professionals buying for their businesses."))
    s.append(KeepTogether(table([
        ["Channel", "Platform", "Advertising"],
        ["gencbeauty.com", "Own BigCommerce storefront", "Google Ads: Search, Shopping (Merchant Center 670525760), Performance Max, Remarketing. The subject of this application."],
        ["Amazon (amazon.ca, amazon.com)", "Marketplace; about 5,100 SKUs, mostly Fulfilled by Amazon", "Amazon Ads (not Google Ads)"],
        ["Walmart Canada", "Marketplace; Walmart Fulfillment Services", "Walmart Connect (not Google Ads)"],
    ], [1.8 * inch, 2.2 * inch, W - 2 * M - 4.0 * inch])))
    s.append(P("Today Google Ads performance is exported from the Google Ads interface into spreadsheets and matched to gencbeauty.com orders by hand. "
               "With a catalogue of more than 10,000 low-unit-price SKUs, that cannot tell us which products are profitable to advertise, which are being "
               "advertised while out of stock, or how the storefront's return compares with the marketplaces, whose data is already in the dashboard. "
               "The API lets us bring Google Ads into the same system and standardise the optimisation process."))

    # ---- 3 tool overview
    s.append(P("3. Tool overview", "h1"))
    s.append(P("3.1 Module A - Campaign reporting and data sync", "h2"))
    s.append(P("A scheduled job queries our Google Ads account with GAQL (Google Ads Query Language) through <b>GoogleAdsService.SearchStream</b> and writes the results "
               "to our internal Postgres database, the same database that holds our Amazon, Walmart and inventory data. The staff dashboard reads from that database, "
               "not from the API directly, so dashboard traffic never generates API calls."))
    s.append(table([
        ["Report", "Resources / fields", "Frequency"],
        ["Account and campaign performance", "customer, campaign, campaign_budget; metrics.cost_micros, impressions, clicks, conversions, conversions_value, ctr, search_impression_share, search_budget_lost_impression_share", "Every 6 hours"],
        ["Ad group and keyword performance", "ad_group, ad_group_criterion (keywords), keyword_view; metrics as above plus quality_score components", "Daily"],
        ["Search terms", "search_term_view; metrics.clicks, cost_micros, conversions", "Daily"],
        ["Shopping / product performance", "shopping_performance_view; segments.product_item_id (joined to our SKU records), product_title; core metrics", "Daily"],
        ["Ads and assets", "ad_group_ad, asset, asset_group (PMax); approval status, performance labels", "Daily"],
        ["Change history", "change_event; who/what changed, for the audit log", "Daily"],
        ["Recommendations", "recommendation; Google's own suggestions, shown next to AI suggestions", "Daily"],
    ], [1.7 * inch, W - 2 * M - 1.7 * inch - 1.0 * inch, 1.0 * inch]))

    s.append(P("3.2 Module B - AI-assisted campaign automation", "h2"))
    s.append(P("The recommendation engine combines deterministic rules (thresholds set by our team, plus stock and margin data from our own systems) with a large "
               "language model used to explain and rank suggestions and to draft ad copy. It never writes to Google Ads on its own. Every proposed change lands in an "
               "<b>Approval queue</b> (Figure 3); a staff member reviews the reason and the before/after values and approves or dismisses it. Approved changes are "
               "executed as single, targeted mutate operations."))
    s.append(table([
        ["Automation", "API service / operation", "Guardrail"],
        ["Adjust campaign daily budget", "CampaignBudgetService.MutateCampaignBudgets", "Max +/-20% per day per campaign; account-level monthly cap"],
        ["Pause / enable campaign or ad group", "CampaignService.MutateCampaigns, AdGroupService.MutateAdGroups", "Pause only after N days below ROAS floor; always reversible"],
        ["Pause ad group while its products are out of stock", "AdGroupService.MutateAdGroups (PAUSED); re-enable proposed on receipt", "Only when every SKU in the group shows zero on hand and zero inbound; reversible"],
        ["Add negative keywords", "CampaignCriterionService.MutateCampaignCriteria", "Only from search terms with clicks and zero conversions; staff review list"],
        ["Adjust target ROAS / target CPA", "CampaignService.MutateCampaigns (bidding strategy fields)", "Step changes of at most 10%; minimum 7 days between changes"],
        ["Pause under-performing keywords", "AdGroupCriterionService.MutateAdGroupCriteria", "Threshold-based; never removes, only pauses"],
        ["Draft responsive search ad copy", "AdGroupAdService.MutateAdGroupAds (create as PAUSED)", "Created paused; staff enable manually after review in Google Ads"],
    ], [1.45 * inch, 3.2 * inch, W - 2 * M - 4.65 * inch]))
    s.append(P("Roll-out is staged. Phase 1 (first 60 days) is read-only reporting. Phase 2 adds recommendations with mandatory approval. "
               "Only after Phase 2 has run cleanly will we consider allowing narrowly scoped, low-risk rules (for example, negative keywords below a fixed click threshold) to apply automatically, "
               "still with full audit logging and a daily summary to the team."))

    # ---- 4 audience
    s.append(P("4. Intended users", "h1"))
    s.append(table([
        ["Role", "Who", "What they do in the tool"],
        ["Head of Digital Transformation", "1 person (API contact)", "Owns the tool, sets rules and caps, approves high-impact changes"],
        ["E-commerce / marketing", "2-3 staff", "Review dashboards, approve or dismiss recommendations, request ad-copy drafts"],
        ["Procurement / catalogue", "1-2 staff", "Read-only view of product-level ad performance next to stock and margin"],
        ["Engineering", "1-2 staff", "Maintain the sync job and executor; no day-to-day campaign actions"],
    ], [1.9 * inch, 1.4 * inch, W - 2 * M - 3.3 * inch]))
    s.append(P("All users are Gen'C Beauty employees signing in with company Google Workspace accounts. There is no public sign-up, no customer-facing surface, "
               "and no mechanism for anyone outside the company to connect their own Google Ads account. Because the tool is internal and we are applying for Basic Access, "
               "the Required Minimum Functionality (RMF) for third-party tools does not apply; we nevertheless follow its spirit by exposing Google's own recommendations and change history to users."))

    # ---- 5 architecture
    s.append(P("5. Architecture and data flow", "h1"))
    s.append(architecture())
    s.append(Spacer(1, 8))
    s.append(table([
        ["Component", "Technology", "Notes"],
        ["Reporting sync job", "Node.js on Google Cloud Run Jobs, Cloud Scheduler", "Read-only GAQL queries; incremental by date segment"],
        ["Recommendation engine", "Node.js service; rules engine + LLM for ranking and copy", "Reads only from our database; produces proposals, never calls Google Ads"],
        ["Change executor", "Node.js on Cloud Run (private ingress)", "Only path that calls mutate services; one operation per approved proposal"],
        ["Operations database", "Postgres (Supabase), private network", "Existing database holding Amazon, Walmart and inventory data; adds campaign metrics, proposals, approvals, audit log"],
        ["Operations dashboard", "Next.js, Google Workspace SSO", "Existing internal dashboard; reads the database, never calls the Google Ads API directly"],
        ["Secrets", "Google Cloud Secret Manager", "OAuth client credentials and refresh token"],
    ], [1.5 * inch, 2.4 * inch, W - 2 * M - 3.9 * inch]))

    # ---- 6 API usage
    s.append(P("6. API usage details", "h1"))
    s.append(table([
        ["Item", "Detail"],
        ["Accounts accessed", "Only Gen'C Beauty's own Google Ads account for gencbeauty.com, accessed through our Google Cloud project"],
        ["Authentication", "OAuth 2.0 with a single refresh token belonging to a company user with Standard access on the account; login-customer-id set to our manager account where one is used"],
        ["Client library", "Official Google Ads API client library, kept on the current API version; migrated before each version sunset"],
        ["Estimated volume", "Reporting: about 8 queries x 4 runs/day, roughly 30-70 operations/day. Mutates: fewer than 50/day in Phase 2. Far below the Basic Access limit"],
        ["Rate limiting / errors", "Exponential backoff with jitter on RESOURCE_EXHAUSTED and transient errors; partial_failure on batched mutates; all GoogleAdsFailure details logged"],
        ["Environment", "Developed and tested against a Google Ads test account first, then the production account"],
        ["Not in scope", "Customer Match / user-list uploads, offline conversion uploads, account creation, billing setup, and any access to accounts we do not own"],
    ], [1.5 * inch, W - 2 * M - 1.5 * inch]))

    # ---- 7 data handling
    s.append(P("7. Data handling, security and privacy", "h1"))
    s.append(bullets([
        "<b>Data collected from the API</b> is aggregate campaign, ad group, keyword, search-term and product performance data. We do not retrieve or store end-user personal data from Google Ads.",
        "<b>Storage.</b> Data is stored in our private Postgres database, encrypted at rest, accessible only from our own services and by staff through the SSO-protected dashboard.",
        "<b>Retention.</b> Daily metrics are retained for 24 months for year-over-year comparison; search-term data for 13 months; audit logs for 5 years.",
        "<b>Credentials.</b> The OAuth client secret and refresh token live in Secret Manager and are never committed to source control or shown in the UI.",
        "<b>Access control.</b> Dashboard roles (viewer / approver / admin) are enforced server-side. Only approvers can approve changes; only admins can edit rules and caps.",
        "<b>Audit.</b> Every API mutate is logged with approver identity, timestamp, resource name, before and after values, and the request ID returned by the API.",
        "<b>Sharing.</b> Google Ads data is not shared with, sold to, or made available to any third party. The LLM receives only aggregate metrics and existing ad text, never credentials or personal data.",
    ]))

    # ---- 8 policy compliance
    s.append(P("8. Compliance with Google Ads API policies", "h1"))
    s.append(bullets([
        "We have read and will comply with the Google Ads API Terms and Conditions and the Google Ads API policies, including the Required Minimum Functionality where applicable.",
        "The tool is an internal tool for our own account. It is not offered to external users, so no demo access is required, and no third-party advertiser data flows through it.",
        "This application is made through our Google Cloud project under Google's Cloud-managed access process, including brand verification of the project.",
        "We will keep the API contact email monitored, keep the project's contact and brand information current, and respond promptly to any compliance request from Google.",
        "We will not use the API to scrape, cache for resale, or redistribute Google Ads data, nor to circumvent Google Ads UI policies or account limits.",
        "Automation is deliberately conservative: human approval, hard caps on change magnitude, reversibility, and full logging, so that API use is predictable and low-volume.",
        "We will apply for Standard Access only if our operation volume approaches the Basic limit, and will provide updated documentation at that time.",
    ]))

    # ---- 9 mockups
    s.append(PageBreak())
    s.append(P("9. Interface mockups", "h1"))
    s.append(P("The following mockups show the two primary screens. Both are pages of the existing internal dashboard, behind company single sign-on. Figures are illustrative."))
    s.append(wireframe_dashboard())
    s.append(Spacer(1, 14))
    s.append(wireframe_approvals())

    # ---- 10 timeline & contact
    s.append(PageBreak())
    s.append(P("10. Implementation timeline", "h1"))
    s.append(table([
        ["Phase", "Scope", "API services used", "Target"],
        ["0 - Test", "Build against a test account; validate GAQL queries and mutate flows", "GoogleAdsService, mutate services (test account only)", "Weeks 1-3"],
        ["1 - Reporting", "Production read-only sync; Google Ads tab live in the operations dashboard", "GoogleAdsService.SearchStream", "Weeks 4-8"],
        ["2 - Assisted automation", "Recommendation engine and approval queue; approved changes executed via API", "Campaign, CampaignBudget, CampaignCriterion, AdGroup, AdGroupCriterion, AdGroupAd services", "Weeks 9-16"],
        ["3 - Review", "Evaluate results, tune rules; consider narrow auto-apply for lowest-risk rules", "As above", "Month 5+"],
    ], [1.3 * inch, 2.3 * inch, 2.1 * inch, W - 2 * M - 5.7 * inch]))

    s.append(P("11. Contact", "h1"))
    s.append(P("Manne Tsang, Head of Digital Transformation, Gen'C Beauty<br/>"
               "manne@superhairpieces.com (API contact email, monitored daily)<br/>"
               "gencbeauty.com"))
    s.append(Spacer(1, 10))
    s.append(P("We are happy to provide further detail, a walkthrough of the tool, or additional screenshots on request.", "small"))

    # keep every section heading with at least ~1.4in of its content
    story = []
    for i, f in enumerate(s):
        if isinstance(f, Paragraph) and f.style.name == "h1" and not (i and isinstance(s[i - 1], PageBreak)):
            story.append(CondPageBreak(1.4 * inch))
        story.append(f)
    doc.build(story)
    print("wrote", OUT)


if __name__ == "__main__":
    build()
