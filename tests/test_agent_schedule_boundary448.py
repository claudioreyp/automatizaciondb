"""Active POS schedule enforcement for agent orders; TestClient and isolated SQLite only."""
from datetime import datetime, time, timezone

import pytest
from sqlalchemy import select, delete
from conftest import clean_database, client, tenant, auth_headers
from test_restaurant_agent_boundary import agent_headers, leased, SENDER
from test_escalar_integrations import create_credential, integration_headers
from app.database import SessionLocal
from app.models import (BranchSettings, ServiceSchedule, ScheduleShift, Order, KitchenTicket,
                        Payment, OrderPaymentRequest, PaymentEvidence)


@pytest.fixture
def clock(monkeypatch):
    from app import settings_service
    value = {"now": datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)}  # Monday 13:00 Lima
    monkeypatch.setattr(settings_service, "utcnow", lambda: value["now"])
    return value


def principal(tenant, *, settings=False, active=True, archived=False, other=False):
    business_id = tenant["other_business_id"] if other else tenant["business_id"]
    branch_id = tenant["other_branch_id"] if other else tenant["branch_id"]
    with SessionLocal.begin() as db:
        originals = list(db.scalars(select(ServiceSchedule.id).where(
            ServiceSchedule.branch_id == tenant["branch_id"], ServiceSchedule.kind == "primary")))
        db.execute(delete(ScheduleShift).where(ScheduleShift.schedule_id.in_(originals)))
        db.execute(delete(ServiceSchedule).where(ServiceSchedule.id.in_(originals)))
        if settings:
            db.add(BranchSettings(business_id=business_id, branch_id=branch_id, digital_takeaway=True,
                payment_methods={"takeaway": ["cash", "yape"]}))
        schedule = ServiceSchedule(business_id=business_id, branch_id=branch_id, name="Principal",
            kind="primary", active=active,
            archived_at=datetime(2026, 10, 18, tzinfo=timezone.utc) if archived else None)
        db.add(schedule)
        db.flush()
        db.add(ScheduleShift(schedule_id=schedule.id, day_of_week=0, starts_at=time(12), ends_at=time(14)))


def body(tenant, method="cash"):
    return {"branch_id": tenant["branch_id"], "channel": "takeaway", "payment_method": method,
            "whatsapp_chat_id": SENDER, "customer_phone": SENDER, "quoted_total": 20,
            "items": [{"product_id": tenant["product_id"], "quantity": 1}]}


def counts():
    with SessionLocal() as db:
        return {model.__tablename__: db.query(model).count() for model in [Order, KitchenTicket, Payment]}


@pytest.mark.parametrize("settings", [False, True])
@pytest.mark.parametrize("method", ["cash", "yape"])
def test_active_closed_primary_blocks_new_agent_order(client, tenant, agent_headers, clock, settings, method):
    principal(tenant, settings=settings)
    clock["now"] = datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc)  # Exact closing.
    assert client.get("/api/v1/integrations/context", headers=agent_headers).json()["open_now"] is False
    assert client.get("/api/v1/integrations/context/catalog", headers=agent_headers).json()["open_now"] is False
    _, headers = leased(client, agent_headers)
    result = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "closed"}, json=body(tenant, method))
    assert result.status_code == 409 and result.json()["code"] == "SERVICE_CLOSED", result.text
    assert counts() == {"orders": 0, "kitchen_tickets": 0, "payments": 0}


@pytest.mark.parametrize("variant", ["open", "inactive", "archived", "other_branch", "absent"])
def test_only_active_current_branch_schedule_limits_new_agent_order(client, tenant, agent_headers, clock, variant):
    principal(tenant, active=variant not in {"inactive", "absent"}, archived=variant == "archived", other=variant == "other_branch")
    if variant == "absent":
        with SessionLocal.begin() as db:
            db.execute(delete(ScheduleShift));db.execute(delete(ServiceSchedule))
    if variant != "open":
        clock["now"] = datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc)
    expected_open = True if variant == "open" else None
    assert client.get("/api/v1/integrations/context", headers=agent_headers).json()["open_now"] is expected_open
    assert client.get("/api/v1/integrations/context/catalog", headers=agent_headers).json()["open_now"] is expected_open
    _, headers = leased(client, agent_headers)
    result = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "allowed"}, json=body(tenant))
    if variant != "open":
        assert result.status_code == 409 and result.json()["code"] == "SERVICE_UNVERIFIED", result.text
        assert counts() == {"orders": 0, "kitchen_tickets": 0, "payments": 0}
        return
    assert result.status_code == 201, result.text
    cash = client.post(f"/api/v1/integrations/orders/{result.json()['id']}/cash-confirm", headers={**headers, "Idempotency-Key": "allowed-cash"})
    assert cash.status_code == 200, cash.text
    assert counts() == {"orders": 1, "kitchen_tickets": 1, "payments": 0}


