"""Tool-neutral discovery from the inventory used by authorization."""
from copy import deepcopy
from .agent_tools import TOOLS

ROUTES = tuple((t.name, t.method, t.path.removeprefix("/integrations"), t.scope) for t in TOOLS)
DESCRIPTIONS = {
    "restaurant_context": "Consultar horarios, modalidades, tiempos y datos vigentes de pago.",
    "customer_catalog": "Consultar productos, disponibilidad, tamaños y personalizaciones obligatorias u opcionales.",
    "validate_selection": "Validar elecciones incompletas y obtener datos faltantes sin registrar efectos comerciales.",
    "turn_context": "Leer restaurante, catálogo y pedido propio durante un turno autorizado.",
    "conversations_native_readiness": "Consultar si la cola nativa requiere un tick, sin tomar mensajes, leases ni permisos de envío.",
    "preview_order": "Calcular importes oficiales. Puede persistir una cotización; no registra el pedido.",
    "create_order_draft": "Registrar un borrador con productos validados y precios del servidor.",
    "customer_order_state": "Consultar pedido, pagos, comprobantes, cocina y acciones permitidas.",
    "add_order_items": "Solicitar un agregado con pago separado, conservando pagos y productos anteriores.",
    "change_fulfillment": "Aplicar un cambio de entrega permitido y dejar el aviso en el POS.",
    "payment_evidence": "Registrar un comprobante para revisión humana; no aprueba el pago.",
    "reconcile_payment_evidence": "Recuperar el resultado de un comprobante cuya respuesta se perdió.",
    "conversation_commit": "Guardar contexto y respuesta pendiente. La entrega requiere confirmación del transporte.",
    "conversation_response_ack": "Confirmar entrega, ausencia de envío o resultado incierto del transporte.",
}


def integration_package(api_base_url: str, business_id: int, branch_id: int, scopes: list[str], profile: str = "generic") -> dict:
    from .main import app
    document = app.openapi()
    components = document.get("components", {}).get("schemas", {})
    endpoints, used = {}, set()

    def references(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name not in used:
                    used.add(name)
                    references(components.get(name, {}))
            for child in value.values():
                references(child)
        elif isinstance(value, list):
            for child in value:
                references(child)

    for tool in TOOLS:
        if profile not in tool.profiles or not set(tool.required_scopes).issubset(scopes):
            continue
        operation = document.get("paths", {}).get("/api/v1" + tool.path, {}).get(tool.method.lower())
        if operation is None:
            raise RuntimeError(f"Registered integration tool has no route: {tool.name}")
        request = deepcopy(operation.get("requestBody"))
        responses = deepcopy(operation.get("responses", {}))
        parameters = deepcopy(operation.get("parameters", []))
        for part in (request, responses, parameters):
            references(part)
        # Header fencing is specific to restaurant agents. Lease/delivery tokens
        # in a route body are checked by the handler for every permitted profile.
        conversation_auth = tool.conversation_auth
        if conversation_auth == "headers" and profile != "restaurant_agent":
            conversation_auth = "none"
        endpoints[tool.name] = {"method": tool.method, "url": api_base_url.rstrip("/") + tool.path,
            "scope": tool.scope, "required_scopes": list(tool.required_scopes),
            "operation_id": operation["operationId"],
            "description": DESCRIPTIONS.get(tool.name, operation.get("summary", tool.name.replace("_", " "))),
            "read_only": tool.read_only, "idempotency": tool.idempotency,
            "idempotency_location": tool.idempotency_location,
            "idempotency_key_field": tool.idempotency_key_field,
            "idempotency_replay": tool.idempotency_replay,
            "conversation_auth": conversation_auth,
            "conversation_auth_fields": list(tool.conversation_auth_fields) if conversation_auth != "none" else [],
            "parameters": parameters, "request_body": request, "responses": responses}
    return {"package_version": 2, "api_contract_version": document["info"]["version"],
        "openapi_url": api_base_url.rstrip("/").removesuffix("/api/v1") + "/openapi.json",
        "api_base_url": api_base_url.rstrip("/"), "business_id": business_id, "branch_id": branch_id,
        "authentication": "bearer", "authorization_header": "Authorization",
        "write_idempotency_header": "Idempotency-Key", "scopes": sorted(set(scopes)), "profile": profile,
        "conversation_headers": ["X-Agent-Conversation", "X-Agent-Lease"] if profile == "restaurant_agent" else [],
        "endpoints": endpoints, "components": {"schemas": {name: deepcopy(components[name]) for name in sorted(used)}}}
