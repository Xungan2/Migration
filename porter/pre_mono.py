"""Prepare the smallest executable input for mono."""
import json
import re
from pathlib import Path


def run(ws: Path) -> int:
    plan_path = ws / "migration-plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan_text = ""
    else:
        candidates = [ws / "plan" / "migration-plan.md",
                      ws / "knowledgebase" / "plan" / "migration-plan.md"]
        source = next((p for p in candidates if p.exists()), None)
        if source is None:
            raise ValueError("pre-mono requires migration-plan.json or prepare migration-plan.md")
        plan_text = source.read_text(encoding="utf-8")
        phases = re.findall(r"\| \*\*(S\d+) [^|]+\|\s*([^|]+)\|\s*([^|]+)\|", plan_text)
        plan = {"order": [p[0] for p in phases],
                "modules": {name: {"verification": [check.strip()],
                                     "source_files": [], "depends_on": []}
                            for name, _work, check in phases},
                "driver_home": "aster-spi-nor"}
        for i, (name, work, _check) in enumerate(phases):
            plan["modules"][name]["description"] = work.strip()
            plan["modules"][name]["depends_on"] = [phases[i - 1][0]] if i else []
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
        if plan_text:
            (ws / "mono-input" / "modules").mkdir(parents=True, exist_ok=True)
            (ws / "mono-input" / "modules" / f"{name}.md").write_text(
                f"# {name}\n\n{value.get('description', '')}\n\n"
                + "\n".join(value.get("verification") or []) + "\n",
                encoding="utf-8")
    manifest = {"version": 1, "modules": modules, "order": order,
                "driver_home": plan.get("driver_home"),
                "unknown": []}
    if plan_text:
        (ws / "migration-plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    out = ws / "mono-input-manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    (ws / "mono-input-report.md").write_text(
        "# Pre-mono input\n\n" + "\n".join(
            f"- `{name}`: {len(item['source_files'])} source files"
            for name, item in modules.items()) + "\n",
        encoding="utf-8")
    return 0