@pytest.mark.parametrize("settings", [False, True])
def test_preview_does_not_authorize_new_order_after_closing(client, tenant, agent_headers, clock, settings):
    principal(tenant, settings=settings)
    _, headers = leased(client, agent_headers)
    preview = client.post("/api/v1/integrations/orders/preview", headers=headers,
        json={"channel": "takeaway", "payment_method": "cash", "items": body(tenant)["items"]})
    assert preview.status_code == 200 and preview.json()["known_total"] == 20
    clock["now"] = datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc)
    result = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "after-preview"}, json=body(tenant))
    assert result.status_code == 409 and result.json()["code"] == "SERVICE_CLOSED", result.text
    assert counts() == {"orders": 0, "kitchen_tickets": 0, "payments": 0}


@pytest.mark.parametrize("method", ["cash", "yape"])
def test_exact_existing_order_replay_after_closing_remains_valid(client, tenant, agent_headers, clock, method):
    principal(tenant)
    _, headers = leased(client, agent_headers)
    request_headers = {**headers, "Idempotency-Key": "exact-draft"}
    first = client.post("/api/v1/integrations/orders/draft", headers=request_headers, json=body(tenant, method))
    assert first.status_code == 201, first.text
    before = counts()
    clock["now"] = datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc)
    replay = client.post("/api/v1/integrations/orders/draft", headers=request_headers, json=body(tenant, method))
    assert replay.status_code == 201 and replay.json() == first.json()
    assert counts() == before
    if method == "cash":
        result = client.post(f"/api/v1/integrations/orders/{first.json()['id']}/cash-confirm", headers={**headers, "Idempotency-Key": "existing-cash"})
        assert result.status_code == 200, result.text
        assert counts() == {"orders": 1, "kitchen_tickets": 1, "payments": 0}


@pytest.mark.parametrize("source", ["staff", "generic_integration"])
def test_agent_guard_does_not_change_existing_other_profile_behavior(client, tenant, agent_headers, auth_headers, clock, source):
    principal(tenant)
    clock["now"] = datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc)
    if source == "generic_integration":
        credential = create_credential(client, tenant, auth_headers)
        response = client.post("/api/v1/integrations/orders/draft", headers={**integration_headers(credential["token"]), "Idempotency-Key": "legacy"}, json=body(tenant))
    else:
        response = client.post("/api/v1/orders", headers={**auth_headers, "Idempotency-Key": "staff"}, json=body(tenant))
    assert response.status_code == 201, response.text
    assert counts()["orders"] == 1


def test_preclosing_cash_addition_releases_after_closing_when_base_is_approved(client, tenant, agent_headers, auth_headers, clock):
    principal(tenant, settings=True)
    _, headers = leased(client, agent_headers)
    result = client.post("/api/v1/integrations/orders/draft", headers={**headers, "Idempotency-Key": "base-yape"}, json=body(tenant, "yape"))
    assert result.status_code == 201, result.text
    order = result.json()
    receipt = client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence", headers={**headers, "Idempotency-Key": "receipt"},
        data={"provider": "yape", "looks_like_payment_receipt": "true", "sender": SENDER, "whatsapp_message_id": "synthetic-448-receipt"},
        files={"file": ("synthetic.png", b"\x89PNG\r\n\x1a\nSYNTHETIC-448", "image/png")})
    assert receipt.status_code == 201, receipt.text
    latest = receipt.json()["order"]
    addition = client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches", headers={**headers, "Idempotency-Key": "addition"},
        json={"sender": SENDER, "expected_version": latest["version"], "payment_method": "cash", "expected_amount": 20, "items": body(tenant)["items"]})
    assert addition.status_code == 200, addition.text
    request_id = addition.json()["payment_request"]["id"]
    assert addition.json()["payment_request"]["status"] == "awaiting_base_review"
    assert counts() == {"orders": 1, "kitchen_tickets": 0, "payments": 0}
    clock["now"] = datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc)
    opened = client.post("/api/v1/cash/sessions/open", headers=auth_headers,
        json={"register_id": tenant["register_id"], "opening_amount": 0})
    assert opened.status_code in {200, 201}
    latest = client.get(f"/api/v1/orders/{order['id']}/detail", headers=auth_headers).json()["order"]
    review = client.post(f"/api/v1/payment-evidence/{receipt.json()['evidence']['id']}/review", headers={**auth_headers, "Idempotency-Key": "approve-base"},
        json={"approve": True, "register_id": tenant["register_id"], "expected_version": latest["version"]})
    assert review.status_code == 200, review.text
    with SessionLocal() as db:
        request = db.get(OrderPaymentRequest, request_id)
        assert request.status == "committed" and request.snapshot["blocked_reason"] is None
        saved = db.get(Order, order["id"])
        assert float(saved.total) == 40 and len(saved.items) == 2
        assert db.get(PaymentEvidence, receipt.json()["evidence"]["id"]).status == "paid"
    assert counts() == {"orders": 1, "kitchen_tickets": 2, "payments": 1}


