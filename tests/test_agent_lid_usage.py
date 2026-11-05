"""Authenticated WhatsApp identities share durable budgets without invented phones.

Only the isolated ASGI database is used; these tests never call a model/provider.
"""
import hashlib
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import event, func, select

from app.agent_usage import _identity_hash, _sender_hash, _sender_hashes
from app.database import SessionLocal, engine
from app.models import AgentConversation, AgentModelUsage, Branch, KitchenTicket, Order, Payment, utcnow
from test_agent_model_usage import acquire, configure, credential, reserve, settle
from test_restaurant_agent_boundary import agent_headers, commit

BASE = "/api/v1/integrations/conversations"
LID = "123456789012345@lid"
OTHER_LID = "987654321012345@lid"
PHONE = "51999900001"


def claim_identity(client, headers, sender, message_id, phone=None):
    response = client.post(BASE + "/claim", headers={**headers, "Idempotency-Key": message_id},
        json={"sender": sender, "message_id": message_id, "message": {
            "text": "hola", **({"verified_phone": phone} if phone else {})}})
    assert response.status_code == 200, response.text
    return response.json()


def branch_headers(tenant, slug):
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug=slug, name=slug)
        db.add(branch)
        db.flush()
        branch_id = branch.id
    return credential(tenant, branch_id, slug)


def test_hash_preserves_historical_phone_and_namespaces_lid_by_business():
    historical = hashlib.sha256(f"2:{PHONE}".encode()).hexdigest()
    assert _identity_hash(2, PHONE) == historical
    assert _identity_hash(2, "+" + PHONE + "@s.whatsapp.net") == historical
    assert _identity_hash(2, LID) != historical
    assert _identity_hash(2, LID) != _identity_hash(3, LID)
    row = SimpleNamespace(business_id=2, sender=LID,
        state={"_transport": {"contact_display_name": "Mi telefono " + PHONE}})
    assert _sender_hash(row) == _identity_hash(2, LID)
    row.state["_transport"]["verified_phone"] = PHONE
    assert _sender_hash(row) == historical


def test_authenticated_lid_can_reserve_without_guessing_phone_from_text(client, agent_headers, tenant):
    lease = acquire(client, agent_headers, sender=LID, text="mi telefono " + PHONE)
    result = reserve(client, agent_headers, lease).json()
    assert result["status"] == "reserved" and result["dispatch_allowed"] is True
    assert result["reason"] is None
    assert "sender_hash" not in result
    with SessionLocal() as db:
        row = db.scalar(select(AgentModelUsage))
        assert row.sender_hash == _identity_hash(tenant["business_id"], LID)
        assert row.sender_hash != _identity_hash(tenant["business_id"], PHONE)
        conversation = db.get(AgentConversation, lease["conversation_id"])
        assert not conversation.state["_transport"].get("verified_phone")
        assert all(db.scalar(select(func.count()).select_from(model)) == 0
            for model in (Order, Payment, KitchenTicket))


def test_distinct_lids_do_not_merge_from_same_phone_in_consumer_text(client, agent_headers, monkeypatch):
    configure(monkeypatch, conversation_tokens=1000, sender_tokens=10, business_tokens=1000)
    first = acquire(client, agent_headers, sender=LID, text="mi telefono " + PHONE)
    second = acquire(client, agent_headers, sender=OTHER_LID, message_id="other-lid", text="mi telefono " + PHONE)
    assert reserve(client, agent_headers, first, tokens=8).json()["dispatch_allowed"]
    assert reserve(client, agent_headers, second, tokens=8).json()["dispatch_allowed"]
    with SessionLocal() as db:
        assert len(set(db.scalars(select(AgentModelUsage.sender_hash)))) == 2


def test_same_lid_shares_budget_across_branches_but_not_businesses(client, agent_headers, tenant, monkeypatch):
    configure(monkeypatch, conversation_tokens=1000, sender_tokens=10, business_tokens=1000)
    first = acquire(client, agent_headers, sender=LID)
    original = reserve(client, agent_headers, first, tokens=8).json()
    assert original["dispatch_allowed"]
    second_headers = branch_headers(tenant, "lid-second")
    second = acquire(client, second_headers, sender=LID, message_id="same-lid-second")
    denied = reserve(client, second_headers, second, tokens=3).json()
    assert denied["status"] == "denied" and denied["reason"] == "budget_exceeded"
    other_headers = credential({"business_id": tenant["other_business_id"]},
        tenant["other_branch_id"], "other-lid-business")
    other = acquire(client, other_headers, sender=LID, message_id="same-lid-other-business")
    allowed = reserve(client, other_headers, other, tokens=8).json()
    assert allowed["dispatch_allowed"]
    with SessionLocal() as db:
        assert db.get(AgentModelUsage, original["reservation_id"]).sender_hash != \
            db.get(AgentModelUsage, allowed["reservation_id"]).sender_hash


