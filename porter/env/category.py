"""category.py — T2 类别识别（agent 小任务）。

组装 prompt = SKILL(P0-category-identify) + 驱动路径，调 opencode，
解析 JSON 输出。设计要点（已定稿）：
- 类别是"选模板的开关"，是加速器不是承重墙——识别失败工具降级而非停摆
- --category 人工指定优先于 agent 判断
- 置信度 low / 无类别 → 回落通用模板并警告（仅"根本不是内核驱动"才硬停）
"""

from __future__ import annotations

import json
from pathlib import Path

from ..common import agent


def identify_category(linux_driver: Path, workdir: Path,
                      override: str | None = None) -> dict:
    """返回类别识别结果 dict（categories/confidence/evidence/...）。

    override 非空时跳过 agent，直接采用人工指定。
    workdir = output_dir（工作区根）；日志写入 P0/logs/。
    """
    if override:
        cats = [c.strip() for c in override.split(",") if c.strip()]
        result = {
            "categories": cats,
            "confidence": "manual",
            "evidence": [],
            "subsystems": [],
            "notes": "人工指定（--category）",
        }
        print(f"[porter] T2: 类别={cats}（人工指定，跳过 agent）")
        return result

    p0 = workdir / "P0"
    (p0 / "logs").mkdir(parents=True, exist_ok=True)
    skill = agent.load_skill("P0-category-identify")
    prompt = (
        f"{skill}\n\n---\n\n"
        f"## 任务数据\n\n待识别的 Linux 驱动源码目录："
        f"`{linux_driver.resolve()}`\n\n"
        f"请按 SKILL 检查并只输出一个 JSON 块。"
    )
    rc, out = agent.run_agent(prompt, workdir=p0,
                              log_stem=str(p0 / "logs" / "T2_category"))
    parsed = agent.extract_json(out) if rc == 0 else None

    if parsed is None or not parsed.get("categories"):
        # 无法解析或空类别：区分"不是驱动"与"解析失败"
        if parsed is not None and parsed.get("confidence") == "none":
            raise SystemExit(
                "[porter] T2: 未发现内核驱动注册特征——输入可能不是内核驱动，"
                "请检查 --linux-driver 路径。证据见 P0/logs/T2_category.log")
        raise SystemExit(
            "[porter] T2: 类别识别失败（输出无法解析）。"
            "请重跑，或用 --category 人工指定。日志: P0/logs/T2_category.log")

    conf = parsed.get("confidence", "low")
    if conf == "low":
        print(f"[porter] T2: ⚠️ 置信度 low——{parsed.get('notes', '')}"
              f"（建议 --category 人工指定；本次按结果继续，后续模板为并集/通用版）")
    print(f"[porter] T2: categories={parsed['categories']} "
          f"confidence={conf} subsystems={parsed.get('subsystems')}")
    return parsed


def write_result(ws: Path, result: dict) -> None:
    proj_path = ws / "project.json"
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    proj["category"] = result.get("categories", [])
    proj["category_confidence"] = result.get("confidence")
    proj["category_evidence"] = result.get("evidence", [])
    proj["subsystems"] = result.get("subsystems", [])
    proj_path.write_text(json.dumps(proj, ensure_ascii=False, indent=2),
                         encoding="utf-8")
