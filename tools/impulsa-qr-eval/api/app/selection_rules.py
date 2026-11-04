"""Selection rules shared by POS writes and the agent's public description."""


def effective_minimum(minimum, required):
    return max(minimum, 1 if required else 0)


def selection_requirements(product):
    variants = product["variants"]
    return {
        "variant": {
            "required": bool(variants), "field": "variant_name",
            "options": [{"id": v["id"], "name": v["name"], "available": v["available"]}
                        for v in variants],
        },
        "modifier_groups": [{
            "group_id": g["id"], "name": g["name"],
            "required": effective_minimum(g["minimum"], g["required"]) > 0,
            "minimum": effective_minimum(g["minimum"], g["required"]),
            "maximum": g["maximum"], "allow_repeats": g["allow_repeats"],
            "max_per_option": g["max_per_option"],
            "options": [{"id": m["id"], "name": m["name"], "available": m["available"]}
                        for m in g["modifiers"]],
        } for g in product["modifier_groups"]],
        "optional_extras_require_selection": False,
        "pricing_authority": "server_preview",
    }