def test_later_verified_phone_keeps_unknown_charge_and_cannot_open_fresh_sender_budget(
        client, agent_headers, tenant, monkeypatch):
    configure(monkeypatch, conversation_tokens=1000, sender_tokens=10, business_tokens=1000)
    original = claim_identity(client, agent_headers, LID, "lid-only")
    reservation = reserve(client, agent_headers, original, tokens=6).json()
    assert reservation["dispatch_allowed"]
    uncertain = settle(client, agent_headers, original, reservation["reservation_id"],
        usage_known=False, input_tokens=None, output_tokens=None)
    assert uncertain.status_code == 200 and uncertain.json()["status"] == "uncertain"
    assert commit(client, agent_headers, original).status_code == 200

    linked = claim_identity(client, agent_headers, LID, "lid-now-phone", PHONE)
    assert linked["conversation_id"] == original["conversation_id"]
    denied = reserve(client, agent_headers, linked, key="linked-reserve", tokens=5).json()
    assert denied["status"] == "denied" and denied["reason"] == "budget_exceeded"
    assert commit(client, agent_headers, linked, key="linked-complete").status_code == 200

    other_headers = branch_headers(tenant, "linked-phone-branch")
    phone_lease = claim_identity(client, other_headers, PHONE, "phone-in-other-branch")
    phone_denied = reserve(client, other_headers, phone_lease, tokens=5).json()
    assert phone_denied["status"] == "denied" and phone_denied["reason"] == "budget_exceeded"
    with SessionLocal() as db:
        old = db.get(AgentModelUsage, reservation["reservation_id"])
        assert old.status == "uncertain" and old.charged_tokens == old.reserved_tokens == 6
        assert old.sender_hash == _identity_hash(tenant["business_id"], LID)
        assert db.get(AgentModelUsage, phone_denied["reservation_id"]).sender_hash == \
            hashlib.sha256(f'{tenant["business_id"]}:{PHONE}'.encode()).hexdigest()
        assert db.scalar(select(func.sum(AgentModelUsage.charged_tokens))) == 6


def test_lid_without_phone_uses_authenticated_peer_mapping_and_all_phone_aliases(
        client, agent_headers, tenant, monkeypatch):
    configure(monkeypatch, conversation_tokens=1000, sender_tokens=10, business_tokens=1000)
    first = claim_identity(client, agent_headers, LID, "old-lid")
    original = reserve(client, agent_headers, first, tokens=6).json()
    assert original["dispatch_allowed"]
    assert commit(client, agent_headers, first).status_code == 200
    linked = claim_identity(client, agent_headers, LID, "phone-link", PHONE)
    assert commit(client, agent_headers, linked, key="link-complete").status_code == 200

    second_headers = branch_headers(tenant, "other-lid-phone")
    second = claim_identity(client, second_headers, OTHER_LID, "other-lid-link", PHONE)
    assert reserve(client, second_headers, second, tokens=4).json()["dispatch_allowed"]
    assert commit(client, second_headers, second, key="other-complete").status_code == 200

    third_headers = branch_headers(tenant, "unresolved-lid")
    third = claim_identity(client, third_headers, LID, "lid-without-phone-again")
    assert third["verified_phone"] is None
    denied = reserve(client, third_headers, third, tokens=1).json()
    assert denied["status"] == "denied" and denied["reason"] == "budget_exceeded"
    with SessionLocal() as db:
        conversation = db.get(AgentConversation, third["conversation_id"])
        selects = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            hashes = _sender_hashes(db, conversation)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert hashes == {_identity_hash(tenant["business_id"], value) for value in (LID, OTHER_LID, PHONE)}
        assert len(selects) == 2
        assert db.scalar(select(func.sum(AgentModelUsage.charged_tokens))) == 10


def test_lid_replay_and_utc_rollover_preserve_prior_reservation(client, agent_headers, monkeypatch):
    configure(monkeypatch, conversation_tokens=10, sender_tokens=10, business_tokens=1000)
    lease = acquire(client, agent_headers, sender=LID)
    before_midnight = utcnow().replace(hour=23, minute=59, second=59, microsecond=0)
    monkeypatch.setattr("app.agent_usage.utcnow", lambda: before_midnight)
    first = reserve(client, agent_headers, lease, tokens=10).json()
    assert first["dispatch_allowed"]
    monkeypatch.setattr("app.agent_usage.utcnow", lambda: before_midnight + timedelta(seconds=2))
    replay = reserve(client, agent_headers, lease, tokens=10).json()
    assert replay["reservation_id"] == first["reservation_id"]
    assert replay["dispatch_allowed"] is False and replay["reason"] == "reservation_replay"
    second = reserve(client, agent_headers, lease, key="new-day", call_id="composer:0", tokens=10).json()
    assert second["dispatch_allowed"]
    with SessionLocal() as db:
        old = db.get(AgentModelUsage, first["reservation_id"])
        new = db.get(AgentModelUsage, second["reservation_id"])
        assert old.charged_tokens == new.charged_tokens == 10
        assert old.window_start != new.window_start
        assert old.sender_hash == new.sender_hash
