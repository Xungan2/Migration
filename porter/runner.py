"""Shared machine-facing runner.json loader."""

from __future__ import annotations

import json
from pathlib import Path


class RunnerError(ValueError):
    pass


def load(path: Path, *, required: bool = True) -> dict:
    path = Path(path)
    if not path.is_file():
        if required:
            raise RunnerError(f"runner.json 不存在：{path}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"runner.json 不可解析：{path}（{exc}）") from exc
    if not isinstance(value, dict):
        raise RunnerError("runner.json 顶层必须是 JSON 对象")
    for name in ("build", "boot", "unit_test", "inject_device"):
        section = value.get(name)
        if section is not None and not isinstance(section, dict):
            raise RunnerError(f"runner.json.{name} 必须是对象")
    if value.get("env") is not None and not isinstance(value["env"], dict):
        raise RunnerError("runner.json.env 必须是对象")
    return value
