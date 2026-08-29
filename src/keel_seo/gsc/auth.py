"""Shared authentication, transport and error handling for every Search Console API.

Search Console is not one API but a family of them, each with its own service name,
version, OAuth scope and required permission level:

======================  ==================  ===========================================  ==================
Capability              Service/version     Scope                                        Property permission
======================  ==================  ===========================================  ==================
Search Analytics        searchconsole v1    ``webmasters.readonly``                      Restricted
URL Inspection          searchconsole v1    ``webmasters.readonly``                      Full or Owner
Sitemaps (read)         searchconsole v1    ``webmasters.readonly``                      Restricted
Sitemaps (submit/del)   searchconsole v1    ``webmasters``                               Full or Owner
Sites (list/get)        searchconsole v1    ``webmasters.readonly``                      Restricted
Sites (add/delete)      searchconsole v1    ``webmasters``                               Owner
Indexing notifications  indexing v3         ``indexing``                                 Owner
======================  ==================  ===========================================  ==================

They all share ONE service-account key (``$GSC_CREDENTIALS``, default
``~/.config/keel-seo/gsc-service-account.json``); only the scope differs per call,
and a service-account key can mint credentials for any scope without re-consent.
Adding the service account as **Owner** of each property therefore unlocks the whole
table above with a single grant.

This module centralises what every one of those clients needs: locating the key,
building a scoped service object, caching it, retrying the transient failures Google
returns under load, and turning Google's opaque errors into an explanation that names
the actual fix. The Google client libraries are imported lazily (the ``[gsc]`` extra),
so importing this module is always safe on a host that never calls out.
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

DEFAULT_CREDENTIALS = Path.home() / ".config" / "keel-seo" / "gsc-service-account.json"

SCOPE_READONLY = "https://www.googleapis.com/auth/webmasters.readonly"
SCOPE_READWRITE = "https://www.googleapis.com/auth/webmasters"
SCOPE_INDEXING = "https://www.googleapis.com/auth/indexing"

SEARCH_CONSOLE = ("searchconsole", "v1")
INDEXING = ("indexing", "v3")

# GSC finalizes Search Analytics data about two days late; querying "today" returns
# partial or empty rows unless dataState="all" is requested explicitly.
DATA_LAG_DAYS = 2

RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_RETRIES = 5

# Transport-level failures are as retryable as a 503 and far more common from a
# network that sits behind an interfering middlebox: a dropped TLS connection to
# googleapis.com surfaces as ConnectionResetError / IncompleteRead / an ssl error,
# never as an HTTP status, so a status-only retry policy fails a whole sweep on one
# bad packet. Matched by class rather than by message so no locale or wording change
# can silently turn a retry back into a hard failure.
RETRY_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)

_service_cache: dict = {}


class GscError(RuntimeError):
    """Any Search Console API failure: missing/malformed key, missing scope,
    insufficient property permission, a disabled Cloud API, quota exhaustion, or a
    request the API refused. Carries a message that names the fix, not just the code."""


def credentials_path() -> Path:
    """Where the service-account key lives.

    Resolution order: ``$GSC_CREDENTIALS`` → Django ``KEEL_SEO["gsc_credentials"]``
    → the default under ``~/.config/keel-seo``. The Django lookup is soft so these
    modules keep working as plain CLIs with no settings module configured.
    """
    env = os.environ.get("GSC_CREDENTIALS")
    if env:
        return Path(env).expanduser()
    configured = _django_setting("gsc_credentials")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_CREDENTIALS


def resolve_site(site: str = "") -> str:
    """The property to act on: an explicit argument, else ``$GSC_SITE``, else the
    Django ``KEEL_SEO["gsc_site"]`` setting. Raises rather than guessing — acting on
    the wrong property is worse than failing."""
    resolved = site or os.environ.get("GSC_SITE", "") or _django_setting("gsc_site") or ""
    if not resolved:
        raise GscError(
            "no property set. Pass --site / site=, or export GSC_SITE, or set "
            'KEEL_SEO["gsc_site"] (e.g. "sc-domain:example.com" for a Domain property, '
            'or "https://example.com/" for a URL-prefix property).'
        )
    return resolved


def _django_setting(key: str):
    """Read one ``KEEL_SEO`` key when Django happens to be configured, else None.

    Every failure mode collapses to None on purpose: no Django installed, no settings
    module, settings not configured yet, or the key simply absent. A CLI invocation
    must never be blocked by the absence of a Django environment.
    """
    try:
        from ..config import seo_setting

        return seo_setting(key)
    except Exception:
        return None


def service_account_email() -> str:
    """The ``client_email`` inside the key file — the exact string to paste into
    Search Console's "Add user" box."""
    path = credentials_path()
    if not path.exists():
        raise GscError(_missing_key_message(path))
    try:
        return json.loads(path.read_text()).get("client_email", "")
    except Exception as exc:
        raise GscError(f"could not parse the service-account key at {path}: {exc}") from exc


def cloud_project_id() -> str:
    """The Cloud project the key belongs to — the project whose API library must have
    Search Console API and Indexing API enabled."""
    path = credentials_path()
    if not path.exists():
        raise GscError(_missing_key_message(path))
    try:
        return json.loads(path.read_text()).get("project_id", "")
    except Exception as exc:
        raise GscError(f"could not parse the service-account key at {path}: {exc}") from exc


