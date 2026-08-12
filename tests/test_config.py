import json

import pytest

from syncroprintd import config as cfgmod


def sample_dict():
    return {
        "account": {"host": "syncromsp.com", "subdomain": "exampleshop", "api_token": "tok123"},
        "transport": {"mode": "auto", "poll_interval_s": 60},
        "printers": {
            "a4": {"cups_name": "HP_LaserJet", "options": ["fit-to-page"]},
            "label": {"cups_name": "Brother_QL", "options": ["media=Custom.62x100mm", "fit-to-page"]},
        },
        "routing": {
            "ticket": {"enabled": True, "auto_print": True, "printer": "a4", "quantity": 2,
                       "duplex": "off", "rotate": False},
            "asset_label": {"enabled": True, "auto_print": True, "printer": "label", "quantity": 1,
                            "duplex": "off", "rotate": True},
        },
    }


def test_round_trip(tmp_path):
    cfg = cfgmod.from_dict(sample_dict())
    path = tmp_path / "config.json"
    cfgmod.save(cfg, str(path))
    loaded = cfgmod.load(str(path))
    assert loaded.to_dict() == cfg.to_dict()
    assert loaded.account.subdomain == "exampleshop"
    assert loaded.printers["label"].cups_name == "Brother_QL"
    assert loaded.routing["ticket"].quantity == 2


def test_defaults_applied():
    cfg = cfgmod.from_dict(sample_dict())
    assert cfg.timeouts.download_s == 120
    assert cfg.retention.failed_spool_days == 7
    assert cfg.transport.mode == "auto"
    assert cfg.location_id is None


def test_base_url_and_allowlist():
    cfg = cfgmod.from_dict(sample_dict())
    assert cfg.account.base_url == "https://exampleshop.syncromsp.com"
    assert "exampleshop.syncromsp.com" in cfg.account.pdf_allowed_hosts


def test_lp_options_duplex_and_rotate():
    cfg = cfgmod.from_dict(sample_dict())
    assert cfg.routing["asset_label"].lp_options() == [
        "sides=one-sided", "orientation-requested=4"]
    route = cfgmod.Route(duplex="long-edge", printer="a4")
    assert "sides=two-sided-long-edge" in route.lp_options()


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d["account"].pop("subdomain"), "subdomain"),
    (lambda d: d["account"].update(api_token=""), "api_token"),
    (lambda d: d["account"].update(host="evil.example.com"), "host"),
    (lambda d: d["transport"].update(mode="carrier-pigeon"), "mode"),
    (lambda d: d["routing"]["ticket"].update(printer="nope"), "not a configured printer"),
    (lambda d: d["routing"]["ticket"].update(quantity=0), "quantity"),
    (lambda d: d["routing"]["ticket"].update(duplex="diagonal"), "duplex"),
    (lambda d: d["routing"]["ticket"].update(bogus_key=1), "unknown key"),
])
def test_validation_errors(mutate, fragment):
    data = sample_dict()
    mutate(data)
    with pytest.raises(cfgmod.ConfigError, match=fragment):
        cfgmod.from_dict(data)


def test_load_missing_and_malformed(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="not found"):
        cfgmod.load(str(tmp_path / "absent.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(cfgmod.ConfigError, match="not valid JSON"):
        cfgmod.load(str(bad))


def test_redacted_dict_masks_token():
    cfg = cfgmod.from_dict(sample_dict())
    red = cfg.redacted_dict()
    assert red["account"]["api_token"] == "********"
    assert cfg.to_dict()["account"]["api_token"] == "tok123"


def test_apply_update_partial_merge_keeps_masked_token():
    cfg = cfgmod.from_dict(sample_dict())
    updated = cfgmod.apply_update(cfg, {
        "account": {"api_token": "********", "subdomain": "exampleshop"},
        "transport": {"poll_interval_s": 120},
    })
    assert updated.account.api_token == "tok123"
    assert updated.transport.poll_interval_s == 120
    assert updated.transport.mode == "auto"


def test_apply_update_rejects_unknown_section():
    cfg = cfgmod.from_dict(sample_dict())
    with pytest.raises(cfgmod.ConfigError, match="unknown config section"):
        cfgmod.apply_update(cfg, {"nonsense": {}})


def test_apply_update_validates_result():
    cfg = cfgmod.from_dict(sample_dict())
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.apply_update(cfg, {"transport": {"mode": "smoke-signals"}})


def test_apply_update_replaces_printers_and_routing_wholesale():
    cfg = cfgmod.from_dict(sample_dict())
    updated = cfgmod.apply_update(cfg, {
        "routing": {"ticket": {"enabled": True, "printer": "a4"}},
    })
    assert set(updated.routing) == {"ticket"}          # asset_label removed
    assert set(updated.printers) == {"a4", "label"}    # untouched section kept
