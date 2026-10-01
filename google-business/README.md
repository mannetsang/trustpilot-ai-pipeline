# Google Business Profile

Scripts that read and manage the salon listings through the **Business Profile
APIs** (`mybusinessaccountmanagement`, `mybusinessbusinessinformation`,
and the `mybusiness` v4 endpoints for reviews, posts and media).

Verified 2026-10-01: the token sees **1 account, 17 locations**.

## Authentication

Unlike the Merchant Center scripts, this does **not** use Application Default
Credentials. The Business Profile APIs act as a Google *user* who manages the
listings, so auth is a user OAuth refresh token. Three values travel
together, because a refresh token can only be exchanged by the OAuth client
that issued it:

| Secret Manager id (`shp-ai-bot-2026`) | Env var (local `.env`) |
|---|---|
| `google-business-profile-client-id` | `GOOGLE_BUSINESS_PROFILE_CLIENT_ID` |
| `google-business-profile-client-secret` | `GOOGLE_BUSINESS_PROFILE_CLIENT_SECRET` |
| `google-business-profile-refresh-token` | `GOOGLE_BUSINESS_PROFILE_REFRESH_TOKEN` |

Facts worth knowing before touching this:

- The OAuth client belongs to a developer-owned Cloud project, **not** to
  `shp-ai-bot-2026`. Business Profile API
  quota is granted per project and ours has none, so keep using this client.
- The refresh token is bound to the Google user who manages the listings
  (a personal account, not an organisation). If that user revokes the app or changes their password, every
  script here fails with `invalid_grant` until a new token is minted.
- Access tokens last one hour. `gbp_api.py` mints one per run and never
  stores it, so the `GOOGLE_BUSINESS_PROFILE_ACCESS_TOKEN` line in `.env` is
  informational only and can be deleted.
- The scope is `https://www.googleapis.com/auth/business.manage` (read and
  write). Anything that edits a listing is live immediately on Google Maps.

To mint a new refresh token: in the OAuth 2.0 Playground, gear icon, tick
*Use your own OAuth credentials*, enter the client id and secret, authorise
the `business.manage` scope as the listings' manager, and exchange the code. Save the
`refresh_token` to Secret Manager and the local `.env`.

## Usage

```bash
python google-business/locations.py          # every location, with ids
python google-business/locations.py --json   # raw API objects
```

In code:

```python
from gbp_api import GbpClient
client = GbpClient()
for loc in client.list_locations():
    print(loc["name"], loc["title"])
```

Only the standard library is used, so no venv is needed locally.

## Locations

Account `accounts/111445610944292236883` (the manager's personal account).
Location ids are not secrets. Nicknames follow the salon names in the
repo's CLAUDE.md.

| Nickname | Listing title | Address | Location id |
|---|---|---|---|
| Dufferin | Superhairpieces | 3220 Dufferin St unit 24, North York ON | `locations/11580068753340767001` |
| Rapistan | Superhairpieces | 7295 Rapistan Ct, Mississauga ON | `locations/7223301939465497732` |
| STC | Superhairpieces | 60 Brian Harrison Way unit 206, Toronto ON | `locations/10905129003754891303` |
| Eglinton | Superhairpieces | 1282 Eglinton Ave E, Mississauga ON | `locations/685188567892497037` |
| Ridgeway | Superhairpieces | Unit 29, 3075 Ridgeway Dr, Mississauga ON | `locations/13689417356433350520` |
| Consumer | True Hair Replacement & Cosmetic Center Inc | 201 Consumers Road #301, North York ON | `locations/9719921915312996452` |
| Brampton | Superhairpieces | 490 Bramalea Rd Unit 101, Brampton ON | `locations/9984412715850155494` |
| Pembrokes | Gen'C Hair Center | 1751 N University Dr, Pembroke Pines FL | `locations/16676111479862273432` |
| New York | Superhairpieces | 122 W 20th St, New York NY | `locations/11640499775623985658` |
| New York | Gen'C Hair Center | 122 W 20th St #1, New York NY | `locations/8200285945777290212` |
| Deerfield Beach | Gen'C Hair Center | 3356 W Hillsboro Blvd, Deerfield Beach FL | `locations/5108674717103047486` |
| Sunrise FL | Superhairpieces.com | 963 Shotgun Rd, Sunrise FL | `locations/8588506284493313246` |
| Madrid | Superhairpieces - Centro de prótesis capilares y pelucas | Paseo de las Delicias 36, Madrid | `locations/10496215448163535265` |
| Diemen NL | Superhairpieces | Sniep 85, Diemen | `locations/9083563788300132682` |
| Eglinton (Gen'C) | Gen'C Beauty | 1282 Eglinton Ave E, Mississauga ON | `locations/2286575413498793421` |
| Rapistan (Gen'C) | Gen'C Beauty | 7295 Rapistan Ct, Mississauga ON | `locations/16570189707651721536` |
| Dufferin (Gen'C) | Gen'C Beauty | 3220 Dufferin St unit 24, North York ON | `locations/6692803018046926984` (closed permanently) |

Dufferin, Rapistan and Eglinton each carry two listings at the same address:
a Superhairpieces one (hair replacement service) and a Gen'C Beauty one
(beauty product supplier). Treat them as separate listings with separate
reviews.
