"""Commercial access is independent from staff permissions and service settings."""

POS_PLAN = "pos"
POS_MODULES = ("pos", "tables", "kds", "inventory", "cash", "delivery", "reservations", "whatsapp")


def full_pos_modules() -> dict[str, bool]:
    return dict.fromkeys(POS_MODULES, True)
