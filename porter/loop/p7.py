"""p7.py — P7 终态报告聚合 + 上游补丁台账（提案定稿相位）。

模式：
  聚合（默认）    汇总 P0→P6 全产物 + git baseline diff + 驱动 crate 统计
                 + platform_patches 台账 → P7/reports/final_report.json/.md
                 （数据驱动骨架，"结论与去向"节留给人工撰写区）。
  --patch-register GAP   登记新补丁（status=proposed，提案文档指针
                         P7/reports/patches/<gap>.md，文档本体人工撰写）。
  --patch-status GAP --to STATUS [--doc PATH] [--note TEXT]
                         补丁状态流转（planned|proposed|closed）。

baseline diff 语义：`git diff --name-status <commit>`（含工作区未提交修改）
∪ `git ls-files --others --exclude-standard`（未跟踪新文件，status="A"），
分组：driver-crate（kernel/core/comps/<driver>）/ workspace-wiring（其余
流水线接线面）/ other。

返回：0 成功 / 1 失败 / 2 前置缺失。
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

PATCH_STATUSES = {"planned", "proposed", "closed"}

# 工作区接线面（P2 骨架 + 会话接线产生的已知修改；driver crate 之外）
_WIRING_FILES = {
    "Cargo.toml", "Cargo.lock", "Components.toml",
    "kernel/core/Cargo.toml", "kernel/core/src/driver/mod.rs",
    "kernel/core/src/net/iface/init.rs",
    ".devcontainer/devcontainer.json", "tools/qemu_args.sh",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------- platform_patches 台账 ----------

def patches_path(ws: Path) -> Path:
    return ws / "platform_patches.json"


def load_patches(ws: Path) -> list[dict]:
    doc = _load(patches_path(ws)) or {"patches": []}
    return doc.get("patches") or []


def _save_patches(ws: Path, patches: list[dict]) -> None:
    patches_path(ws).write_text(
        json.dumps({"patches": patches}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def register_patch(ws: Path, gap: str, title: str, rationale: str = "",
                   doc: str | None = None) -> dict:
    patches = load_patches(ws)
    if any(p["gap"] == gap for p in patches):
        raise ValueError(f"补丁已存在: {gap}")
    entry = {"gap": gap, "module": "P6", "status": "proposed",
             "strategy": "proposal",
             "title": title,
             "instruction": rationale,
             "doc": doc or f"P7/reports/patches/{gap}.md",
             "evidence": "",
             "registered": _now()}
    patches.append(entry)
    _save_patches(ws, patches)
    try:                        # 类 2 钩子：平台补丁提案理由 → 候选
        from ..bootstrap import candidates as _cand
        if (rationale or "").strip():
            _cand.record_candidate(
                ws, hook="patch-register", ref=gap,
                draft=f"平台缺口 {gap}（{title}）：{rationale}",
                evidence=[entry["doc"], "platform_patches.json"],
                suggested="pitfalls")
    except Exception:
        pass
    return entry


def set_patch_status(ws: Path, gap: str, status: str, doc: str | None = None,
                     note: str = "") -> dict:
    if status not in PATCH_STATUSES:
        raise ValueError(f"非法状态: {status}（须 {sorted(PATCH_STATUSES)}）")
    patches = load_patches(ws)
    entry = next((p for p in patches if p["gap"] == gap), None)
    if entry is None:
        raise ValueError(f"补丁不存在: {gap}")
    entry["status"] = status
    if doc:
        entry["doc"] = doc
    if note:
        entry["closed_note"] = note
        entry["closed_time"] = _now()
    _save_patches(ws, patches)
    return entry


# ---------- git baseline diff ----------

def _git_lines(target_os: Path, args: list[str]) -> list[str]:
    """子进程 git（tests 可打桩）。失败返回空表（调用方自会记 degraded）。"""
    try:
        out = subprocess.run(["git", "-C", str(target_os), *args],
                             capture_output=True, text=True, timeout=60)
        return (out.stdout or "").splitlines() if out.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        return []


def baseline_diff(target_os: Path, baseline: str, driver: str
                  ) -> dict:
    """baseline commit → 工作区 的变更清单（含未跟踪新文件）。"""
    crate_prefix = f"kernel/core/comps/{driver}/"
    tracked = _git_lines(target_os, ["diff", "--name-status", baseline])
    untracked = ["A\t" + ln for ln in
                 _git_lines(target_os, ["ls-files", "--others",
                                        "--exclude-standard"])]
    files: list[dict] = []
    for ln in [*tracked, *untracked]:
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0][:1], parts[-1]
        if (path.startswith(("target/", "target2/"))
                or path.endswith((".log", ".pcap"))):
            continue
        group = ("driver-crate" if path.startswith(crate_prefix)
                 else "workspace-wiring" if path in _WIRING_FILES
                 else "other")
        files.append({"path": path, "status": status, "group": group})
    groups: dict[str, int] = {}
    for f in files:
        groups[f["group"]] = groups.get(f["group"], 0) + 1
    return {"baseline": baseline, "files_total": len(files),
            "groups": groups, "files": files,
            "degraded": not files and not tracked}


# ---------- 统计面 ----------

def mapping_stats(ws: Path) -> dict:
    m = _load(ws / "P2" / "mapping.json") or {}
    entries = m.get("entries") or []
    by_verdict: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for e in entries:
        by_verdict[e.get("verdict", "?")] = by_verdict.get(
            e.get("verdict", "?"), 0) + 1
        by_origin[e.get("origin", "?")] = by_origin.get(
            e.get("origin", "?"), 0) + 1
        by_risk[e.get("risk", "?")] = by_risk.get(e.get("risk", "?"), 0) + 1
    return {"total": len(entries), "by_verdict": by_verdict,
            "by_origin": by_origin, "by_risk": by_risk,
            "redesigns": len(m.get("redesigns") or []),
            "wiring": len(m.get("wiring") or [])}


def crate_stats(target_os: Path, driver: str) -> dict:
    crate = target_os / "kernel" / "core" / "comps" / driver
    files = lines = ktests = 0
    if crate.is_dir():
        for rs in crate.rglob("*.rs"):
            files += 1
            try:
                text = rs.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines += text.count("\n")
            ktests += text.count("#[ktest]")
    return {"files": files, "lines": lines, "ktests": ktests}


def _l4_summary(ws: Path) -> dict:
    doc = _load(ws / "P6" / "reports" / "l4_criteria.json") or {}
    cs = doc.get("criteria") or []
    return {"status": doc.get("status"), "total": len(cs),
            "clear": sum(1 for c in cs if c.get("disposition") == "clear"),
            "park": sum(1 for c in cs if c.get("disposition") == "park"),
            "parked_ids": [c["id"] for c in cs
                           if c.get("disposition") == "park"]}


def _last_execution_verdict(ws: Path) -> dict | None:
    h = _load(ws / "P6" / "reports" / "health.json") or {}
    return h.get("verdict") if h.get("mode") == "execute" else None


# ---------- 聚合 ----------

def run_p7(ws: Path) -> int:
    proj = _load(ws / "project.json")
    if not proj:
        print("[porter] P7: 缺 project.json（先跑 p0）")
        return 2
    target_os = Path(proj["target_os"])
    driver = Path(proj["linux_driver"]).name
    (ws / "P7" / "reports").mkdir(parents=True, exist_ok=True)

    st = _load(ws / "loop_state.json") or {}
    mods = st.get("modules") or {}
    acceptance_pass = skipped = 0
    for m, v in mods.items():
        if v.get("skipped"):
            skipped += 1
        else:
            acc = (_load(ws / "P5" / m / "reports" / "acceptance.json")
                   or _load(ws / "P4" / m / "reports" / "acceptance.json")
                   or {})
            acceptance_pass += 1 if acc.get("pass") else 0

    deferred = _load(ws / "deferred.json") or {"entries": []}
    open_e = [e for e in deferred["entries"] if e["status"] == "open"]
    defects = (_load(ws / "defects.json") or {"defects": []})["defects"]
    patches = load_patches(ws)

    report = {
        "time": datetime.now().isoformat(), "workspace": str(ws),
        "driver": driver, "target_os": str(target_os),
        "pipeline": {"modules_total": len(mods),
                     "phase_done": sum(1 for v in mods.values()
                                       if v.get("phase") == "done"),
                     "acceptance_pass": acceptance_pass, "skipped": skipped},
        "crate": crate_stats(target_os, driver),
        "mapping": mapping_stats(ws),
        "l4": _l4_summary(ws),
        "p6_verdict": _last_execution_verdict(ws),
        "deferred": {"cleared": len(deferred["entries"]) - len(open_e),
                     "open": len(open_e),
                     "open_ids": [e["id"] for e in open_e]},
        "defects": {"fixed": sum(1 for d in defects if d["status"] == "fixed"),
                    "parked": sum(1 for d in defects
                                  if d["status"] == "parked"),
                    "parked_ids": [d["id"] for d in defects
                                   if d["status"] == "parked"]},
        "patches": {"total": len(patches),
                    "by_status": {s: [p["gap"] for p in patches
                                      if p["status"] == s]
                                  for s in sorted({p["status"]
                                                   for p in patches})}},
        "baseline": baseline_diff(
            target_os,
            ((proj.get("target_os_baseline") or {}).get("baseline_commit")
             or ""), driver),
    }
    rp = ws / "P7" / "reports"
    (rp / "final_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_md(ws, rp / "final_report.md", report)
    print(f"[porter] P7: 聚合完成 → {rp / 'final_report.json'}")
    print(f"[porter] P7: 模块 {report['pipeline']['phase_done']}"
          f"/{report['pipeline']['modules_total']} done（skip "
          f"{skipped}），crate {report['crate']['files']} 文件"
          f"/{report['crate']['lines']} 行/{report['crate']['ktests']} ktest，"
          f"映射 {report['mapping']['total']} 条，"
          f"baseline 变更 {report['baseline']['files_total']} 文件"
          f"（{report['baseline']['groups']}）")
    return 0


def _fmt_counts(d: dict) -> str:
    return " / ".join(f"{k} {v}" for k, v in sorted(d.items(),
                                                    key=lambda kv: -kv[1]))


def _write_md(ws: Path, path: Path, r: dict) -> None:
    p, c, m = r["pipeline"], r["crate"], r["mapping"]
    b = r["baseline"]
    v = r["p6_verdict"] or {}
    lines = [
        "# P7 终态报告（骨架——数据面机器生成）", "",
        f"- 工作区：{r['workspace']}", f"- 驱动：{r['driver']} @ "
        f"{r['target_os']}（baseline `{b['baseline'][:12]}`）",
        f"- 生成：{r['time']}", "",
        "## 流水线终态", "",
        f"- 模块：{p['phase_done']}/{p['modules_total']} done"
        f"（acceptance PASS {p['acceptance_pass']}，授权 skip "
        f"{p['skipped']}）",
        f"- 驱动 crate：{c['files']} 个 .rs / {c['lines']} 行 / "
        f"{c['ktests']} 个 #[ktest]",
        f"- 映射表：{m['total']} 条（{_fmt_counts(m['by_verdict'])}；换思路 "
        f"{m['redesigns']} + 接线 {m['wiring']}）",
        f"- P6 判定：{'ALL GREEN（泊车除外）' if v.get('all_green_except_parked') else '—（未见执行态 health）'}",
        f"- L4：{r['l4']['total']} 条 = clear {r['l4']['clear']} + "
        f"park {r['l4']['park']}", "",
        "## 账本", "",
        f"- deferred：cleared {r['deferred']['cleared']} / open "
        f"{r['deferred']['open']}（{', '.join(r['deferred']['open_ids'])
           or '—'}）",
        f"- defects：fixed {r['defects']['fixed']} / parked "
        f"{r['defects']['parked']}（{', '.join(r['defects']['parked_ids'])
           or '—'}）",
        f"- platform_patches：{r['patches']['total']} 条（"
        + "；".join(f"{s}: {', '.join(g) if isinstance(g, list) else g}"
                    for s, g in r["patches"]["by_status"].items())
        + ")", "",
        "## baseline diff", "",
        f"- 变更 {b['files_total']} 文件：{_fmt_counts(b['groups'])}", "",
        "| 组 | 文件 | 状态 |", "|---|---|---|",
    ]
    for f in b["files"]:
        lines.append(f"| {f['group']} | {f['path']} | {f['status']} |")
    lines += ["", "## 结论与去向（人工撰写区）", "",
              "<!-- P7-4 人工撰写：完整度清单（已迁/未迁/不适用）、泊车条目"
              "与翻回条件、补丁台账处置、复用建议 -->", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 入口 ----------

def run_p7_cli(ws: Path, patch_register: str | None = None,
               title: str = "", rationale: str = "",
               patch_status: str | None = None, status_to: str = "",
               doc: str | None = None, note: str = "") -> int:
    if patch_register:
        try:
            e = register_patch(ws, patch_register, title, rationale, doc)
            print(f"[porter] P7: 补丁登记 {e['gap']}（proposed，文档 "
                  f"{e['doc']}）")
            return 0
        except ValueError as ex:
            print(f"[porter] P7: {ex}")
            return 1
    if patch_status:
        try:
            e = set_patch_status(ws, patch_status, status_to, doc, note)
            print(f"[porter] P7: 补丁 {e['gap']} → {e['status']}")
            return 0
        except ValueError as ex:
            print(f"[porter] P7: {ex}")
            return 1
    return run_p7(ws)
