"""Inject dashboard_data.json into template.html and write site/index.html.

    python google-ads/dashboard/build_site.py [dashboard_data.json] [--artifact out.html]

The template is written for the claude.ai artifact host (no <html>/<head>
wrapper of its own), so the Cloud Run page wraps it in a full document.
--artifact writes the unwrapped page with the data injected, for publishing
the same dashboard as an artifact.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
args = [a for a in sys.argv[1:] if not a.startswith("--")]
data_path = args[0] if args else os.path.join(HERE, "dashboard_data.json")
artifact = sys.argv[sys.argv.index("--artifact") + 1] if "--artifact" in sys.argv else None

data = json.load(open(data_path, encoding="utf-8"))


def pack(report):
    """Store rows as [cols, [values...]] so repeated keys are not sent once per row (about 60% smaller)."""
    rows = report.get("rows") or []
    if not rows or not isinstance(rows[0], dict):
        return
    cols = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    report["cols"] = cols
    report["rows"] = [[r.get(c) for c in cols] for r in rows]


for account in data.get("accounts", []):
    for report in account.get("reports", {}).values():
        pack(report)
data["packed"] = True
html = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
# </script> inside a JSON string would end the script block early; escape it.
payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
page_body = html.replace("__DATA__", payload)
page = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">"
        "<style>body{margin:0;font-family:system-ui,sans-serif;background:#f3f5f7}[hidden]{display:none!important}</style></head><body>"
        + page_body + "</body></html>")
os.makedirs(os.path.join(HERE, "site"), exist_ok=True)
with open(os.path.join(HERE, "site", "index.html"), "w", encoding="utf-8") as f:
    f.write(page)
print("site/index.html", len(page) // 1024, "KB; data generated", data.get("generated"), "for", len(data.get("accounts", [])), "account(s)")
if artifact:
    with open(artifact, "w", encoding="utf-8") as f:
        f.write(page_body)
    print("artifact page", artifact, len(page_body) // 1024, "KB")