@pytest.mark.parametrize('post_request_state',['closed','inactive','missing','timezone'])
def test_preclosing_yape_addition_can_receive_receipt_and_staff_approval_after_closing(
    client,tenant,agent_headers,auth_headers,clock,post_request_state,monkeypatch,
):
    from test_agent_service_verified450 import replace_hours
    from test_pos_printing import enable,jobs
    principal(tenant,settings=True)
    enable(tenant)
    _,headers=leased(client,agent_headers)
    created=client.post('/api/v1/integrations/orders/draft',headers={**headers,'Idempotency-Key':'base'},json=body(tenant))
    assert created.status_code==201,created.text
    order=created.json()
    confirmed=client.post(f"/api/v1/integrations/orders/{order['id']}/cash-confirm",headers={**headers,'Idempotency-Key':'cash'})
    assert confirmed.status_code==200,confirmed.text
    requested=client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches",headers={**headers,'Idempotency-Key':'preclosing-addition'},
        json={'sender':SENDER,'expected_version':confirmed.json()['order']['version'],'payment_method':'yape','expected_amount':20,'items':body(tenant)['items']})
    assert requested.status_code==200,requested.text
    request_id=requested.json()['payment_request']['id']
    assert counts()=={'orders':1,'kitchen_tickets':1,'payments':0}
    clock['now']=datetime(2026,10,19,19,0,tzinfo=timezone.utc)
    if post_request_state!='closed':replace_hours(tenant,post_request_state)
    uploaded=client.post(f"/api/v1/integrations/orders/{order['id']}/payment-evidence",headers={**headers,'Idempotency-Key':'afterclose-receipt'},
        data={'provider':'yape','looks_like_payment_receipt':'true','sender':SENDER,'whatsapp_message_id':'synthetic-postclosing-addition','payment_request_id':request_id},
        files={'file':('synthetic.png',b'\x89PNG\r\n\x1a\nSYNTHETIC-APPROVAL','image/png')})
    assert uploaded.status_code==201,uploaded.text
    assert counts()=={'orders':1,'kitchen_tickets':1,'payments':0}
    opened=client.post('/api/v1/cash/sessions/open',headers=auth_headers,json={'register_id':tenant['register_id'],'opening_amount':0})
    assert opened.status_code in {200,201}
    evidence_id=uploaded.json()['evidence']['id']
    review_path=f'/api/v1/payment-evidence/{evidence_id}/review'
    latest=client.get(f"/api/v1/orders/{order['id']}/detail",headers=auth_headers).json()['order']
    review_body={'approve':True,'register_id':tenant['register_id'],'expected_version':latest['version']}
    review_headers={**auth_headers,'Idempotency-Key':'approve-postclosing-addition'}
    from app.config import get_settings
    with monkeypatch.context() as auth_patch:
        auth_patch.setattr(get_settings(),'supabase_url','https://auth.example.invalid')
        auth_patch.setattr(get_settings(),'supabase_jwt_secret','isolated-local-verifier-secret-not-for-deployment')
        denied=client.post(review_path,headers={**headers,'Idempotency-Key':'consumer-cannot-approve'},json=review_body)
        assert denied.status_code==401,denied.text
    reviewed=client.post(review_path,headers=review_headers,json=review_body)
    assert reviewed.status_code==200,reviewed.text
    assert client.post(review_path,headers=review_headers,json=review_body).json()==reviewed.json()
    assert counts()=={'orders':1,'kitchen_tickets':2,'payments':1}
    print_rows=jobs(client,order,auth_headers)['items']
    assert len(print_rows)==3 and sum(j['job_type']=='customer_receipt' for j in print_rows)==1
    assert sum(j['job_type']=='kitchen_ticket' for j in print_rows)==2
    with SessionLocal() as db:
        saved=db.get(Order,order['id']);request=db.get(OrderPaymentRequest,request_id)
        assert saved.folio==order['folio'] and float(saved.total)==40 and len(saved.items)==2
        assert request.status=='paid' and request.payment_id
    # The exception is internal to approval of the stored request, not an
    # input field available to a consumer or a fresh postclosing request.
    latest=client.get(f"/api/v1/integrations/orders/{order['id']}/customer-state",headers=headers,params={'sender':SENDER}).json()
    fresh=client.post(f"/api/v1/integrations/orders/{order['id']}/item-batches",headers={**headers,'Idempotency-Key':'new-afterclose'},
        json={'sender':SENDER,'expected_version':latest['version'],'payment_method':'yape','expected_amount':20,'items':body(tenant)['items']})
    assert fresh.status_code==409 and fresh.json()['code'] in {'SERVICE_CLOSED','SERVICE_UNVERIFIED'},fresh.text
    assert counts()=={'orders':1,'kitchen_tickets':2,'payments':1}
