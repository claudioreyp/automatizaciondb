"""Export public contracts without loading private configuration or connecting a DB."""
import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
os.environ.update(ESCALAR_ENV_FILE_DISABLED="true", ENVIRONMENT="test",
    DATABASE_URL="sqlite+pysqlite:///:memory:", AUTO_CREATE_SCHEMA="false")
from app.main import app
from app.integration_package import integration_package
from app.schemas import RESTAURANT_AGENT_SCOPES

output = root / "docs" / "agent-tools-v1"
output.mkdir(parents=True, exist_ok=True)
documents = {
    "openapi.json": app.openapi(),
    "restaurant-agent-tools.json": integration_package("https://api.escalarai.tech/api/v1", 1, 1,
        sorted(RESTAURANT_AGENT_SCOPES), "restaurant_agent"),
}
for name, value in documents.items():
    (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"version": app.version, "output": str(output), "files": list(documents)}))
