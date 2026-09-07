"""Inject dashboard_data.json into template.html and write site/index.html."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
data_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "dashboard_data.json")
data = json.load(open(data_path, encoding="utf-8"))
html = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
# the template is written for the artifact host: wrap it in a full document
page = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<style>body{margin:0;font-family:system-ui,sans-serif;background:#f3f5f7}[hidden]{display:none!important}</style></head><body>"
        + html.replace("__DATA__", json.dumps(data, separators=(",", ":"))) + "</body></html>")
os.makedirs(os.path.join(HERE, "site"), exist_ok=True)
open(os.path.join(HERE, "site", "index.html"), "w", encoding="utf-8").write(page)
print("site/index.html", len(page), "bytes; data generated", data.get("generated"))
