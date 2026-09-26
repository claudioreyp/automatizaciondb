from app import api
from app.database import SessionLocal
from app.models import KitchenTicket, Order, Payment, PaymentEvidence
from test_agent_checkout import PHONE, setup


def test_initial_receipt_rejects_cash_other_chat_and_unclassified_images_without_mutation(
    client, tenant, auth_headers, monkeypatch,
):
    headers, order = setup(client, tenant, auth_headers, method="cash")
    path = f"/api/v1/integrations/orders/{order['id']}/payment-evidence"

    async def unexpected_storage(*_args, **_kwargs):
        raise AssertionError("Rejected receipts must not be stored")

    monkeypatch.setattr(api, "store_private_file", unexpected_storage)

    def upload(key, **fields):
        return client.post(path, headers={**headers, "Idempotency-Key": key},
            data={"provider": "yape", "sender": PHONE, "whatsapp_message_id": "wamid-cash-image",
                "looks_like_payment_receipt": "true", **fields},
            files={"file": ("receipt.png", b"\x89PNG\r\n\x1a\n" + key.encode(), "image/png")})

    cash_image = upload("cash-image")
    assert cash_image.status_code == 409
    assert cash_image.json()["code"] == "PAYMENT_METHOD_NOT_YAPE"

    with SessionLocal.begin() as db:
        db.get(Order, order["id"]).payment_method = "yape"
    for key, fields, status, code in (
        ("other-chat", {"sender": "51888888888"}, 404, None),
        ("missing-sender", {"sender": ""}, 404, None),
        ("not-classified", {"looks_like_payment_receipt": "false"}, 422, "PAYMENT_RECEIPT_REQUIRED"),
        ("classification-missing", {"looks_like_payment_receipt": ""}, 422, "PAYMENT_RECEIPT_REQUIRED"),
        ("message-id-missing", {"whatsapp_message_id": ""}, 422, "WHATSAPP_MESSAGE_ID_REQUIRED"),
        ("wrong-provider", {"provider": "plin"}, 409, "PAYMENT_METHOD_NOT_YAPE"),
    ):
        response = upload(key, **fields)
        assert response.status_code == status, response.text
        if code:
            assert response.json()["code"] == code

    async def rejected_by_vision(*_args, **_kwargs):
        return {"looks_like_payment_receipt": False}

    monkeypatch.setattr(api, "analyze_payment_image", rejected_by_vision)
    negative = upload("vision-rejected")
    assert negative.status_code == 422
    assert negative.json()["code"] == "PAYMENT_RECEIPT_REQUIRED"
    async def inconclusive_vision(*_args, **_kwargs):
        return {"available": True, "error": "invalid_model_json"}

    monkeypatch.setattr(api, "analyze_payment_image", inconclusive_vision)
    inconclusive = upload("vision-inconclusive")
    assert inconclusive.status_code == 422
    assert inconclusive.json()["code"] == "PAYMENT_RECEIPT_REQUIRED"
    with SessionLocal() as db:
        saved = db.get(Order, order["id"])
        assert saved.status == "draft" and saved.version == order["version"]
        assert saved.payment_status not in {"evidence_received", "invalid_evidence"}
        assert db.query(PaymentEvidence).count() == 0
        assert db.query(Payment).count() == 0
        assert db.query(KitchenTicket).count() == 0


def test_initial_receipt_saves_three_digit_code_and_replays_same_operation(
    client, tenant, auth_headers, monkeypatch,
):
    headers, order = setup(client, tenant, auth_headers)

    async def classified_receipt(*_args, **_kwargs):
        return {"looks_like_payment_receipt": True, "operation_number": "YP-12345",
            "security_code": "085", "recipient": "Pizza House", "amount": "20.00"}

    monkeypatch.setattr(api, "analyze_payment_image", classified_receipt)
    path = f"/api/v1/integrations/orders/{order['id']}/payment-evidence"
    # n8n includes empty multipart fields when vision cannot read a value.
    # The API must still save a classified receipt and use legible analysis fields.
    fields = {"provider": "yape", "sender": PHONE, "whatsapp_message_id": "wamid-three-digit",
        "looks_like_payment_receipt": "true", "amount_detected": "", "operation_number": "",
        "security_code": "", "recipient": ""}
    image = {"file": ("receipt.png", b"\x89PNG\r\n\x1a\nthree-digit", "image/png")}
    request_headers = {**headers, "Idempotency-Key": "one-image-one-operation"}
    first = client.post(path, headers=request_headers, data=fields, files=image)
    assert first.status_code == 201, first.text
    assert first.json()["evidence"]["security_code"] == "085"
    assert first.json()["evidence"]["operation_number"] == "YP-12345"
    assert first.json()["evidence"]["whatsapp_message_id"] == "wamid-three-digit"
    assert first.json()["order"]["payment_status"] == "evidence_received"

    repeated = client.post(path, headers=request_headers, data=fields, files=image)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json() == first.json()
    reused_key = client.post(path, headers=request_headers,
        data={**fields, "whatsapp_message_id": "wamid-other"}, files=image)
    assert reused_key.status_code == 409
    with SessionLocal() as db:
        assert db.query(PaymentEvidence).filter_by(order_id=order["id"]).count() == 1
        assert db.query(Payment).count() == 0
        assert db.query(KitchenTicket).count() == 0
