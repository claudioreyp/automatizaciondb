from collections import Counter
from copy import deepcopy

from .models import KitchenTicket


def modifier_key(option: dict) -> tuple:
    return (
        option.get("modifier_id"), option.get("name"),
        option.get("group_id"), option.get("group_name"),
        str(option.get("price_delta", 0)),
    )


def removed_modifiers(before: list[dict], after: list[dict]) -> list[dict]:
    remaining = Counter(modifier_key(option) for option in after)
    removed = []
    for option in before:
        key = modifier_key(option)
        if remaining[key]:
            remaining[key] -= 1
        else:
            removed.append(deepcopy(option))
    return removed


def effective_ticket_items(ticket: KitchenTicket) -> list[dict]:
    """Project revisions without overwriting the original kitchen snapshot."""
    items = deepcopy(ticket.items_snapshot or [])
    for revision in (ticket.context_snapshot or {}).get("revisions", []):
        before = revision["before"]
        after = deepcopy(revision["after"])
        for index, item in enumerate(items):
            if item.get("item_id") == before.get("item_id"):
                items[index] = after
                break
    return items


def retired_modifier_units(before: dict, after: dict, *, keep_history: bool) -> list[dict]:
    """Store absolute retired units so later quantity edits cannot multiply history."""
    previous = deepcopy(before.get("removed_modifiers", [])) if keep_history else []
    for option in previous:
        option.setdefault("removed_quantity", before.get("previous_quantity", before["quantity"]))
    remaining = Counter()
    for option in after.get("modifiers", []):
        remaining[modifier_key(option)] += float(after["quantity"])
    for option in before.get("modifiers", []):
        key = modifier_key(option)
        units = float(before["quantity"])
        kept = min(units, remaining[key])
        remaining[key] -= kept
        if units > kept:
            previous.append({**deepcopy(option), "removed_quantity": units - kept})
    return previous
