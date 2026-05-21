from __future__ import annotations

"""Active checks that template contracts still match their JSON schemas."""

import json
from pathlib import Path

import jsonschema


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = REPO_ROOT / "contracts"
SCHEMAS = CONTRACTS / "schemas"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_bootstrap_inventory_template_matches_schema():
    jsonschema.validate(
        instance=_load_json(CONTRACTS / "bootstrap_hosts.template.json"),
        schema=_load_json(SCHEMAS / "bootstrap-inventory.schema.json"),
    )


def test_doctor_result_template_matches_schema():
    jsonschema.validate(
        instance=_load_json(CONTRACTS / "doctor_result.template.json"),
        schema=_load_json(SCHEMAS / "doctor-result.schema.json"),
    )


def test_mcp_result_templates_match_schema():
    schema = _load_json(SCHEMAS / "mcp-result.schema.json")
    for path in sorted(CONTRACTS.glob("mcp_result_*.template.json")):
        jsonschema.validate(instance=_load_json(path), schema=schema)

