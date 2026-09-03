"""Credential resolution for scripts in this repo.

One store, one lookup path. Credentials live in GCP Secret Manager on the
project below; nothing sensitive belongs in this repo, in a GitHub Actions
variable, or in a Claude cloud environment's variables box.

Resolution order for every credential:

1. An explicit environment variable, when the caller names one. This is the
   local-development path and reads a gitignored .env, so you can run any
   script on your own machine without touching GCP.
2. GCP Secret Manager, using Application Default Credentials. In GitHub
   Actions that's the service account from the GCP_SA_KEY secret; on Cloud
   Run it's the service identity; locally it's `gcloud auth
   application-default login`.

Secret naming convention: `<service>-<credential>`, lowercase, hyphenated.
For example `bigcommerce-access-token`, `hubspot-private-app-token`.

The Secret Manager client is imported lazily, so a script that resolves
everything from the environment needs no third-party packages installed.
"""

import os

DEFAULT_PROJECT = "shp-ai-bot-2026"

_DOTENV_LOADED = False


class SecretNotFound(RuntimeError):
    pass


def load_dotenv(start_dir=None):
    """Read KEY=VALUE pairs from .env, walking up from start_dir to the repo root.

    Real environment variables always win, so `FOO=x python3 script.py` works.
    Runs at most once per process.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True

    directory = os.path.abspath(start_dir or os.path.dirname(os.path.abspath(__file__)))
    seen = set()
    while directory and directory not in seen:
        seen.add(directory)
        path = os.path.join(directory, ".env")
        if os.path.isfile(path):
            _read_dotenv(path)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent


def _read_dotenv(path):
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def get_secret(secret_id, env_var=None, project=None, required=True, version="latest"):
    """Return a credential by Secret Manager id, preferring a local env var.

    secret_id: the Secret Manager secret name, e.g. "bigcommerce-access-token".
    env_var:   an environment variable checked first, e.g. "BC_ACCESS_TOKEN".
    required:  raise SecretNotFound when nothing resolves, instead of None.
    """
    if env_var:
        load_dotenv()
        value = os.environ.get(env_var)
        if value:
            return value

    project = project or os.environ.get("GCP_PROJECT") or DEFAULT_PROJECT
    try:
        value = _fetch_from_secret_manager(secret_id, project, version)
    except Exception as exc:  # noqa: BLE001 - reported verbatim below
        if required:
            hint = f" or set {env_var} locally" if env_var else ""
            raise SecretNotFound(
                f"could not read secret '{secret_id}' from project '{project}': {exc}. "
                f"Add it with `gcloud secrets create`{hint}."
            ) from exc
        return None

    if value is None and required:
        raise SecretNotFound(f"secret '{secret_id}' resolved to nothing in project '{project}'")
    return value


def _fetch_from_secret_manager(secret_id, project, version):
    try:
        from google.cloud import secretmanager
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-secret-manager is not installed "
            "(pip install google-cloud-secret-manager)"
        ) from exc

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    # Secrets are stored without a trailing newline, but strip line endings
    # defensively: `echo` into `gcloud secrets versions add` is a common way to
    # add one, and on Windows cmd that appends "\r\n".
    return response.payload.data.decode("utf-8").rstrip("\r\n")
