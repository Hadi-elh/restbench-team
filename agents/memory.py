"""Stub — Hadi's memory module. Will be replaced."""

import json


def parse_notes(s: str) -> dict:
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


def build_notes(d: dict) -> str:
    try:
        return json.dumps(d)[:4000]
    except Exception:
        return ""
