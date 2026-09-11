"""Prepare the smallest executable input for mono."""
import json
from pathlib import Path


def run(ws: Path) -> int:
    plan_path = ws / "migration-plan.json"
    if not plan_path.exists():
        plan_path = ws / "P1" / "modules" / "deps.json"
    if not plan_path.exists():
        raise ValueError("pre-mono requires migration-plan.json or P1/modules/deps.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    order = list(plan.get("order") or [])
    raw = plan.get("modules") or {}
    modules = {}
    for name in order:
        value = raw.get(name, {}) if isinstance(raw, dict) else {}
        if not isinstance(value, dict):
            value = {}
        modules[name] = {
            "source_files": value.get("source_files") or value.get("files") or [],
            "target": value.get("target") or value.get("target_home"),
            "depends_on": value.get("depends_on") or value.get("deps") or [],
            "verification": value.get("verification") or [],
            "status": value.get("status") or "planned",
        }
    manifest = {"version": 1, "modules": modules, "order": order,
                "driver_home": plan.get("driver_home"),
                "unknown": []}
    out = ws / "mono-input-manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    (ws / "mono-input-report.md").write_text(
        "# Pre-mono input\n\n" + "\n".join(
            f"- `{name}`: {len(item['source_files'])} source files"
            for name, item in modules.items()) + "\n",
        encoding="utf-8")
    return 0
