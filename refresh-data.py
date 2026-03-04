#!/usr/bin/env python3
"""Mission Control data refresh script.

Pulls data from OpenClaw session stats, Railway usage, DataForSEO balance,
CosmoDigest health endpoints, and outputs data.json for the dashboard.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "data.json"
TZ = ZoneInfo("America/New_York")
DATAFORSEO_AUTH = base64.b64encode(
    b"hello@cosmodigest.io:a8b726d0c20e742f"
).decode("utf-8")
RAILWAY_API = "https://backboard.railway.app/graphql/v2"
RAILWAY_MEASUREMENTS = [
    "CPU_USAGE",
    "MEMORY_USAGE_GB",
    "NETWORK_RX_GB",
    "NETWORK_TX_GB",
    "DISK_USAGE_GB",
]
MODEL_PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "gpt-5.1-codex": {"input": 2.0, "output": 2.0},
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-5.1": {"input": 1.25, "output": 10.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "default": {"input": 2.0, "output": 8.0},
}


def run_command(cmd: List[str], cwd: Optional[Path] = None) -> str:
    return subprocess.run(  # noqa: S603
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def read_json_command(cmd: List[str], cwd: Optional[Path] = None) -> Any:
    try:
        raw = run_command(cmd, cwd=cwd)
        return json.loads(raw)
    except Exception:
        return None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model.lower(), MODEL_PRICING["default"])
    return (
        (input_tokens / 1_000_000) * pricing["input"]
        + (output_tokens / 1_000_000) * pricing["output"]
    )


def collect_openclaw() -> Dict[str, Any]:
    payload = {
        "todayTokens": 0,
        "todayCostUsd": 0.0,
        "modelsUsed": [],
        "sessionCount": 0,
        "note": None,
    }
    status = read_json_command(["openclaw", "status", "--json"])
    if not status:
        payload["note"] = "Unable to read OpenClaw status"
        return payload

    sessions = status.get("sessions", {}).get("recent", [])
    payload["sessionCount"] = len(sessions)
    today_date = dt.datetime.now(tz=TZ).date()
    model_rollup: Dict[str, Dict[str, Any]] = {}

    for session in sessions:
        model = session.get("model") or "unknown"
        updated_ms = session.get("updatedAt")
        if not updated_ms:
            continue
        updated_local = (
            dt.datetime.fromtimestamp(updated_ms / 1000, tz=dt.timezone.utc)
            .astimezone(TZ)
        )
        if updated_local.date() != today_date:
            continue
        input_tokens = int(session.get("inputTokens") or 0)
        output_tokens = int(session.get("outputTokens") or 0)
        tokens = input_tokens + output_tokens
        payload["todayTokens"] += tokens
        cost = estimate_cost(model, input_tokens, output_tokens)
        payload["todayCostUsd"] += cost
        slot = model_rollup.setdefault(
            model,
            {"model": model, "sessions": 0, "tokens": 0, "costUsd": 0.0},
        )
        slot["sessions"] += 1
        slot["tokens"] += tokens
        slot["costUsd"] += cost

    payload["todayCostUsd"] = round(payload["todayCostUsd"], 4)
    payload["modelsUsed"] = [
        {
            "model": info["model"],
            "sessions": info["sessions"],
            "tokens": info["tokens"],
            "costUsd": round(info["costUsd"], 4),
        }
        for info in sorted(
            model_rollup.values(), key=lambda row: row["costUsd"], reverse=True
        )
    ]
    if not payload["modelsUsed"]:
        payload["note"] = "No sessions logged today"
    return payload


@dataclass
class RailwayAuth:
    token: str
    project_id: str


def load_railway_auth() -> Optional[RailwayAuth]:
    config_path = Path.home() / ".railway" / "config.json"
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text())
    except Exception:
        return None
    token = data.get("user", {}).get("token")
    projects = data.get("projects", {})
    project_id = None
    for entry in projects.values():
        if entry.get("name") == "cosmodigest":
            project_id = entry.get("project")
            break
    if not project_id and projects:
        project_id = next(iter(projects.values())).get("project")
    if not token or not project_id:
        return None
    return RailwayAuth(token=token, project_id=project_id)


def collect_railway() -> Dict[str, Any]:
    payload = {
        "monthSpendUsd": None,
        "estimateUsd": None,
        "breakdown": [],
        "note": None,
    }
    auth = load_railway_auth()
    if not auth:
        payload["note"] = "Railway token/project id missing"
        return payload

    query = (
        "query($projectId: String!, $measurements: [MetricMeasurement!]!) {"
        "  estimatedUsage(projectId: $projectId, measurements: $measurements) {"
        "    measurement"
        "    estimatedValue"
        "  }"
        "}"
    )
    try:
        resp = requests.post(
            RAILWAY_API,
            headers={
                "Authorization": f"Bearer {auth.token}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "variables": {
                    "projectId": auth.project_id,
                    "measurements": RAILWAY_MEASUREMENTS,
                },
            },
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        payload["note"] = f"Railway API error: {exc}"
        return payload

    data = resp.json()
    estimates = data.get("data", {}).get("estimatedUsage", [])
    total = 0.0
    breakdown = []
    for entry in estimates:
        measurement = entry.get("measurement")
        value = float(entry.get("estimatedValue") or 0)
        usd_value = round(value / 100, 2)  # API returns approximate cents
        total += usd_value
        breakdown.append({"measurement": measurement, "usd": usd_value})

    payload["breakdown"] = breakdown
    payload["monthSpendUsd"] = round(total, 2)
    payload["estimateUsd"] = payload["monthSpendUsd"]
    payload["note"] = "Derived from Railway estimatedUsage"
    return payload


def collect_dataforseo() -> Dict[str, Any]:
    payload = {"balance": None, "currency": None, "lastUpdated": None, "note": None}
    try:
        resp = requests.get(
            "https://api.dataforseo.com/v3/appendix/user_data",
            headers={"Authorization": f"Basic {DATAFORSEO_AUTH}"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        payload["note"] = f"DataForSEO error: {exc}"
        return payload

    tasks = data.get("tasks") or []
    if tasks:
        result = tasks[0].get("result") or []
        if result:
            money = result[0].get("money", {})
            payload["balance"] = float(money.get("balance") or 0.0)
            payload["currency"] = money.get("currency_code") or "USD"
    payload["lastUpdated"] = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    return payload


def collect_cosmodigest() -> Dict[str, Any]:
    payload = {
        "users": None,
        "articles": None,
        "sources": None,
        "healthy": False,
        "responseMs": None,
        "note": None,
    }
    try:
        resp = requests.get(
            "https://cosmodigest-production.up.railway.app/health",
            timeout=10,
        )
        resp.raise_for_status()
        payload["responseMs"] = int(resp.elapsed.total_seconds() * 1000)
        data = resp.json()
        payload["healthy"] = data.get("status") == "healthy"
    except Exception as exc:  # noqa: BLE001
        payload["note"] = f"Health check error: {exc}"
        return payload

    try:
        stats_resp = requests.get(
            "https://cosmodigest-production.up.railway.app/api/v1/admin/stats",
            timeout=10,
        )
        if stats_resp.status_code == 200:
            stats = stats_resp.json()
            payload["users"] = stats.get("users")
            payload["articles"] = stats.get("articles")
            payload["sources"] = stats.get("sources")
        else:
            payload["note"] = "Admin stats require auth"
    except Exception:
        payload["note"] = "Unable to load CosmoDigest stats"
    return payload


def collect_gumroad() -> Dict[str, Any]:
    return {
        "sales": None,
        "revenue": None,
        "note": "Needs Gumroad API token for live data",
    }


def assemble_payload() -> Dict[str, Any]:
    return {
        "updatedAt": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "railway": collect_railway(),
        "dataforseo": collect_dataforseo(),
        "gumroad": collect_gumroad(),
        "cosmodigest": collect_cosmodigest(),
        "openclaw": collect_openclaw(),
        "activeAgents": {
            "notes": "Update manually with currently running subagents",
        },
    }


def main() -> None:
    payload = assemble_payload()
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"Saved data to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
