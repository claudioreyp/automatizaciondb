"""Additive printing JSON fields shared by settings and document snapshots."""

from copy import deepcopy


FONT_SIZES = ("small", "normal", "large")
PRINTING_JSON_DEFAULTS = {
    "printer_config": {"automatic_printing": True},
    "customer_ticket_template": {
        "font_size": "normal", "header_enabled": False, "header_text": "",
        "footer_enabled": False, "footer_text": "",
    },
    "kitchen_ticket_template": {"font_size": "normal"},
}


def printing_json(field: str, value: dict | None) -> dict:
    result = deepcopy(value) if isinstance(value, dict) else {}
    for key, default in PRINTING_JSON_DEFAULTS[field].items():
        supplied = result.get(key, default)
        if key == "font_size":
            valid = supplied in FONT_SIZES
        elif isinstance(default, bool):
            valid = type(supplied) is bool
        else:
            valid = isinstance(supplied, str) and len(supplied) <= 500
        result[key] = supplied if valid else default
    return result


def preserve_printing_extensions(field: str, supplied: dict, stored: dict | None) -> dict:
    # Legacy clients replace their known JSON, but cannot erase fields they predate.
    preserved = {key: value for key, value in (stored or {}).items()
                 if key in PRINTING_JSON_DEFAULTS[field] and key not in supplied}
    return {**deepcopy(preserved), **supplied}
