"""OAuth CRM и service account — только через env / файлы в examples/."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_crm_oauth() -> dict[str, Any]:
    path = os.environ.get("CRM_SECRETS_JSON", "").strip()
    if not path:
        path = str(Path(__file__).resolve().parent / "examples" / "crm_secrets.json")
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(
            f"CRM secrets not found: {p}. Copy examples/crm_secrets.json.example"
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {
        "username": raw["username"],
        "password": raw["password"],
        "account_id": int(raw["account_id"]),
        "client_id": int(raw["client_id"]),
        "client_secret": raw["client_secret"],
        "grant_type": raw.get("grant_type", "login"),
    }


def google_service_account_path() -> Path:
    env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if env:
        return Path(env).expanduser()
    p = Path(__file__).resolve().parent / "examples" / "service-account.json"
    if not p.is_file():
        raise FileNotFoundError(
            f"Service account not found: {p}. Copy examples/service-account.json.example"
        )
    return p
