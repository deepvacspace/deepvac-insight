"""Agent requests: nothing an agent asks for runs until a person approves it,
and an approved request is executed at most once."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import agent_requests_service as svc

pytestmark = pytest.mark.integration

CHAMBER = {"id": 3, "name": "Testa chamber"}


def _request():
    return svc.create_request(
        "setpoint", CHAMBER, {"temperature_c": 30}, "Set to 30 °C", "soak test"
    )


def test_new_requests_wait_for_a_person(deepvac_data_dir):
    request = _request()
    assert request["status"] == svc.PENDING
    assert svc.approved_requests() == []
    assert svc.pending_count() == 1


def test_only_a_named_user_can_decide_and_only_once(deepvac_data_dir):
    request = _request()
    with pytest.raises(svc.AgentRequestError):
        svc.decide(request["id"], True, "")
    decided = svc.decide(request["id"], False, "Ana", "not today")
    assert (decided["status"], decided["decided_by"], decided["decision_comment"]) == (
        svc.REJECTED,
        "Ana",
        "not today",
    )
    with pytest.raises(svc.AgentRequestError):
        svc.decide(request["id"], True, "Ana")


def test_approved_requests_are_claimed_once(deepvac_data_dir):
    request = _request()
    svc.decide(request["id"], True, "Ana")
    assert [r["id"] for r in svc.approved_requests()] == [request["id"]]
    assert svc.claim(request["id"]) is True
    assert svc.claim(request["id"]) is False  # a second executor gets nothing
    svc.mark_done(request["id"], True, "setpoint job sent")
    done = svc.get_request(request["id"])
    assert (done["status"], done["result"]) == (svc.EXECUTED, "setpoint job sent")


def test_undecided_requests_expire(deepvac_data_dir):
    request = _request()
    svc.expire_stale(now=datetime.now(timezone.utc) + svc.PENDING_TIMEOUT + timedelta(seconds=1))
    assert svc.get_request(request["id"])["status"] == svc.EXPIRED
    with pytest.raises(svc.AgentRequestError):
        svc.decide(request["id"], True, "Ana")
