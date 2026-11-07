"""Staff-only contact display, using synthetic identities and the isolated DB."""
from copy import deepcopy

import pytest
from sqlalchemy import event, select

from app.agent_attention import serialize_attention
from app.database import SessionLocal, engine
from app.models import AgentAttentionRequest, AgentConversation, Branch

PN = "51900000001"
LID = "7499818262692@lid"


def notice(tenant, sender=LID, **changes):
    with SessionLocal.begin() as db:
        row = AgentAttentionRequest(business_id=tenant["business_id"], branch_id=tenant["branch_id"],
            sender=sender, kind="human_requested", summary="Atención sintética", source_key=f"notice:{sender}",
            details={"message_ids": ["synthetic-message"]}, **changes)
        db.add(row)
        db.flush()
        return row.id


def conversation(tenant, sender=LID, transport=None, **changes):
    with SessionLocal.begin() as db:
        row = AgentConversation(business_id=changes.pop("business_id", tenant["business_id"]),
            branch_id=changes.pop("branch_id", tenant["branch_id"]), channel=changes.pop("channel", "whatsapp"),
            sender=sender, state={"_transport": transport if transport is not None else {},
                "contact_display_name": "Untrusted model name", "history": ["Never expose this"]}, **changes)
        db.add(row)
        db.flush()
        return row.id