def _missing_key_message(path: Path) -> str:
    return (
        f"service-account key not found at {path}\n"
        "Create one at https://console.cloud.google.com/iam-admin/serviceaccounts "
        "(Keys -> Add key -> JSON), save it there with chmod 600, or point "
        "$GSC_CREDENTIALS at wherever you keep it."
    )


def credentials(scopes):
    """Service-account credentials for the given scopes."""
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GscError(
            "google client libraries are missing; install the extra: pip install 'keel-seo[gsc]'"
        ) from exc
    path = credentials_path()
    if not path.exists():
        raise GscError(_missing_key_message(path))
    try:
        return service_account.Credentials.from_service_account_file(str(path), scopes=list(scopes))
    except Exception as exc:
        raise GscError(f"could not read the service-account key at {path}: {exc}") from exc


def service(api=SEARCH_CONSOLE, scopes=(SCOPE_READONLY,)):
    """A built, cached API client for ``api`` under ``scopes``.

    Caching is keyed by (service, version, scopes, key path): building a discovery
    client costs a network round trip on first use, and a batch of 500 URL
    inspections must not pay it 500 times. The key path is part of the cache key so a
    process that switches ``$GSC_CREDENTIALS`` between properties never reuses the
    wrong identity.
    """
    name, version = api
    key = (name, version, tuple(sorted(scopes)), str(credentials_path()))
    cached = _service_cache.get(key)
    if cached is not None:
        return cached
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GscError(
            "google client libraries are missing; install the extra: pip install 'keel-seo[gsc]'"
        ) from exc
    built = build(name, version, credentials=credentials(scopes), cache_discovery=False)
    _service_cache[key] = built
    return built


def reset_service_cache() -> None:
    """Drop cached clients — for tests, and for a process that rotates credentials."""
    _service_cache.clear()


def _status_code(exc: Exception):
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            return None
    return getattr(exc, "status_code", None)


def execute(request, *, retries: int = MAX_RETRIES, what: str = ""):
    """Execute one googleapiclient request with retry/backoff, raising :class:`GscError`.

    Google answers overload and per-minute quota bursts with 429/5xx, which are
    retryable, and permanent problems (missing scope, wrong permission level,
    disabled API, unknown property) with 4xx, which are not — retrying those just
    burns the daily quota. Backoff is exponential with jitter so a batch that trips
    the per-minute limit spreads out instead of resynchronising on the next attempt.
    """
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            return request.execute()
        except Exception as exc:
            code = _status_code(exc)
            retryable = code in RETRY_STATUSES or (
                code is None and isinstance(exc, RETRY_EXCEPTIONS)
            )
            if retryable and attempt < retries:
                time.sleep(delay + random.uniform(0, 0.4))
                delay = min(delay * 2, 32.0)
                continue
            raise GscError(explain(exc, what=what)) from exc


def explain(exc: Exception, *, what: str = "") -> str:
    """Translate a Google API error into the action that actually fixes it."""
    text = str(exc)
    code = _status_code(exc)
    prefix = f"{what}: " if what else ""

    if "SERVICE_DISABLED" in text or "has not been used in project" in text:
        project = ""
        try:
            project = cloud_project_id()
        except GscError:
            pass
        suffix = f"?project={project}" if project else ""
        return (
            f"{prefix}the required Google API is not enabled on the Cloud project. Enable both:\n"
            f"  https://console.cloud.google.com/apis/library/searchconsole.googleapis.com{suffix}\n"
            f"  https://console.cloud.google.com/apis/library/indexing.googleapis.com{suffix}\n"
            f"then retry (propagation takes a few minutes). [{text}]"
        )
    if code == 401 or "invalid_grant" in text or "UNAUTHENTICATED" in text:
        return (
            f"{prefix}401 the credentials were rejected. The key may be revoked, deleted, or "
            f"belong to a deleted service account — issue a fresh JSON key. [{text}]"
        )
    if code == 403 or "PERMISSION_DENIED" in text:
        email = ""
        try:
            email = service_account_email()
        except GscError:
            pass
        return (
            f"{prefix}403 permission denied. Check, in order:\n"
            "  1. The service account is a user of the property in Search Console -> Settings\n"
            f"     -> Users and permissions -> Add user: {email or '(key unreadable)'}\n"
            "  2. Its permission level is high enough: Restricted reads Search Analytics, but\n"
            "     URL Inspection needs Full or Owner, and the Indexing API needs Owner.\n"
            "  3. The Search Console API / Indexing API are enabled on the Cloud project.\n"
            f"  [{text}]"
        )
    if code == 404:
        return (
            f"{prefix}404 not found — the property, sitemap or resource does not exist under "
            f"this account. Run `python -m keel_seo.gsc sites list` to see the exact property "
            f"strings this key can act on (Domain properties look like sc-domain:example.com). [{text}]"
        )
    if code == 429 or "RESOURCE_EXHAUSTED" in text or "quotaExceeded" in text:
        return (
            f"{prefix}429 quota exhausted. Search Console limits are per property per day "
            "(URL Inspection: 2,000/day, 600/minute) and the Indexing API is per Cloud project "
            f"(200 publishes/day by default). Wait for the daily reset or request more quota. [{text}]"
        )
    if code == 400:
        return f"{prefix}400 the API refused the request as malformed. [{text}]"
    if code is None and isinstance(exc, RETRY_EXCEPTIONS):
        return (
            f"{prefix}the connection to Google failed and did not recover across retries "
            f"({type(exc).__name__}: {text}). This is a network problem, not an API one — "
            "re-run, or run it from a server whose route to googleapis.com is clean."
        )
    return f"{prefix}{text}" if prefix else text
