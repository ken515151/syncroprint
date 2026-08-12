"""Configuration owner for syncroprintd.

The daemon is the single owner of the config file; the applet reads and
writes it only through the control socket (`get_config` / `set_config`).

File location: /etc/syncroprint/config.json, root:syncroprint 0640 —
the API token lives here, so permissions matter more than convenience.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any

DEFAULT_CONFIG_PATH = "/etc/syncroprint/config.json"

KNOWN_HOSTS = ("syncromsp.com", "repairshopr.com")
TRANSPORT_MODES = ("auto", "pusher", "poll")
DUPLEX_VALUES = ("off", "long-edge", "short-edge")

_DUPLEX_TO_LP = {
    "off": "sides=one-sided",
    "long-edge": "sides=two-sided-long-edge",
    "short-edge": "sides=two-sided-short-edge",
}


class ConfigError(ValueError):
    """Raised when the config file is missing required data or malformed."""


@dataclass
class Account:
    host: str = "syncromsp.com"
    subdomain: str = ""
    api_token: str = ""

    @property
    def is_configured(self) -> bool:
        """False on a fresh install — account details are entered via the
        applet's Settings window, so an empty account is a valid state."""
        return bool(self.subdomain and self.api_token)

    @property
    def base_url(self) -> str:
        return f"https://{self.subdomain}.{self.host}"

    @property
    def pdf_allowed_hosts(self) -> set[str]:
        """Hosts we will download PDFs from. Payload URLs pointing anywhere
        else are refused (§5.6): the channel is untrusted input.

        Both product domains are allowed regardless of the account's host:
        Syncro and RepairShopr share a backend, and live testing showed job
        PDFs served from pdf.repairshopr.com even for Syncro accounts
        (PROTOCOL.md §7). Matching includes subdomains of these.
        """
        return {f"{self.subdomain}.{self.host}", *KNOWN_HOSTS}


@dataclass
class Transport:
    mode: str = "auto"
    poll_interval_s: int = 60


@dataclass
class Printer:
    cups_name: str
    options: list[str] = field(default_factory=list)


@dataclass
class Route:
    enabled: bool = True
    auto_print: bool = False
    printer: str = "a4"
    quantity: int = 1
    duplex: str = "off"
    rotate: bool = False

    def lp_options(self) -> list[str]:
        opts = [_DUPLEX_TO_LP[self.duplex]]
        if self.rotate:
            opts.append("orientation-requested=4")  # 90 deg clockwise
        return opts


@dataclass
class Timeouts:
    download_s: int = 120
    print_submit_s: int = 30
    stuck_flag_s: int = 60


@dataclass
class Retention:
    failed_spool_days: int = 7


