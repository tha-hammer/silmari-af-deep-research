"""B0 scaffolding smoke test — proves tests/ui/ collects and the config is valid."""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / "ui" / "config" / "tenancy.json"


def test_tenancy_config_loads_and_has_required_keys():
    data = json.loads(CONFIG_PATH.read_text())
    assert data["default_org_slug"] == "silmari-default"
    assert data["default_run_visibility"] == "private"
    assert data["legacy_import_owner_email"]
    assert isinstance(data["bootstrap_owner_emails"], list) and data["bootstrap_owner_emails"]
