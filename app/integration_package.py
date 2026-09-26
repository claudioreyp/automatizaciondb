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
    ("revise_order_items", "POST", "/orders/{order_id}/item-revisions", "orders:write"),
    ("choose_delivery_payment", "PATCH", "/orders/{order_id}/delivery-payment", "orders:write"),
    ("request_human", "POST", "/orders/{order_id}/request-human", "orders:write"),
    ("create_reservation", "POST", "/reservations", "reservations:write"),
    ("events", "GET", "/events", "events:read"),
    ("ack_event", "POST", "/events/{event_id}/ack", "events:read"),
)


def integration_package(api_base_url: str, business_id: int, branch_id: int, scopes: list[str]) -> dict:
    base = api_base_url.rstrip("/")
    return {
        "api_base_url": base, "business_id": business_id, "branch_id": branch_id,
        "authentication": "bearer", "authorization_header": "Authorization",
        "write_idempotency_header": "Idempotency-Key", "scopes": sorted(set(scopes)),
        "endpoints": {
            name: {"method": method, "url": f"{base}/integrations{path}", "scope": scope}
            for name, method, path, scope in ROUTES if scope in scopes
        },
    }
