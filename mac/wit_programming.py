"""Shared WIT show personalities used by the generator, API and feeder."""

from pathlib import Path
import yaml

CONFIG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/wit_dj_personas.yaml").read_text())
PROFILES = CONFIG["hosts"]


def host_info(show_id: str, fallback: str) -> dict:
    profile = PROFILES.get(show_id, {})
    return {"host": profile.get("name", fallback), "dj_role": profile.get("role", "")}
