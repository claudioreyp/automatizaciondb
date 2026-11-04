"""Consistent errors and schemas for integration consumers; POS v1 stays compatible."""
from uuid import uuid4
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from .agent_contracts import (AgentError, OrderToolResult, ConversationToolResult, AttentionToolResult,
    IntegrationEventResult, EventAcknowledgement, PaymentMethodPreview, InventoryContext,
    InventoryItemResult, TableAvailability, LegacyMenu)
from .agent_tools import TOOLS_BY_ROUTE


def error_body(request, detail, status, code=None, issues=None):
    if code is None and isinstance(detail, dict):
        code = detail.get("code")
    code = code or {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
        409: "STATE_CONFLICT", 422: "VALIDATION_ERROR", 429: "RATE_LIMITED"}.get(status, f"HTTP_{status}")
    return {"detail": detail, "code": code, "request_id": getattr(request.state, "request_id", str(uuid4())),
        "retryable": status in {408, 425, 429, 502, 503, 504}, "issues": issues or []}


async def integration_http_exception(request: Request, exc: HTTPException):
    if not request.url.path.startswith("/api/v1/integrations/"):
        return await http_exception_handler(request, exc)
    return JSONResponse(status_code=exc.status_code,
        content=jsonable_encoder(error_body(request, exc.detail, exc.status_code)), headers=exc.headers)


def install_openapi(app):
    def document():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        errors = AgentError.model_json_schema(ref_template="#/components/schemas/{model}")
        definitions = errors.pop("$defs", {})
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(definitions)
        components["AgentError"] = errors
        for model in (OrderToolResult, ConversationToolResult, AttentionToolResult, IntegrationEventResult,
                      EventAcknowledgement, PaymentMethodPreview, InventoryContext, InventoryItemResult, TableAvailability, LegacyMenu):
            model_schema = model.model_json_schema(ref_template="#/components/schemas/{model}", mode="serialization")
            for name, value in model_schema.pop("$defs", {}).items():
                components.setdefault(name, value)
            components[model.__name__] = model_schema
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                tool = TOOLS_BY_ROUTE.get((method.upper(), path.removeprefix("/api/v1")))
                if tool is None:
                    continue
                operation["x-tool-name"] = tool.name
                operation["x-required-scopes"] = list(tool.required_scopes)
                operation["x-read-only"] = tool.read_only
                operation["x-idempotency"] = tool.idempotency
                operation["x-idempotency-location"] = tool.idempotency_location
                operation["x-idempotency-key-field"] = tool.idempotency_key_field
                operation["x-idempotency-replay"] = tool.idempotency_replay
                operation["x-conversation-auth"] = tool.conversation_auth
                operation["x-conversation-auth-fields"] = list(tool.conversation_auth_fields)
                schema["components"].setdefault("securitySchemes", {})["IntegrationBearer"] = {
                    "type": "http", "scheme": "bearer", "bearerFormat": "integration-token"}
                operation["security"] = [{"IntegrationBearer": []}]
                if tool.conversation_auth == "headers":
                    parameters = operation.setdefault("parameters", [])
                    for name in tool.conversation_auth_fields:
                        if not any(p.get("in") == "header" and p.get("name") == name for p in parameters):
                            parameters.append({"in": "header", "name": name, "required": False,
                                "schema": {"type": "string"}, "x-required-for-profile": "restaurant_agent",
                                "description": "Required for restaurant_agent credentials; private runtime context, never model input."})
                for status, response in operation.setdefault("responses", {}).items():
                    if not status.startswith("2"):
                        continue
                    content = response.setdefault("content", {})
                    if not content.get("application/json", {}).get("schema"):
                        if tool.method == "GET" and any(part in tool.path for part in ("yape-qr", "menu-card")):
                            response["content"] = {mime: {"schema": {"type": "string", "format": "binary"}}
                                                   for mime in ("image/png", "image/jpeg", "image/webp")}
                        else:
                            model = {"ack_event": EventAcknowledgement, "inventory": InventoryContext,
                                "adjust_inventory": InventoryItemResult, "tables": TableAvailability,
                                "reservation_availability": TableAvailability, "menu": LegacyMenu,
                                "preview_original_payment_method": PaymentMethodPreview}.get(tool.name)
                            model = model or (ConversationToolResult if "/conversations/" in tool.path
                                              else AttentionToolResult if "attention" in tool.path else OrderToolResult)
                            shape = {"$ref": f"#/components/schemas/{model.__name__}"}
                            if tool.name == "events":
                                shape = {"type": "array", "items": {"$ref": "#/components/schemas/IntegrationEventResult"}}
                            content["application/json"] = {"schema": shape}
                for status in (401, 403, 404, 409, 422, 429, 503):
                    operation.setdefault("responses", {})[str(status)] = {"description": "Error de integración con código estable.",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/AgentError"}}}}
        app.openapi_schema = schema
        return schema
    app.openapi = document
