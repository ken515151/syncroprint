"""Minimal REST client for the Syncro/RepairShopr AutoPrintr endpoints.

Only two endpoints exist in the original protocol (PROTOCOL.md §2, §12):

  POST https://admin.{host}/api/v1/sign_in            (email/password login)
  GET  https://{sub}.{host}/api/v1/settings/printing  (channel + registers)

We authenticate with the account subdomain + the API token generated from
the AutoPrinter App Center card (per current Syncro docs), which slots into
the same `api_key` parameter the original used its session token for. The
legacy email/password login is kept for RepairShopr accounts that need it.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .config import Account

log = logging.getLogger("syncroprintd.api")

_TIMEOUT = (10, 30)
_USER_AGENT = "SyncroPrint-Linux/0.1"


class ApiError(RuntimeError):
    pass


class AuthError(ApiError):
    """Bad token / bad credentials — do not retry without user action."""


def _check(resp: requests.Response, what: str) -> dict[str, Any]:
    if resp.status_code in (401, 403):
        raise AuthError(f"{what}: authentication rejected (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        raise ApiError(f"{what}: HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ApiError(f"{what}: response was not JSON") from exc
    if isinstance(data, dict) and data.get("error"):
        raise ApiError(f"{what}: {data['error']}")
    return data


def fetch_printing_settings(account: Account) -> dict[str, Any]:
    """GET /api/v1/settings/printing — returns the Pusher channel name and
    the account's POS registers.

    Response: {"messaging_channel": "...", "registers": [{id, name,
    location_id, location_name}]}.
    """
    url = f"{account.base_url}/api/v1/settings/printing"
    resp = requests.get(url, params={"api_key": account.api_token},
                        headers={"User-Agent": _USER_AGENT},
                        timeout=_TIMEOUT, verify=True)
    data = _check(resp, "settings/printing")
    if not data.get("messaging_channel"):
        raise ApiError("settings/printing returned no messaging_channel — "
                       "check the token was created from the AutoPrinter App Center card")
    return data


def login(host: str, email: str, password: str) -> dict[str, Any]:
    """Legacy email/password login (POST /api/v1/sign_in, form-encoded).

    Response includes user_token, subdomain, user_id, default_location,
    locations_allowed[] — see PROTOCOL.md §2.
    """
    url = f"https://admin.{host}/api/v1/sign_in"
    resp = requests.post(url, data={"email": email, "password": password},
                         headers={"User-Agent": _USER_AGENT},
                         timeout=_TIMEOUT, verify=True)
    return _check(resp, "sign_in")


def test_connection(account: Account) -> tuple[bool, str]:
    """Used by the applet's 'Test connection' button."""
    try:
        data = fetch_printing_settings(account)
        n = len(data.get("registers") or [])
        return True, f"OK — channel acquired, {n} register(s) on account"
    except AuthError as exc:
        return False, str(exc)
    except (ApiError, requests.RequestException) as exc:
        return False, str(exc)
