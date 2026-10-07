"""Regenerate the public example from defaults, never from a user's .env."""

import json
from pathlib import Path

from pydantic import SecretStr

from app.config import Settings

lines = [
    "# Local defaults. Docker Compose supplies its own DATABASE_URL.",
    "# Optional values are commented out; do not replace them with empty strings.",
    "# OWS_TOKEN must be issued by the official Electronic Finance Center.",
    "# List settings use JSON arrays. No real credentials belong in this file.",
    "",
]
for name, value in Settings(_env_file=None).model_dump().items():
    key = name.upper()
    if value is None:
        lines.append(f"# {key}=")
        continue
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if isinstance(value, Path):
        value = value.as_posix()
    lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
lines.extend(
    [
        "",
        "# Docker-only connection string; keep in sync if changing POSTGRES_* values.",
        "COMPOSE_DATABASE_URL=postgresql+asyncpg://procurement:procurement@postgres:5432/procurement",
        "POSTGRES_DB=procurement",
        "POSTGRES_USER=procurement",
        "POSTGRES_PASSWORD=procurement",
    ]
)
Path(".env.example").write_text("\n".join(lines) + "\n", encoding="utf-8")
