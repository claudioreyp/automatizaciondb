"""Checkout requirements at the integration boundary; manual POS stays unchanged."""
from math import isfinite
from urllib.parse import urlparse

from .errors import CodedHTTPException


def validate_integration_fulfillment(payload):
    if "channel" not in payload.model_fields_set:
        raise CodedHTTPException(422, "Select how the customer will receive the order", "ORDER_CHANNEL_REQUIRED")
    if payload.channel != "delivery":
        return
    destination = payload.delivery_address or {}
    def text(value):
        return value.strip() if isinstance(value, str) else ""

    address = text(destination.get("address") or destination.get("full_address"))
    maps_url = text(destination.get("maps_url") or destination.get("map_url"))
    try:
        parsed = urlparse(maps_url)
        maps = parsed.scheme == "https" and parsed.hostname in {
            "maps.app.goo.gl", "maps.google.com", "www.google.com", "google.com", "goo.gl"
        } and ("maps" in parsed.path or parsed.hostname in {"maps.app.goo.gl", "maps.google.com"})
    except ValueError:
        maps = False
    coordinates = False
    lat, lng = destination.get("latitude"), destination.get("longitude")
    if lat is not None and lng is not None and not isinstance(lat, bool) and not isinstance(lng, bool):
        try:
            lat, lng = float(lat), float(lng)
            coordinates = isfinite(lat) and isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180
        except (TypeError, ValueError):
            pass
    if not (len(address) >= 5 or maps or coordinates):
        raise CodedHTTPException(422, "Delivery requires a usable destination", "DELIVERY_DESTINATION_REQUIRED")
    if not text(destination.get("reference")):
        raise CodedHTTPException(422, "Delivery requires a delivery reference", "DELIVERY_REFERENCE_REQUIRED")