@dataclass
class Config:
    account: Account = field(default_factory=Account)
    transport: Transport = field(default_factory=Transport)
    printers: dict[str, Printer] = field(default_factory=dict)
    routing: dict[str, Route] = field(default_factory=dict)
    timeouts: Timeouts = field(default_factory=Timeouts)
    retention: Retention = field(default_factory=Retention)
    register_printer: str | None = None
    location_id: int | None = None
    pusher_app_key: str | None = None  # overridable in case the key rotates

    def route_for(self, document_type: str) -> Route | None:
        return self.routing.get(document_type)

    def printer_for(self, route: Route) -> Printer | None:
        return self.printers.get(route.printer)

    def validate(self) -> None:
        if self.account.host not in KNOWN_HOSTS:
            raise ConfigError(f"account.host must be one of {KNOWN_HOSTS}, got {self.account.host!r}")
        # A fully empty account is the valid "not set up yet" state; a
        # half-filled one is a mistake worth flagging.
        if self.account.subdomain or self.account.api_token:
            if not self.account.subdomain:
                raise ConfigError("account.subdomain is required when an api_token is set")
            if not self.account.api_token:
                raise ConfigError("account.api_token is required when a subdomain is set")
        if self.transport.mode not in TRANSPORT_MODES:
            raise ConfigError(f"transport.mode must be one of {TRANSPORT_MODES}, got {self.transport.mode!r}")
        if self.transport.poll_interval_s < 10:
            raise ConfigError("transport.poll_interval_s must be >= 10")
        for name, printer in self.printers.items():
            if not printer.cups_name:
                raise ConfigError(f"printers.{name}.cups_name is required")
        for doc_type, route in self.routing.items():
            if route.printer not in self.printers:
                raise ConfigError(f"routing.{doc_type}.printer {route.printer!r} is not a configured printer")
            if route.quantity < 1 or route.quantity > 99:
                raise ConfigError(f"routing.{doc_type}.quantity must be 1..99")
            if route.duplex not in DUPLEX_VALUES:
                raise ConfigError(f"routing.{doc_type}.duplex must be one of {DUPLEX_VALUES}")
        if self.register_printer is not None and self.register_printer not in self.printers:
            raise ConfigError(f"register_printer {self.register_printer!r} is not a configured printer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": vars(self.account).copy(),
            "transport": vars(self.transport).copy(),
            "printers": {k: {"cups_name": p.cups_name, "options": list(p.options)} for k, p in self.printers.items()},
            "routing": {k: vars(r).copy() for k, r in self.routing.items()},
            "timeouts": vars(self.timeouts).copy(),
            "retention": vars(self.retention).copy(),
            "register_printer": self.register_printer,
            "location_id": self.location_id,
            "pusher_app_key": self.pusher_app_key,
        }

    def redacted_dict(self) -> dict[str, Any]:
        """to_dict() with the API token masked — what get_config returns."""
        d = self.to_dict()
        if d["account"].get("api_token"):
            d["account"]["api_token"] = "*" * 8
        return d


def _build(section: type, data: dict[str, Any], where: str):
    allowed = section.__dataclass_fields__
    unknown = set(data) - set(allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")
    try:
        return section(**data)
    except TypeError as exc:
        raise ConfigError(f"bad {where} section: {exc}") from exc


def from_dict(data: dict[str, Any]) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a JSON object")
    cfg = Config(
        account=_build(Account, data.get("account", {}), "account"),
        transport=_build(Transport, data.get("transport", {}), "transport"),
        printers={k: _build(Printer, v, f"printers.{k}") for k, v in data.get("printers", {}).items()},
        routing={k: _build(Route, v, f"routing.{k}") for k, v in data.get("routing", {}).items()},
        timeouts=_build(Timeouts, data.get("timeouts", {}), "timeouts"),
        retention=_build(Retention, data.get("retention", {}), "retention"),
        register_printer=data.get("register_printer"),
        location_id=data.get("location_id"),
        pusher_app_key=data.get("pusher_app_key"),
    )
    cfg.validate()
    return cfg


def load(path: str = DEFAULT_CONFIG_PATH) -> Config:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {exc}") from exc
    return from_dict(data)


def save(cfg: Config, path: str = DEFAULT_CONFIG_PATH) -> None:
    """Atomic write; preserves the strict permissions of the existing file."""
    cfg.validate()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, indent=2)
        fh.write("\n")
    if os.path.exists(path):
        st = os.stat(path)
        os.chmod(tmp, st.st_mode & 0o777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)  # not available / not needed on Windows tests
        except (AttributeError, PermissionError):
            pass
    else:
        os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def apply_update(cfg: Config, update: dict[str, Any]) -> Config:
    """Merge a partial `set_config` payload into an existing config.

    The applet sends only the sections it changed; a masked api_token
    (all asterisks) means "keep the current one".
    """
    merged = cfg.to_dict()
    for key, value in update.items():
        if key not in merged:
            raise ConfigError(f"unknown config section: {key}")
        if key in ("printers", "routing"):
            # keyed collections are replaced wholesale so entries can be removed
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            deep = copy.deepcopy(merged[key])
            deep.update(value)
            merged[key] = deep
        else:
            merged[key] = value
    token = merged.get("account", {}).get("api_token", "")
    if token and set(token) == {"*"}:
        merged["account"]["api_token"] = cfg.account.api_token
    return from_dict(merged)
