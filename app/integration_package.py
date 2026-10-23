"""Tool-neutral API discovery. Tokens are never part of stored configuration."""

ROUTES = (
    ("restaurant_context", "GET", "/context", "menu:read"),
    ("yape_qr", "GET", "/context/yape-qr", "menu:read"),
    ("menu_card_image", "GET", "/context/menu-card", "menu:read"),
    ("menu_card_gallery_image", "GET", "/context/menu-cards/{image_id}", "menu:read"),
    ("menu", "GET", "/context/menu", "menu:read"),
    ("customer_catalog", "GET", "/context/catalog", "menu:read"),
    ("inventory", "GET", "/context/inventory", "inventory:read"),
    ("adjust_inventory", "POST", "/inventory/{item_id}/adjust", "inventory:write"),
    ("set_product_availability", "PATCH", "/context/menu/{product_id}/availability", "inventory:write"),
    ("tables", "GET", "/context/tables", "reservations:write"),
    ("reservation_availability", "GET", "/context/availability", "reservations:write"),
    ("create_order_draft", "POST", "/orders/draft", "orders:write"),
    ("preview_order", "POST", "/orders/preview", "orders:write"),
    ("update_order", "PATCH", "/orders/{order_id}", "orders:write"),
    ("confirm_order", "POST", "/orders/{order_id}/confirm", "orders:write"),
    ("confirm_cash_order", "POST", "/orders/{order_id}/cash-confirm", "orders:write"),
    ("payment_evidence", "POST", "/orders/{order_id}/payment-evidence", "payments:write"),
    ("order_status", "GET", "/orders/{order_id}/status", "orders:read"),
    ("customer_order_state", "GET", "/orders/{order_id}/customer-state", "orders:read"),
    ("add_order_items", "POST", "/orders/{order_id}/item-batches", "orders:write"),
    ("choose_delivery_payment", "PATCH", "/orders/{order_id}/delivery-payment", "orders:write"),
    ("request_human", "POST", "/orders/{order_id}/request-human", "orders:write"),
    ("create_reservation", "POST", "/reservations", "reservations:write"),
    ("events", "GET", "/events", "events:read"),
    ("ack_event", "POST", "/events/{event_id}/ack", "events:read"),
    ("fulfillment_preview", "POST", "/orders/{order_id}/fulfillment-preview", "orders:write"),
    ("change_fulfillment", "PATCH", "/orders/{order_id}/fulfillment", "orders:write"),
    ("attention_request", "POST", "/attention-requests", "agent:attention"),
    ("conversation_claim", "POST", "/conversations/claim", "agent:state"),
    ("conversation_commit", "POST", "/conversations/{conversation_id}/commit", "agent:state"),
    ("conversation_renew", "POST", "/conversations/{conversation_id}/renew", "agent:state"),
    ("conversation_next", "POST", "/conversations/{conversation_id}/next", "agent:state"),
    ("model_usage_reserve", "POST", "/conversations/{conversation_id}/model-usage/reserve", "agent:state"),
    ("model_usage_settle", "POST", "/conversations/{conversation_id}/model-usage/{reservation_id}/settle", "agent:state"),
    ("conversation_enqueue", "POST", "/conversations/enqueue", "agent:state"),
    ("conversation_work", "POST", "/conversations/work/claim", "agent:state"),
    ("conversation_outbox", "POST", "/conversations/outbox/claim", "agent:state"),
    ("conversation_response_ack", "PATCH", "/conversations/outbox/{delivery_id}/ack", "agent:state"),
)


def integration_package(api_base_url: str, business_id: int, branch_id: int, scopes: list[str], profile: str = "generic") -> dict:
    base = api_base_url.rstrip("/")
    from .agent_runtime_api import AGENT_ROUTE_ALLOWLIST
    return {
        "api_base_url": base, "business_id": business_id, "branch_id": branch_id,
        "authentication": "bearer", "authorization_header": "Authorization",
        "write_idempotency_header": "Idempotency-Key", "scopes": sorted(set(scopes)),
        "profile": profile,
        "conversation_headers": ["X-Agent-Conversation", "X-Agent-Lease"] if profile == "restaurant_agent" else [],
        "endpoints": {
            name: {"method": method, "url": f"{base}/integrations{path}", "scope": scope}
            for name, method, path, scope in ROUTES if scope in scopes
            and (profile != "restaurant_agent" or (method, f"/integrations{path}") in AGENT_ROUTE_ALLOWLIST)
        },
    }
