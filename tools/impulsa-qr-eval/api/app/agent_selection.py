"""Read-only validation of incomplete consumer choices; never creates a quote."""
from collections import Counter

from fastapi import APIRouter, Depends, Response
from fastapi import HTTPException
from .agent_selection_schemas import PartialSelection

from .auth import IntegrationAuthContext, require_integration_scope
from .database import get_db
from .agent_contracts import SelectionValidation

router = APIRouter(prefix="/api/v1/integrations/orders", tags=["agent-selection"])








def validate_selection(payload: PartialSelection, catalog: dict, *, payment_methods=None):
    missing, issues = [], []
    def report(target, path, code, message, **fields):
        target.append({"path": path, "code": code, "message": message, **fields})
    if payload.channel is None:
        report(missing, "channel", "SERVICE_REQUIRED", "Selecciona una modalidad de atención.")
    if payload.payment_method is None:
        report(missing, "payment_method", "PAYMENT_METHOD_REQUIRED", "Selecciona Yape o efectivo.")
    if payload.channel == "delivery":
        if not payload.delivery_address:
            report(missing, "delivery_address", "DELIVERY_DESTINATION_REQUIRED", "Falta la dirección o ubicación de entrega.")
        else:
            from .integration_order_validation import validate_delivery_destination
            try:
                validate_delivery_destination(payload.delivery_address.model_dump(exclude_none=True))
            except HTTPException as exc:
                code = getattr(exc, "code", "DELIVERY_DESTINATION_REQUIRED")
                field = "delivery_address.reference" if code == "DELIVERY_REFERENCE_REQUIRED" else "delivery_address"
                report(missing, field, code, "Falta una referencia de entrega." if code == "DELIVERY_REFERENCE_REQUIRED"
                    else "Completa una dirección o ubicación válida.")
    elif payload.channel == "takeaway" and payload.delivery_address:
        report(issues, "delivery_address", "UNEXPECTED_DELIVERY_DESTINATION", "Un pedido para recoger no requiere dirección de entrega.")
    if not payload.items:
        report(missing, "items", "PRODUCT_REQUIRED", "Selecciona al menos un producto.")
    products = {p["id"]: p for p in catalog["products"]}
    if catalog.get("open_now") is not True:
        report(issues, "service", "SERVICE_UNVERIFIED" if catalog.get("open_now") is None else "SERVICE_CLOSED",
               "El restaurante no tiene recepción de pedidos disponible y verificada.")
    if payload.payment_method and payment_methods is not None and payload.channel is not None:
        if payload.payment_method not in payment_methods.get(payload.channel, []):
            report(issues, "payment_method", "PAYMENT_METHOD_DISABLED", "Ese método de pago no está habilitado.")
    for index, line in enumerate(payload.items):
        path = f"items[{index}]"
        if line.product_id is None:
            report(missing, path + ".product_id", "PRODUCT_REQUIRED", "Selecciona un producto del catálogo.")
            continue
        product = products.get(line.product_id)
        if product is None:
            report(issues, path + ".product_id", "PRODUCT_UNAVAILABLE", "El producto no está en el catálogo de esta sucursal.")
            continue
        if not product["available"]:
            report(issues, path + ".product_id", "PRODUCT_UNAVAILABLE", "El producto no está disponible.")
        if payload.channel and not product["availability"]["channels"].get(payload.channel, False):
            report(issues, path + ".product_id", "PRODUCT_UNAVAILABLE_FOR_CHANNEL", "El producto no está disponible para esa modalidad.")
        requirements = product["selection_requirements"]
        variant = requirements["variant"]
        if variant["required"] and not line.variant_name:
            report(missing, path + ".variant_name", "VARIANT_REQUIRED", "Selecciona el tamaño o variante.", options=variant["options"])
        elif line.variant_name and not any(v["available"] and v["name"].strip().casefold() == line.variant_name.strip().casefold()
                                         for v in variant["options"]):
            report(issues, path + ".variant_name", "VARIANT_UNAVAILABLE", "La variante seleccionada no está disponible.", options=variant["options"])
        counts, group_counts = Counter(), Counter()
        groups = requirements["modifier_groups"]
        for selected_index, selected in enumerate(line.modifiers):
            matches = [(g, m) for g in groups for m in g["options"] if
                (m["id"] == selected.modifier_id if selected.modifier_id is not None else
                 bool(selected.name) and m["name"].strip().casefold() == selected.name.strip().casefold())]
            if len(matches) != 1 or not matches[0][1]["available"]:
                report(issues, f"{path}.modifiers[{selected_index}]", "MODIFIER_UNAVAILABLE", "La opción no está disponible para este producto.")
                continue
            group, option = matches[0]
            counts[option["id"]] += selected.quantity
            group_counts[group["group_id"]] += selected.quantity
        for group in groups:
            count = group_counts[group["group_id"]]
            group_path = f"{path}.modifier_groups[{group['group_id']}]"
            if count < group["minimum"]:
                report(missing, group_path, "MODIFIER_MINIMUM_REQUIRED", f"Completa las opciones de {group['name']}.",
                    options=group["options"], minimum=group["minimum"], maximum=group["maximum"])
            if group["maximum"] is not None and count > group["maximum"]:
                report(issues, group_path, "MODIFIER_MAXIMUM_EXCEEDED", f"Hay demasiadas opciones de {group['name']}.", maximum=group["maximum"])
            for option in group["options"]:
                limit = group["max_per_option"] if group["allow_repeats"] else 1
                if limit is not None and counts[option["id"]] > limit:
                    report(issues, group_path, "MODIFIER_REPEAT_LIMIT", f"Se excedió el límite de {option['name']}.", maximum=limit)
    valid = not missing and not issues
    return {"schema_version": 1, "business_id": catalog["business_id"], "branch_id": catalog["branch_id"],
        "catalog_version": catalog["catalog_version"], "valid": valid, "ready_for_preview": valid,
        "missing_fields": missing, "issues": issues, "pricing_authority": "server_preview"}


@router.post("/validate-selection", response_model=SelectionValidation, response_model_exclude_unset=True,
    summary="Validar elecciones incompletas sin crear un pedido ni cotización")
def check_selection(payload: PartialSelection, response: Response,
    integration: IntegrationAuthContext = Depends(require_integration_scope("menu:read", read_only=True)), db=Depends(get_db)):
    from .agent_checkout_api import catalog
    from .api import integration_branch_from_auth
    from .agent_context import agent_context
    _, branch = integration_branch_from_auth(db, integration, None)
    menu = catalog(response, integration=integration, db=db)
    context = agent_context(db, branch, restaurant_agent=integration.profile == "restaurant_agent")
    response.headers["Cache-Control"] = "private, no-store"
    return validate_selection(payload, menu, payment_methods=context["payments"]["methods"])
