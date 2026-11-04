"""Discovery must tell a transport how to call and recover each tool accurately."""
import pytest

from app.agent_tools import TOOLS
from app.integration_package import integration_package
from app.auth import ALL_INTEGRATION_SCOPES
from test_restaurant_agent_boundary import agent_headers


def package(profile):
    return integration_package("https://example.test/api/v1", 1, 1,
                               sorted(ALL_INTEGRATION_SCOPES), profile)["endpoints"]


def test_key_locations_match_real_route_parameters(client):
    document = client.get("/openapi.json").json()
    for tool in TOOLS:
        operation = document["paths"]["/api/v1" + tool.path][tool.method.lower()]
        header_names = {p["name"].lower() for p in operation.get("parameters", []) if p["in"] == "header"}
        if tool.idempotency_location == "header":
            assert tool.idempotency != "none" and "idempotency-key" in header_names, tool.name
        else:
            assert "idempotency-key" not in header_names, tool.name
        if tool.idempotency_location == "path":
            assert tool.idempotency_key_field == "event_id"
        if tool.idempotency == "none":
            assert tool.idempotency_location is None and tool.idempotency_key_field is None


@pytest.mark.parametrize("suffix", ["renew", "freshness", "defer"])
def test_fenced_runtime_reads_and_defer_still_require_request_key(client, agent_headers, suffix):
    result = client.post(f"/api/v1/integrations/conversations/no-such-conversation/{suffix}",
        headers=agent_headers, json={"lease_token": "x" * 32, "expected_version": 1})
    assert result.status_code == 422
    assert "Idempotency-Key" in result.json()["detail"]
    metadata = next(tool for tool in TOOLS if tool.path.endswith("/{conversation_id}/" + suffix))
    assert metadata.idempotency == "required" and metadata.idempotency_location == "header"


def test_transport_and_conversation_auth_are_distinct_and_profile_correct(client):
    for profile in ("generic", "restaurant_agent"):
        tools = package(profile)
        for name in ("conversation_commit", "conversation_renew", "model_usage_reserve", "model_usage_settle"):
            assert tools[name]["conversation_auth"] == "body"
            assert tools[name]["conversation_auth_fields"] == ["lease_token", "expected_version"]
        assert tools["conversation_next"]["conversation_auth"] == "none"
        assert tools["conversation_next"]["conversation_auth_fields"] == []
        assert tools["conversation_response_ack"]["conversation_auth"] == "body"
        assert tools["conversation_response_ack"]["conversation_auth_fields"] == ["delivery_token"]
    generic, agent = package("generic"), package("restaurant_agent")
    assert generic["create_order_draft"]["conversation_auth"] == "none"
    assert agent["create_order_draft"]["conversation_auth"] == "headers"
    assert agent["create_order_draft"]["conversation_auth_fields"] == ["X-Agent-Conversation", "X-Agent-Lease"]
    assert agent["conversations_native_conversation_id_next"]["conversation_auth"] == "none"
    assert agent["conversations_outbox_delivery_id_freshness"]["conversation_auth_fields"] == ["delivery_token"]


def test_recovery_metadata_does_not_promise_a_new_transport_authorization(client):
    tools = package("restaurant_agent")
    for name in ("conversation_outbox", "conversations_native_outbox_claim"):
        assert tools[name]["idempotency_replay"] == "uncertain_on_replay"
    for name in ("conversation_work", "conversation_next", "conversation_renew",
                 "conversations_conversation_id_freshness", "conversations_media_reservations_inspect"):
        assert tools[name]["idempotency_replay"] == "current_state"
    reconcile = tools["reconcile_payment_evidence"]
    assert (reconcile["idempotency_location"], reconcile["idempotency_key_field"], reconcile["idempotency_replay"]) == (
        "body", "idempotency_key", "original_operation_result")
    for name in ("queue_event_notice", "resolve_event_notice"):
        assert package("generic")[name]["idempotency_location"] == "path"
    assert tools["conversations_media_reservations_inspect"]["read_only"] is True


def test_no_key_tools_are_explicit_without_hiding_freshness_requirement(client):
    for name in ("turn_context", "validate_selection", "conversations_conversation_id_media_read"):
        assert package("restaurant_agent")[name]["idempotency"] == "none"
    assert package("restaurant_agent")["conversations_outbox_delivery_id_freshness"]["idempotency"] == "required"


def test_published_schemas_describe_binary_events_and_private_transport_tokens(client):
    document = client.get("/openapi.json").json()
    def response(path, method="get", status="200"):
        return document["paths"]["/api/v1/integrations/"+path][method]["responses"][status]["content"]
    assert response("events")["application/json"]["schema"] == {"type": "array", "items": {"$ref": "#/components/schemas/IntegrationEventResult"}}
    assert response("events/{event_id}/ack", "post")["application/json"]["schema"]["$ref"].endswith("EventAcknowledgement")
    assert set(response("context/yape-qr")) == {"image/png", "image/jpeg", "image/webp"}
    assert "delivery_token" in document["components"]["schemas"]["ConversationToolResult"]["properties"]
    assert "preview_fingerprint" in document["components"]["schemas"]["PaymentMethodPreview"]["properties"]
    for tool in TOOLS:
        operation = document["paths"]["/api/v1"+tool.path][tool.method.lower()]
        assert operation["x-idempotency-location"] == tool.idempotency_location
        assert operation["x-conversation-auth-fields"] == list(tool.conversation_auth_fields)
        assert operation["security"] == [{"IntegrationBearer": []}]