def read(client, tenant, headers):
    response = client.get(f'/api/v1/agent-attention?branch_id={tenant["branch_id"]}', headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


@pytest.mark.parametrize(("sender", "transport", "name", "phone"), [
    (LID, {"contact_display_name": "María"}, "María", None),
    (PN, {}, None, PN),
    (LID, {"contact_display_name": "María", "verified_phone": PN}, "María", PN),
    (LID, {}, None, None),
    (LID, {"contact_display_name": "  María\u202e\x01   Rey  "}, "María Rey", None),
    (LID, {"contact_display_name": 123, "verified_phone": LID}, None, None),
    (LID, {"contact_display_name": "x" * 121, "verified_phone": "9" * 20}, None, None),
    (LID, {"verified_phone": 51900000001}, None, None),
])
def test_contact_combinations_and_provider_sanitization(client, tenant, auth_headers, sender, transport, name, phone):
    notice(tenant, sender)
    conversation(tenant, sender, transport)
    row, = read(client, tenant, auth_headers)
    assert row["contact_display_name"] == name
    assert row["contact_phone"] == phone
    assert row["sender"] == sender
    assert "_transport" not in row and "history" not in row


def test_provider_sanitization_removes_nul_before_postgres_storage():
    # PostgreSQL text cannot represent NUL. The real ingress removes it before
    # storing protected metadata; do not seed an impossible provider snapshot.
    from app.agent_runtime_api import contact_display_name
    assert contact_display_name("  María\u202e\x00   Rey  ") == "María Rey"


@pytest.mark.parametrize(("notice_sender", "stored_sender", "transport"), [
    (LID, PN, {"identity_aliases": {LID: True}, "verified_phone": PN}),
    (PN, LID, {"verified_phone": PN}),
    (PN + "@s.whatsapp.net", PN, {}),
])
def test_only_verified_identity_links_supply_profile(client, tenant, auth_headers, notice_sender, stored_sender, transport):
    notice(tenant, notice_sender)
    conversation(tenant, stored_sender, {**transport, "contact_display_name": "Perfil auténtico"})
    row, = read(client, tenant, auth_headers)
    assert row["contact_display_name"] == "Perfil auténtico"
    assert row["contact_phone"] == PN


@pytest.mark.parametrize("alias_value", [False, 1, "true", "not-a-boolean", None])
def test_unverified_alias_does_not_match(client, tenant, auth_headers, alias_value):
    notice(tenant)
    conversation(tenant, PN, {"identity_aliases": {LID: alias_value}, "contact_display_name": "Ajeno"})
    row, = read(client, tenant, auth_headers)
    assert row["contact_display_name"] is None and row["contact_phone"] is None


@pytest.mark.parametrize("sender", [LID, PN])
def test_ambiguous_identity_never_picks_latest_profile(client, tenant, auth_headers, sender):
    notice(tenant, sender)
    conversation(tenant, "700000000001@lid", {"identity_aliases": {sender: True}, "contact_display_name": "Uno", "verified_phone": PN})
    conversation(tenant, "700000000002@lid", {"identity_aliases": {sender: True}, "contact_display_name": "Dos", "verified_phone": PN})
    row, = read(client, tenant, auth_headers)
    assert row["contact_display_name"] is None
    assert row["contact_phone"] == (PN if sender == PN else None)


def test_scope_and_channel_are_mandatory(client, tenant, auth_headers):
    notice(tenant)
    with SessionLocal.begin() as db:
        branch = Branch(business_id=tenant["business_id"], slug="second", name="Second")
        db.add(branch)
        db.flush()
        other_same_business_branch = branch.id
    conversation(tenant, transport={"contact_display_name": "Other business"},
        business_id=tenant["other_business_id"], branch_id=tenant["other_branch_id"])
    conversation(tenant, transport={"contact_display_name": "Other branch"}, branch_id=other_same_business_branch)
    conversation(tenant, transport={"contact_display_name": "Other channel"}, channel="telegram")
    row, = read(client, tenant, auth_headers)
    assert row["contact_display_name"] is None and row["contact_phone"] is None
    response = client.get(f'/api/v1/agent-attention?branch_id={tenant["other_branch_id"]}', headers=auth_headers)
    assert response.status_code == 403


@pytest.mark.parametrize("transport", ["invalid", ["invalid"], {"identity_aliases": [LID]}])
def test_malformed_transport_never_breaks_list_or_uses_public_state(client, tenant, auth_headers, transport):
    notice(tenant)
    conversation(tenant, transport=transport)
    row, = read(client, tenant, auth_headers)
    assert row["contact_display_name"] is None and row["contact_phone"] is None


def test_conflicting_phone_cannot_supply_someone_elses_name(client, tenant, auth_headers):
    notice(tenant, PN)
    conversation(tenant, PN, {"verified_phone": "51900000002", "contact_display_name": "Contradiction"})
    row, = read(client, tenant, auth_headers)
    assert row["contact_display_name"] is None and row["contact_phone"] == PN


def test_historical_missing_profile_and_non_individual_jids(client, tenant, auth_headers):
    for sender in [LID, "123456789@g.us", "status@broadcast", "123'bad@lid"]:
        notice(tenant, sender)
    rows = read(client, tenant, auth_headers)
    assert len(rows) == 4
    assert all(row["contact_display_name"] is None and row["contact_phone"] is None for row in rows)


def test_read_is_grouped_and_does_not_modify_records_or_shared_serializer(client, tenant, auth_headers):
    for index in range(205):
        sender = f"{7000000000000 + index}@lid"
        notice(tenant, sender)
        conversation(tenant, sender, {"contact_display_name": f"Perfil {index}"})
    with SessionLocal() as db:
        first = db.scalar(select(AgentAttentionRequest))
        original = deepcopy(serialize_attention(first))
        before = [(row.id, row.version, row.updated_at, deepcopy(row.state)) for row in db.scalars(select(AgentConversation))]
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        rows = read(client, tenant, auth_headers)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(rows) == 205 and all(row["contact_display_name"] for row in rows)
    assert len([sql for sql in statements if "FROM agent_conversations" in sql]) == 3
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    with SessionLocal() as db:
        after = [(row.id, row.version, row.updated_at, row.state) for row in db.scalars(select(AgentConversation))]
        assert after == before
        serialized = serialize_attention(db.get(AgentAttentionRequest, original["id"]))
        assert serialized == original
    returned = next(row for row in rows if row["id"] == original["id"])
    assert set(returned) == set(original) | {"contact_display_name", "contact_phone"}


def test_staff_authorization_unchanged(client, tenant, auth_headers):
    notice(tenant)
    conversation(tenant, transport={"contact_display_name": "Private"})
    path = f'/api/v1/agent-attention?branch_id={tenant["branch_id"]}'
    assert client.get(path).status_code == 401
    assert client.get(path, headers={**auth_headers, "X-Dev-Role": "waiter"}).status_code == 403
    assert client.get(path, headers={**auth_headers, "X-Dev-Role": "dispatcher"}).status_code == 200


def test_empty_list_does_not_query_conversations(client, tenant, auth_headers):
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert read(client, tenant, auth_headers) == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any("FROM agent_conversations" in sql for sql in statements)
