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
