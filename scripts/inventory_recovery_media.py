"""Read-only media inventory; never rewrite a missing historical attachment."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import sqlalchemy as sa

from scripts.transfer_sqlite_to_postgres import Base, ROOT, sqlite_engine


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def inventory(source):
    engine = sqlite_engine(source)
    found = {}
    fields = {"storage_path", "logo_url", "image_url", "agent_menu_images"}
    try:
        with engine.connect() as db:
            for table in Base.metadata.sorted_tables:
                for column in table.c:
                    if not (column.name.endswith("storage_path") or column.name in fields):
                        continue
                    summary = {"local_available": 0, "local_missing": 0, "old_remote_unrecoverable": 0, "external_url_unverified": 0}
                    for value in db.scalars(sa.select(column).where(column.is_not(None))):
                        for item in strings(value):
                            if item.startswith("supabase://") or ".supabase.co" in urlparse(item).netloc:
                                summary["old_remote_unrecoverable"] += 1
                            elif item.startswith(("https://", "http://")):
                                summary["external_url_unverified"] += 1
                            elif item.startswith(("uploads/", "uploads\\")) or Path(item).is_absolute():
                                path = Path(item)
                                path = path if path.is_absolute() else ROOT / path
                                summary["local_available" if path.is_file() else "local_missing"] += 1
                    if any(summary.values()):
                        found[f"{table.name}.{column.name}"] = summary
    finally:
        engine.dispose()
    return {"fields": found, "files_rewritten": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    print(json.dumps(inventory(parser.parse_args().source), indent=2))
