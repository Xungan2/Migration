"""runbook.py — runbook 域收成（runner.json → knowledge/temp/runbook）。

粒度：一个操作主题一个文件（build / boot / unit_test；notes 坑史随
所属主题入文，人工晋升时可蒸馏成独立坑条目或转 pitfalls）。
命名空间：temp 跨迁移共享 → `temp/runbook/<目标OS名>/<主题>.md`。

收成点（固定知识，两处）：
  p0 末（T5 门禁通过后——runner 定型）；
  P5 unit_test backfill 后（补探/烟测验证完成时）。

消费点：T3 R1 prompt 注入 runbook 目录（INDEX 级"起点假设"——
环境会漂移，命令与特征仍须本轮实测复核）；路由 env/infra 类关口
（S6 接入）。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import kb
from .. import log as _log


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _kv(label: str, value) -> str:
    return f"- {label}：{value if value not in (None, '') else '—'}"


def _topic_md(target: str, topic: str, sec: dict) -> str:
    lines = [f"# {topic} —— {target} 操作手册（runbook）", ""]
    if sec.get("cmd"):
        lines += ["命令：", "```bash", str(sec["cmd"]), "```"]
    extra = {
        "build": ["timeout_full_sec", "timeout_inc_sec"],
        "boot": ["timeout_sec", "log_file", "log_is_stdout",
                 "success_pattern", "panic_pattern"],
        "unit_test": ["mechanism", "timeout_sec", "success_pattern",
                      "fail_pattern", "scope_hint", "verified",
                      "discovered_by"],
    }.get(topic, [])
    for k in extra:
        if k in sec:
            lines.append(_kv(k, sec[k]))
    if topic == "boot":
        inj = sec.get("inject_device") or {}
        if inj:
            lines += ["", "设备注入机制：",
                      _kv("mechanism", inj.get("mechanism")),
                      _kv("env", inj.get("env")),
                      _kv("cmd_suffix", inj.get("cmd_suffix")),
                      _kv("example_args", json.dumps(
                          inj.get("example_args") or {},
                          ensure_ascii=False))]
    if topic == "build" and sec.get("success_pattern"):
        lines.append(_kv("成功特征", sec["success_pattern"]))
    if sec.get("notes"):
        lines += ["", "## 坑史（notes，人工可蒸馏为 pitfalls）", "",
                  str(sec["notes"])]
    return "\n".join(lines) + "\n"


def draft_runbook(ws: Path) -> int:
    """收成：runner.json 三节 → temp/runbook/<目标>/<主题>.md（幂等重建）。

    返回 0（runner 缺失 rc 1 跳过）。
    """
    runner = _load(ws / "runner.json")
    proj = _load(ws / "project.json")
    if not runner or not proj:
        return 1
    target = Path(proj["target_os"]).name
    rdir = kb.domain_temp("runbook", ws=ws)
    ns_dir = rdir / target
    ns_dir.mkdir(parents=True, exist_ok=True)

    descs = {
        "build": lambda s: (f"{target} 构建命令"
                            + (f"（成功特征 {s.get('success_pattern')!r}）"
                               if s.get("success_pattern") else "")),
        "boot": lambda s: (f"{target} 启动命令与双信号判定"
                           + (f"，设备注入 {list((s.get('inject_device') or {}).get('example_args') or {})}"
                              if (s.get("inject_device") or {})
                              .get("example_args") else "")),
        "unit_test": lambda s: (f"{target} 内核态单测（{s.get('mechanism', '?')}）"
                                + ("，含坑史 notes" if s.get("notes") else "")),
    }
    rows: list[dict] = []
    for topic in ("build", "boot", "unit_test"):
        sec = runner.get(topic)
        if not isinstance(sec, dict) or not sec:
            continue
        fname = f"{target}/{topic}.md"
        (rdir / fname).write_text(_topic_md(target, topic, sec),
                                  encoding="utf-8")
        rows.append({"file": fname,
                     "desc": descs[topic](sec), "hits": 0})

    idx = kb.load_index(rdir) or []
    kept_hits = {e["file"]: int(e.get("hits", 0) or 0)
                 for e in idx if isinstance(e, dict) and e.get("file")}
    idx = [e for e in idx if not (isinstance(e, dict)
                                  and str(e.get("file", ""))
                                  .startswith(f"{target}/"))]
    for r in rows:
        r["hits"] = kept_hits.get(r["file"], 0)
    idx.extend(rows)
    kb.save_index(rdir, idx)
    if rows:
        _log.console_line(f"[porter] runbook 知识: 草稿已刷新 knowledge/temp/runbook/"
              f"{target}/（{len(rows)} 主题）")
    return 0
