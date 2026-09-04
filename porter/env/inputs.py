"""inputs.py — T1 输入解析。

接收并校验启动参数，创建迁移工作区与 project.json（项目身份的真值源）。
全部为确定性检查，不使用 agent。
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
from pathlib import Path
from .. import log as _log

TOOL_ROOT = Path(__file__).resolve().parent.parent


class InputError(Exception):
    """输入校验失败；message 面向人阅读。"""


def validate(linux_driver: Path, target_os: Path,
             materials: list[Path] | None = None,
             intent_file: Path | None = None) -> dict:
    """校验三要素（+可选意图文件）并返回摘要。失败抛 InputError。"""
    # Linux 驱动源码目录
    if not linux_driver.is_dir():
        raise InputError(f"linux-driver 路径不存在或不是目录: {linux_driver}")
    c_files = [p for p in linux_driver.rglob("*.[ch]")]
    if not c_files:
        raise InputError(f"linux-driver 目录下没有任何 .c/.h 文件: {linux_driver}")
    has_build = any((linux_driver / n).exists() for n in ("Makefile", "Kbuild"))
    if not has_build:
        # Kbuild 缺失不一定致命（可能由父级 Makefile 构建），记警告由调用方携带
        pass

    # 目标 OS 源码树
    if not target_os.is_dir():
        raise InputError(f"target-os 路径不存在或不是目录: {target_os}")
    probe_write = target_os / ".porter_write_probe"
    try:
        probe_write.write_text("probe", encoding="utf-8")
        probe_write.unlink()
    except OSError as e:
        raise InputError(f"target-os 树不可写: {target_os} ({e})")

    # 资料束（可为空：仅凭源码树提取）
    materials = materials or []
    for m in materials:
        if not m.exists():
            raise InputError(f"material 路径不存在: {m}")

    # 迁移意图文件（可选：范围声明层的用户输入，拷贝入工作区 goals.md，
    # 由 P1-strategy 消费——见 tool-p1-adaptation-plan 范围声明层设计）
    if intent_file is not None:
        if not intent_file.is_file():
            raise InputError(f"intent-file 路径不存在或不是文件: {intent_file}")
        if intent_file.stat().st_size == 0:
            raise InputError(f"intent-file 为空文件: {intent_file}")

    return {
        "c_file_count": len(c_files),
        "has_kbuild": has_build,
        "materials": [str(m.resolve()) for m in materials],
        "intent_file": intent_file.name if intent_file is not None else None,
    }


def target_os_baseline(target_os: Path) -> dict:
    """记录目标树 VCS 基线（不改动用户仓库，只记录）。

    P7 用 git diff <baseline_commit> 提取本次迁移的全部改动。
    工作区不干净时如实记录，不阻塞（迁移本身也会让树变脏）。
    """
    info = {"is_git": False}
    git_dir = target_os / ".git"
    if git_dir.exists():
        import subprocess
        def _git(*args: str) -> str:
            r = subprocess.run(["git", *args], cwd=str(target_os),
                               capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else ""
        head = _git("rev-parse", "HEAD")
        branch = _git("branch", "--show-current")
        status = _git("status", "--short")
        info = {
            "is_git": bool(head),
            "baseline_commit": head,
            "branch": branch,
            "dirty_files": len(status.splitlines()) if status else 0,
        }
    return info


def init_workspace(output_dir: Path, linux_driver: Path, target_os: Path,
                   materials: list[Path],
                   intent_file: Path | None = None) -> Path:
    """创建迁移工作区并写入 project.json。幂等：目录已存在则拒绝。

    在 output_dir 下创建 P0/ 子目录（含 logs/、reports/）。
    project.json 写在 output_dir 根（跨阶段共享）。
    intent_file 给出时拷贝为 <ws>/goals.md 并记录（防外部漂移，
    随工作区 git 入库，类比 answers.md）。
    """
    ws = output_dir
    if ws.exists() and any(ws.iterdir()):
        raise InputError(f"工作区已存在且非空: {ws}（如需重跑请删除或换 --output-dir）")
    ws.mkdir(parents=True, exist_ok=True)
    # P0 阶段子目录
    (ws / "P0" / "logs").mkdir(parents=True, exist_ok=True)
    (ws / "P0" / "reports").mkdir(parents=True, exist_ok=True)

    summary = validate(linux_driver, target_os, materials, intent_file)
    project = {
        "name": ws.name,               # 从 output_dir basename 推断
        "created": _dt.datetime.now().isoformat(timespec="seconds"),
        "tool_version": "0.1.0",
        # 身份
        "linux_driver": str(linux_driver.resolve()),
        "target_os": str(target_os.resolve()),
        "materials": [str(m.resolve()) for m in materials],
        "category": None,               # T2 回填
        "category_confidence": None,
        "target_os_baseline": target_os_baseline(target_os),
    }
    if intent_file is not None:
        shutil.copyfile(intent_file, ws / "goals.md")
        project["intent_file"] = "goals.md"
        project["intent_source"] = str(intent_file.resolve())
    # 真值源用 JSON（工具链零依赖；结构化真值源 + markdown 视图的原则）
    (ws / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    _log.console_line(f"[porter] T1: workspace {ws}")
    _log.console_line(f"[porter] T1: 输入校验通过 {summary}")
    return ws


def backfill_intent(ws: Path, proj_path: Path, intent_file: Path) -> None:
    """resume 路径的 --intent-file 处理（幂等）。

    project.json 无 intent_file 记录 → 校验+拷贝 goals.md+记录；
    有记录：goals.md 缺失 → 从传入文件恢复；内容一致 → 跳过；
    不一致 → 警告不覆盖（更换意图请手工编辑 goals.md 后重跑）。
    """
    if not intent_file.is_file() or intent_file.stat().st_size == 0:
        raise InputError(f"intent-file 不存在或为空: {intent_file}")
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    dest = ws / "goals.md"
    if not proj.get("intent_file"):
        shutil.copyfile(intent_file, dest)
        proj["intent_file"] = "goals.md"
        proj["intent_source"] = str(intent_file.resolve())
        proj_path.write_text(
            json.dumps(proj, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        _log.console_line(f"[porter] T1: intent 已补拷 → {dest}")
        return
    if not dest.exists():
        shutil.copyfile(intent_file, dest)
        _log.console_line(f"[porter] T1: goals.md 缺失——已从 {intent_file} 恢复")
        return
    if dest.read_bytes() == intent_file.read_bytes():
        _log.console_line("[porter] T1: goals.md 与传入文件一致——跳过")
        return
    _log.console_line(
        "[porter] T1: ⚠️ 工作区已有 goals.md 且与传入文件不同——不覆盖"
        "（如需更换意图请手工编辑 goals.md 后重跑）")
