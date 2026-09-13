"""Workspace artifact discovery and the mutable phase location index."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from .workspace import write_json

ARTIFACTS = (
    "module-division.md",
    "module-division.json",
    "migration-plan.md",
    "migration-plan.json",
)
PREPARE_ARTIFACTS = ("migration-plan.md",)

_LEGACY = {
    "module-division.md": "module-divsion.md",
    "module-division.json": "module-divsion.json",
}

_SKIP_PARTS = {".git", "handoffs", "__pycache__"}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _usable(path: Path, ws: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0 and path.resolve().is_relative_to(ws)
    except OSError:
        return False


def _scan(ws: Path, name: str) -> Path | None:
    candidates = [ws / name]
    candidates.extend(sorted(ws.rglob(name)))
    for path in candidates:
        try:
            rel = path.relative_to(ws)
        except ValueError:
            continue
        if any(part in _SKIP_PARTS for part in rel.parts):
            continue
        if _usable(path, ws):
            return path.resolve()
    return None


def _state_paths(ws: Path, state_path: Path,
                 required: tuple[str, ...]) -> dict[str, Path]:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = value.get("artifacts") if isinstance(value, dict) else None
    if not isinstance(entries, dict):
        return {}
    found = {}
    for name in required:
        entry = entries.get(name)
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if not isinstance(raw, str):
            continue
        path = (ws / raw).resolve()
        if _usable(path, ws):
            found[name] = path
    return found


def write_state(ws: Path, paths: dict[str, Path], state_path: Path | None = None) -> Path:
    """Record artifact locations relative to the workspace."""
    state_path = state_path or ws / "state.json"
    current = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, json.JSONDecodeError):
            pass
    current["artifact_index_version"] = 1
    entries = current.get("artifacts") if isinstance(current.get("artifacts"), dict) else {}
    entries.update({
        name: {"path": str(path.resolve().relative_to(ws.resolve())),
               "sha256": _digest(path)}
        for name, path in paths.items() if name in ARTIFACTS and _usable(path, ws)
    })
    current["artifacts"] = entries
    current["artifacts_updated_at"] = time.time()
    write_json(state_path, current)
    return state_path


def canonicalize(ws: Path, paths: dict[str, Path]) -> dict[str, Path]:
    """Copy only legacy division spellings to adjacent canonical filenames."""
    result = dict(paths)
    for name, legacy in _LEGACY.items():
        path = result.get(name)
        if path is None or path.name != legacy:
            continue
        target = path.with_name(name)
        if not _usable(target, ws):
            shutil.copyfile(path, target)
        result[name] = target.resolve()
    return result


def locate(ws: Path, *, state_path: Path | None = None,
           required: tuple[str, ...] = ARTIFACTS,
           allow_legacy: bool = True, record: bool = True) -> dict[str, Path]:
    """Resolve all required files from state first, then recursively scan ws."""
    ws = Path(ws).resolve()
    state_path = state_path or ws / "state.json"
    indexed = _state_paths(ws, state_path, required)
    found = dict(indexed)
    for name in required:
        if name in found:
            continue
        found[name] = _scan(ws, name)
        if found[name] is None and allow_legacy and name in _LEGACY:
            found[name] = _scan(ws, _LEGACY[name])
    if any(found.get(name) is None for name in required):
        missing = ", ".join(name for name in required if found.get(name) is None)
        raise ValueError(f"workspace artifacts missing: {missing}")
    result = {name: found[name] for name in required}
    if record and indexed != result:
        write_state(ws, result, state_path)
    return result
