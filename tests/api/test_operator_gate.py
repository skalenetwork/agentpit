"""Operator gate: X-Admin-Token required on market lifecycle actions,
create_agent, and create_personality. Mirrors tests/api/test_admin.py's
token handling.
"""

import pytest
from fastapi.testclient import TestClient

from agentpit.api.main import app

# AGENTPIT_ADMIN_TOKEN is read at app startup by Settings; tests rely on
# the default ("dev-admin-token") so we don't need to mutate env here.
ADMIN_TOKEN = "dev-admin-token"

OPERATOR_ENDPOINTS = [
    pytest.param(
        "/markets",
        {
            "question": "Gate test?",
            "description": "x",
            "outcome_labels": ["YES", "NO"],
        },
        id="create_market",
    ),
    pytest.param("/markets/1/activate", None, id="activate_market"),
    pytest.param("/markets/1/close", None, id="close_market"),
    pytest.param("/markets/1/cancel", None, id="cancel_market"),
    pytest.param(
        "/markets/1/resolve", {"winning_outcome_index": 0}, id="resolve_market"
    ),
    pytest.param(
        "/create_agent",
        {"agent_id": "gate_agent", "personality_id": "gate_personality"},
        id="create_agent",
    ),
    pytest.param(
        "/create_personality",
        {
            "personality_id": "gate_p",
            "title": "Gate",
            "beliefs": "x",
            "methods": "x",
            "needs": "x",
        },
        id="create_personality",
    ),
]


@pytest.mark.parametrize("path,payload", OPERATOR_ENDPOINTS)
def test_operator_endpoint_rejects_missing_token(path, payload):
    with TestClient(app) as client:
        resp = client.post(path, json=payload)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "admin token missing or invalid"


@pytest.mark.parametrize("path,payload", OPERATOR_ENDPOINTS)
def test_operator_endpoint_rejects_wrong_token(path, payload):
    with TestClient(app) as client:
        resp = client.post(
            path, json=payload, headers={"X-Admin-Token": "wrong-token"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "admin token missing or invalid"


def test_create_personality_and_agent_succeed_with_admin_token():
    with TestClient(app) as client:
        headers = {"X-Admin-Token": ADMIN_TOKEN}
        personality_payload = {
            "personality_id": "gate_ok_personality",
            "title": "Gate OK",
            "beliefs": "Markets are efficient",
            "methods": "Follow the trend",
            "needs": "Price feeds",
        }
        resp = client.post(
            "/create_personality", json=personality_payload, headers=headers
        )
        assert resp.status_code == 200, resp.text

        agent_payload = {
            "agent_id": "gate_ok_agent",
            "personality_id": "gate_ok_personality",
        }
        resp = client.post("/create_agent", json=agent_payload, headers=headers)
        assert resp.status_code == 200, resp.text


def test_create_market_refuses_a_supplied_condition_id():
    with TestClient(app) as client:
        resp = client.post(
            "/markets",
            json={
                "question": "Gate condition?",
                "description": "x",
                "outcome_labels": ["YES", "NO"],
                "condition_id": {"value": "0x" + "ab" * 32},
            },
            headers={"X-Admin-Token": ADMIN_TOKEN},
        )
        assert resp.status_code == 400, resp.text