def test_success_contracts_publish_values_needed_by_following_operations(client):
    document = client.get("/openapi.json").json()
    expected = {
        ("post", "orders/{order_id}/fulfillment-preview"): ("FulfillmentPreview", {"order_version", "quote", "final_total"}),
        ("post", "orders/{order_id}/item-revisions/preview"): ("OriginalItemsPreview", {"order_version", "quote_fingerprint", "expected_total", "items"}),
        ("post", "conversations/work/claim"): ("ConversationWorkClaim", {"status"}),
        ("post", "conversations/native/work/claim"): ("ConversationWorkClaim", {"status"}),
        ("patch", "context/menu/{product_id}/availability"): ("ProductAvailabilityResult", {"id", "available"}),
        ("post", "reservations"): ("ReservationResult", {"id", "business_id", "branch_id", "table_ids", "version"}),
        ("get", "orders/{order_id}/status"): ("OrderSnapshot", {"id", "version", "payment_status", "items"}),
        ("post", "events/{event_id}/outbox"): ("EventNoticeResult", {"event_id", "event_acknowledged"}),
    }
    for (method, path), (model, required) in expected.items():
        responses = document["paths"]["/api/v1/integrations/" + path][method]["responses"]
        success = next(value for status, value in responses.items() if status.startswith("2"))
        assert success["content"]["application/json"]["schema"]["$ref"].endswith("/" + model)
        schema = document["components"]["schemas"][model]
        assert set(schema["required"]) >= required
        assert schema["additionalProperties"] is True
    quote = document["components"]["schemas"]["DeliveryQuoteSnapshot"]
    assert set(quote["required"]) >= {"id", "fee", "configuration_version", "expires_at"}
    work = document["components"]["schemas"]["ConversationWorkClaim"]
    assert set(work["properties"]) >= {"message", "input_ids", "lease_until", "retry_after_ms"}
    assert "lease_token" not in work["required"]  # Idle/queued responses never grant a lease.
    notice = document["components"]["schemas"]["EventNoticeResult"]
    assert {item["type"] for item in notice["properties"]["attention_id"]["anyOf"]} == {"string", "null"}


def test_fulfillment_preview_contract_supports_the_real_commit(client, tenant, auth_headers):
    from app.agent_contracts import FulfillmentPreview
    from test_agent_domain_revision import setup_order, PHONE, DESTINATION
    headers, order = setup_order(client, tenant, auth_headers, fee=6)
    body = {"sender": PHONE, "expected_version": order["version"], "channel": "delivery", "delivery_address": DESTINATION}
    path = f"/api/v1/integrations/orders/{order['id']}"
    response = client.post(path + "/fulfillment-preview", headers={**headers, "Idempotency-Key": "contract-fulfillment"}, json=body)
    assert response.status_code == 200, response.text
    preview = FulfillmentPreview.model_validate(response.json())
    assert preview.order_version == order["version"] and preview.final_total == 26
    committed = client.patch(path + "/fulfillment", headers={**headers, "Idempotency-Key": "contract-fulfillment-commit"},
        json={**body, "expected_version": preview.order_version, "quote_id": preview.quote.id, "expected_delivery_fee": preview.quote.fee})
    assert committed.status_code == 200, committed.text
    assert committed.json()["order"]["total"] == preview.final_total


def test_original_items_preview_contract_supports_the_real_commit(client, tenant, auth_headers):
    from app.agent_contracts import OriginalItemsPreview
    from test_agent_checkout import setup, PHONE
    headers, order = setup(client, tenant, auth_headers, method="yape")
    body = {"sender": PHONE, "expected_version": order["version"], "operations": [{"type": "edit", "item_id": order["items"][0]["id"],
        "replacement": {"product_id": tenant["product_id"], "quantity": 2, "notes": "Corrección sintética"}}]}
    path = f"/api/v1/integrations/orders/{order['id']}/item-revisions"
    response = client.post(path + "/preview", headers={**headers, "Idempotency-Key": "contract-items"}, json=body)
    assert response.status_code == 200, response.text
    preview = OriginalItemsPreview.model_validate(response.json())
    assert preview.order_version == order["version"] and preview.expected_total == 40
    committed = client.post(path, headers={**headers, "Idempotency-Key": "contract-items-commit"},
        json={**body, "expected_version": preview.order_version, "quote_fingerprint": preview.quote_fingerprint, "expected_total": preview.expected_total})
    assert committed.status_code == 200, committed.text
    assert committed.json()["order"]["total"] == preview.expected_total


def test_work_claim_schema_matches_idle_and_acquired_without_granting_a_second_lease(client, agent_headers):
    from app.agent_contracts import ConversationWorkClaim
    from test_restaurant_agent_boundary import SENDER
    path = "/api/v1/integrations/conversations/work/claim"
    idle = client.post(path, headers={**agent_headers, "Idempotency-Key": "contract-idle"}, json={})
    assert idle.status_code == 200, idle.text
    assert ConversationWorkClaim.model_validate(idle.json()).status == "idle"
    enqueued = client.post("/api/v1/integrations/conversations/enqueue", headers={**agent_headers, "Idempotency-Key": "contract-enqueue"},
        json={"sender": SENDER, "message_id": "contract-message", "message": {"text": "dos pizzas"}})
    assert enqueued.status_code == 200, enqueued.text
    response = client.post(path, headers={**agent_headers, "Idempotency-Key": "contract-work"}, json={})
    assert response.status_code == 200, response.text
    acquired = ConversationWorkClaim.model_validate(response.json())
    assert acquired.status == "acquired" and acquired.input_ids == ["contract-message"]
    assert acquired.message["text"] == "dos pizzas" and acquired.lease_until is not None
    again = client.post(path, headers={**agent_headers, "Idempotency-Key": "contract-work-2"}, json={})
    assert again.status_code == 200, again.text
    assert ConversationWorkClaim.model_validate(again.json()).status == "idle"
