"""All configuration, through Dynaconf: a settings file, environment variables over it, flags over both.

The settings file is `settings.toml` (plus `.secrets.toml` for tokens) found in the working directory
or any parent of it, or the file given with --config. Every key is also an environment variable with
the prefix COMPAT_REVIEW_ (COMPAT_REVIEW_MODEL, COMPAT_REVIEW_POLL_REPOS, ...), which overrides the
file. The conventional names GITHUB_TOKEN, GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY,
GITHUB_APP_PRIVATE_KEY_FILE, GITHUB_WEBHOOK_SECRET, OPENAI_BASE_URL, OPENAI_API_KEY,
ANTHROPIC_API_KEY and PORT are honored when the prefixed form is absent. `settings.example.toml`
at the repository root lists every key.
"""

import json
import os
from pathlib import Path

from dynaconf import Dynaconf, ValidationError, Validator

PREFIX = "COMPAT_REVIEW"
DEFAULT_FILES = ["settings.toml", ".secrets.toml"]
APIS = ["responses", "chat", "anthropic"]
EFFORTS = ["low", "medium", "high"]


def _env(name):
    """A default that falls back to an unprefixed environment variable."""
    return lambda settings, validator: os.environ.get(name) or None


def validators():
    return [
        Validator("MODEL", default=""),
        Validator("API", default="responses", is_in=APIS),
        Validator("EFFORT", default="medium", is_in=EFFORTS),
        Validator("BASE_URL", default=_env("OPENAI_BASE_URL")),
        Validator("API_KEY", default=_env("OPENAI_API_KEY")),
        Validator("ANTHROPIC_API_KEY", default=_env("ANTHROPIC_API_KEY")),
        Validator("HEADERS", default={}),
        Validator("TIMEOUT", default=300, cast=float, gt=0),
        Validator("ATTEMPTS", default=4, cast=int, gte=1),
        Validator("DATA", default="data"),
        Validator("PORT", default=lambda s, v: int(os.environ.get("PORT") or 8000), cast=int),
        Validator("POLL_REPOS", default=[]),
        Validator("POLL_INTERVAL", default=60, cast=int, gte=1),
        Validator("POLL_MAX_AGE_DAYS", default=7, cast=float, gte=0),
        Validator("SKIP_DRAFTS", default=True, cast=bool),
        Validator("MAX_MIRRORS_GB", default=50, cast=float, gt=0),
        Validator("FEEDBACK_INTERVAL", default=1800, cast=int, gte=60),
        Validator("DASHBOARD_TOKEN", default=None),
        Validator("PRICE_IN_PER_M", default=None),
        Validator("PRICE_OUT_PER_M", default=None),
        Validator("GITHUB_APP_ID", default=_env("GITHUB_APP_ID")),
        Validator("GITHUB_APP_PRIVATE_KEY", default=_env("GITHUB_APP_PRIVATE_KEY")),
        Validator("GITHUB_APP_PRIVATE_KEY_FILE", default=_env("GITHUB_APP_PRIVATE_KEY_FILE")),
        Validator("GITHUB_WEBHOOK_SECRET", default=_env("GITHUB_WEBHOOK_SECRET")),
        Validator("GITHUB_TOKEN", default=_env("GITHUB_TOKEN")),
    ]


_current = None


def load(settings_file=None):
    """Load the configuration and make it current. `settings_file` replaces the default file search.

    Raises ValueError for a value that fails validation, FileNotFoundError for a named file that is missing.
    """
    global _current
    if settings_file:
        if not Path(settings_file).is_file():
            raise FileNotFoundError(f"settings file not found: {settings_file}")
        files = [str(Path(settings_file).resolve())]
    else:
        files = DEFAULT_FILES
    cfg = Dynaconf(envvar_prefix=PREFIX, settings_files=files, environments=False, load_dotenv=False,
                   validators=validators())
    try:
        cfg.validators.validate()
    except ValidationError as e:
        raise ValueError(str(e)) from None
    _current = cfg
    return cfg


def current():
    """The configuration loaded last, loading the defaults on first use."""
    return _current if _current is not None else load()


def headers(cfg):
    """Extra request headers for the model gateway: a table in the settings file, or a JSON object in the
    environment variable."""
    h = cfg.HEADERS
    if isinstance(h, str):
        h = json.loads(h) if h.strip() else {}
    return {str(k): str(v) for k, v in dict(h or {}).items()}


def price(cfg, name):
    v = cfg.get(name)
    return None if v in (None, "") else float(v)


def repos(cfg):
    """`poll_repos` as a list of OWNER/REPO strings, from a list in the file or a comma-separated variable."""
    v = cfg.POLL_REPOS
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x for x in str(v).replace(",", " ").split() if x]


def private_key(cfg):
    key = cfg.GITHUB_APP_PRIVATE_KEY
    if not key and cfg.GITHUB_APP_PRIVATE_KEY_FILE:
        key = Path(cfg.GITHUB_APP_PRIVATE_KEY_FILE).read_text()
    return key or None
