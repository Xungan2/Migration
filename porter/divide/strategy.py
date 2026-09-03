"""strategy.py — P1 拆分策略选择编排（skills/P1-strategy.md 的执行器）。

零合同设计（用户定稿）：策略是咨询性产物，无固定 schema、无机器路由——
agent 产出自由 Markdown 分析，落盘后**每次都由开发人员审阅**（放行与否
由人决定，novel_strategy 标记已删）。

编排器职责：
  1. 组装 prompt（SKILL + 任务数据 + 样例库 INDEX）
  2. 一次 agent 调用
  3. agent 的分析正文落盘 strategy.md（幂等：存在即复用）
  4. 样例库草稿 + 知识报告（见下）
  5. 打印人审入口提示

质量把关在两处（都不是这里）：人工审阅 + 下游 divide 的客观校验
（覆盖 diff / 依赖无环 / 粒度护栏）。

样例库（本模块是唯一管理点；规范见
knowledge/base/splits/strategies/README.md）：
- 三分区：knowledge/base/splits/strategies（工具随附，任意目标 OS
  可用）、<本次知识库目录>/splits/strategies（已沉淀，p0 --kb 指定）、
  knowledge/temp/splits/strategies（草稿）；条目 = 分区内除 README.md
  外的 *.md（每条 = 某工作区 strategy.md 输出产物原样）
- INDEX.json（裸数组）为条目目录，注入 strategy prompt
- run_strategy 产出 strategy.md → 自动草稿入 temp（价值判定：与已沉淀
  分区完全一致者不写）→ 写工作区报告 reports/P1-knowledge.md
- 晋升（沉淀）：p1-promote 命令（temp → 本次知识库目录），P1 完成后
  由开发者自行决定执行
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from ..bootstrap import kb
from ..common import agent
from .. import log as _log

# ---------- 样例库 ----------

def sample_partitions(kb_dir: Path | None,
                      ws: Path | None = None) -> list[tuple[str, Path]]:
    """样例库分区表（标签, 目录）。已沉淀 = base ∪ 知识库目录。"""
    parts: list[tuple[str, Path]] = [
        ("工具随附样例（base，任意目标 OS 可用）",
         kb.domain_base("splits")),
    ]
    if kb_dir is not None:
        parts.append(("已沉淀样例（本次知识库目录）",
                      kb.domain_kb("splits", kb_dir)))
    parts.append(("草稿样例（未经人审，与已沉淀冲突时以已沉淀为准）",
                  kb.domain_temp("splits", ws=ws)))
    return parts


_EMPTY_NOTE = "（样例库当前为空——没有可参考的样例）"


def _list_entries(d: Path) -> list[Path]:
    """条目判定（唯一规则）：分区内除 README.md 外的 *.md。"""
    if not d.is_dir():
        return []
    return sorted(f for f in d.glob("*.md") if f.name != "README.md")


def _load_index(d: Path) -> list | None:
    """解析 INDEX.json（裸数组）；缺失/损坏返回 None。"""
    return kb.load_index(d)


def _save_index(d: Path, entries: list) -> None:
    kb.save_index(d, entries)


def _curated_partitions(kb_dir: Path | None) -> list[Path]:
    """已沉淀分区目录（base + 知识库目录）。"""
    return [d for _label, d in sample_partitions(kb_dir)
            if "草稿" not in _label]


def _merged_curated_index(kb_dir: Path | None) -> list:
    idx: list = []
    for d in _curated_partitions(kb_dir):
        part = _load_index(d)
        if part is None:
            if (d / "INDEX.json").exists():
                _log.console_line(f"[porter] P1S: ⚠️ {d / 'INDEX.json'} 损坏，按空库判定")
            continue
        idx.extend(part)
    return idx


def _build_samples_injection(ws: Path) -> str:
    """样例库注入块：导读 + 各非空分区的目录路径与 INDEX 内容。"""
    sections = []
    for label, d in sample_partitions(kb.kb_dir_for(ws), ws=ws):
        entries = _list_entries(d)
        idx = _load_index(d)
        if not entries:
            if idx:
                _log.console_line(f"[porter] P1S: ⚠️ {label} INDEX 有条目登记但目录无 *.md")
            continue
        sec = [f"### {label}", f"样例目录: {d.resolve()}"]
        if idx is None:
            _log.console_line(f"[porter] P1S: ⚠️ {d / 'INDEX.json'} 缺失或损坏，"
                  f"退化为条目文件清单")
            sec += [f"- {f.name}" for f in entries]
        else:
            sec.append(json.dumps(idx, ensure_ascii=False, indent=1))
            listed = {e.get("entry_file") for e in idx if isinstance(e, dict)}
            actual = {f.name for f in entries}
            ghost = sorted(listed - actual)
            unlisted = sorted(actual - listed)
            if ghost or unlisted:
                _log.console_line(f"[porter] P1S: ⚠️ {label} INDEX 与条目不一致"
                      f"（幽灵登记: {ghost or '无'}；"
                      f"未登记: {unlisted or '无'}）")
        sections.append("\n".join(sec))
    if not sections:
        return _EMPTY_NOTE
    head = ("INDEX（JSON）每条目说明该样例拆的是哪个驱动（名字、Linux "
            "目录/文件）；对照你当前的驱动判断相似度，命中后按条目文件"
            "（entry_file）读全文参考，未命中不必读。")
    return head + "\n\n" + "\n\n".join(sections)


def _classify_sample(driver_name: str, files: set,
                     knowledge_index: list) -> tuple[str, str | None]:
    """对沉淀分区判价值：返回 (exact|related|none, 匹配条目文件)。

    exact  = 同名驱动且文件集相同（不比对绝对路径/内容）
    related = 同名驱动但文件集不同
    none   = 无同名驱动
    """
    related_file = None
    for e in knowledge_index:
        if not isinstance(e, dict) or e.get("driver_name") != driver_name:
            continue
        if set(e.get("linux_files") or []) == files:
            return "exact", e.get("entry_file")
        if related_file is None:
            related_file = e.get("entry_file")
    if related_file is not None:
        return "related", related_file
    return "none", None


def _resolve_dest_name(d: Path, index: list, driver_name: str,
                       files: set) -> str | None:
    """为目标分区定条目文件名（同名碰撞语义：改名保留不同构成）。

    无碰撞 → 裸名 `<驱动名>.md`；碰撞时比对文件集：相同 = 真重复 →
    None；不同（或碰撞文件无 INDEX 元数据可比）→ 自动改名
    `<驱动名>__2.md`、`__3.md`… 保留。
    """
    k, name = 1, f"{driver_name}.md"
    while True:
        hit = next((e for e in index
                    if isinstance(e, dict) and e.get("entry_file") == name),
                   None)
        if hit is None and not (d / name).exists():
            return name
        if hit is not None and set(hit.get("linux_files") or []) == files:
            return None
        k += 1
        name = f"{driver_name}__{k}.md"


def _draft_to_temp(ws: Path, proj: dict, driver_root: Path,
                   strategy_path: Path) -> dict:
    """样例草稿入 temp 分区（幂等）。返回 {driver, linux_dir,
    linux_files, status, entry_file, value}。"""
    driver = Path(proj["linux_driver"]).name
    files = sorted(p.name for p in driver_root.iterdir()
                   if p.is_file() and p.suffix in (".c", ".h"))
    res = {"driver": driver, "linux_dir": proj.get("linux_driver", ""),
           "linux_files": files, "entry_file": None}

    kidx = _merged_curated_index(kb.kb_dir_of(proj))
    kind, matched = _classify_sample(driver, set(files), kidx)
    if kind == "exact":
        res.update(status="未写入",
                   value=f"已沉淀分区已有完全一致样例（{matched}），不重复草稿")
        return res

    tdir = kb.domain_temp("splits", ws=ws)
    tidx = _load_index(tdir) or []
    name = _resolve_dest_name(tdir, tidx, driver, set(files))
    if name is None:
        res.update(status="未写入",
                   value="temp 已有完全一致草稿（同名+同文件集），未重复加入")
        return res

    tdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(strategy_path, tdir / name)
    tidx.append({"entry_file": name, "driver_name": driver,
                 "linux_dir": res["linux_dir"], "linux_files": files,
                 "hits": 0})
    _save_index(tdir, tidx)
    if kind == "related":
        value = (f"新而有价值：已沉淀分区有相关样例（{matched}），"
                 f"但文件构成不同")
    else:
        value = "新而有价值：已沉淀分区无相关样例（全新）"
    if name != f"{driver}.md":
        value += f"；temp 已有同名草稿（构成不同），改名 {name} 保留"
    res.update(status="已写入 temp", entry_file=name, value=value)
    return res


def _write_knowledge_report(ws: Path, proj: dict, res: dict) -> Path:
    """写工作区知识报告 P1/reports/P1-knowledge.md（未来可并入统一
    P1 报告）。"""
    p1 = ws / "P1"
    rpt = p1 / "reports" / "P1-knowledge.md"
    rpt.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# P1 知识样例报告", "",
        f"- 工作区: {ws.name}",
        f"- 驱动: {res['driver']}",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 本步样例草稿与价值判定", "",
        "| 条目文件 | 驱动名 | Linux 目录 | 文件数 | 状态 | 价值判定 |",
        "|---|---|---|---|---|---|",
        f"| {res.get('entry_file') or '—'} | {res['driver']} "
        f"| `{res['linux_dir']}` "
        f"| {len(res['linux_files'])} | {res['status']} | {res['value']} |",
        "",
        "## 判定标准", "",
        "- 沉淀分区已有完全一致样例（驱动名相同 且 文件集相同）",
        "  → 重复，不写入 temp",
        "- 沉淀分区相关但非完全一致（同驱动名、文件构成不同）",
        "  → 新而有价值",
        "- 沉淀分区无相关样例 → 新而有价值（全新）",
        "- 不比对绝对路径与文件内容",
        "",
        "## 沉淀", "",
        "沉淀不自动。**P1 整体完成后**由开发者决定是否晋升 temp 中的草稿：",
        "",
        f"    python3 porter/main.py p1-promote --output-dir <工作区> "
        f"--driver {res.get('entry_file') or res['driver']}",
        "",
        "规范详见 `knowledge/base/splits/strategies/README.md`。",
    ]
    rpt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rpt


def promote_sample(driver: str, kb_dir: Path) -> int:
    """p1-promote：样例草稿晋升 temp → 本次知识库目录。返回 0=成功。

    --driver 可给驱动名或条目文件名；按驱动名命中多条（同名不同构成）
    时列候选要求指定条目文件名。目标分区同名碰撞：同文件集=真重复→
    拒绝；构成不同→改名并入（保留）。与 base 完全一致也拒绝（工具已随附）。
    """
    tdir = kb.domain_temp("splits", kb_dir=kb_dir)
    tidx = _load_index(tdir) or []
    arg = driver[:-3] if driver.endswith(".md") else driver
    matches = [e for e in tidx if isinstance(e, dict) and
               (e.get("driver_name") == arg
                or e.get("entry_file") in (arg, f"{arg}.md"))]
    if not matches:
        _log.console_line(f"[porter] p1-promote: temp 分区无匹配 {driver!r} 的草稿")
        return 1
    if len(matches) > 1:
        _log.console_line(f"[porter] p1-promote: temp 分区有 {len(matches)} 个匹配 "
              f"{driver!r} 的草稿（同名不同构成），请指定条目文件名晋升：")
        for e in matches:
            print(f"  - {e.get('entry_file')}"
                  f"（{len(e.get('linux_files') or [])} 个文件）")
        return 1
    entry = matches[0]

    src = tdir / entry.get("entry_file", "")
    if not src.exists():
        _log.console_line(f"[porter] p1-promote: 草稿文件缺失 {src}"
              f"（temp INDEX 与磁盘不一致）")
        return 1
    kdir = kb.domain_kb("splits", kb_dir)
    # 真重复判定含 base（工具已随附的构成不再入库）
    merged = _merged_curated_index(kb_dir)
    kind, _matched = _classify_sample(entry.get("driver_name", arg),
                                      set(entry.get("linux_files") or []),
                                      merged)
    kidx = _load_index(kdir) or []
    name = None if kind == "exact" else _resolve_dest_name(
        kdir, kidx, entry.get("driver_name", arg),
        set(entry.get("linux_files") or []))
    if name is None:
        _log.console_line(f"[porter] p1-promote: 已沉淀分区已有完全一致样例"
              f"（同名+同文件集），拒绝重复晋升")
        return 1
    kdir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(kdir / name))
    _save_index(tdir, [e for e in tidx if e is not entry])
    entry["entry_file"] = name
    # 晋升折叠：运行时咨询热度从旁车并回 INDEX 行
    entry["hits"] = int(entry.get("hits", 0) or 0) + int(
        kb.fold_sidecar_hits(kb_dir, "splits", [name]).get(name, 0))
    kidx.append(entry)
    _save_index(kdir, kidx)
    _log.console_line(f"[porter] p1-promote: {entry.get('driver_name')} 已晋升"
          f"（{src.name} → {kdir / name}）")
    return 0


# ---------- P1-strategy 编排 ----------

def _task_data(ws: Path, proj: dict, driver_root: Path) -> str:
    cats = proj.get("category") or []
    mats = proj.get("materials") or []
    lines = [
        "## 任务数据", "",
        f"- 驱动源码路径：`{driver_root.resolve()}`"
        "（其所在的 Linux 源码仓全部代码可读，不限于本驱动目录）",
        f"- 项目类别标签（参考）：{cats or '未知'}",
    ]
    runner_path = ws / "runner.json"
    if runner_path.exists():
        r = json.loads(runner_path.read_text(encoding="utf-8"))
        ex = (r.get("inject_device") or {}).get("example_args") or {}
        if ex:
            lines.append("- runner 设备注入参数（迁移环境证据之一）：")
            lines += [f"  - {k}: {v}" for k, v in ex.items()]
    if mats:
        lines.append("- 资料路径（迁移环境事实来源，自己去读）：")
        lines += [f"  - {m}" for m in mats]
    lines += ["", "## 拆分样例库（参考思路，不可照搬）", "",
              _build_samples_injection(ws)]
    return "\n".join(lines)


def run_strategy(ws: Path, driver_root: Path) -> int:
    """返回 0=策略已就绪（待人工审阅）；1=agent 调用失败。"""
    proj_path = ws / "project.json"
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    p1 = ws / "P1"
    (p1 / "logs").mkdir(parents=True, exist_ok=True)
    (p1 / "reports").mkdir(exist_ok=True)
    out_path = p1 / "strategy.md"

    if out_path.exists():
        _log.console_line(f"[porter] P1S: 复用 {out_path}（如需重做请删除该文件）")
    else:
        skill = agent.load_skill("P1-strategy")
        prompt = (f"{skill}\n\n---\n\n"
                  f"{_task_data(ws, proj, driver_root)}\n\n"
                  f"请按 SKILL 的「产出要求」完成分析。你的分析全文将被保存为"
                  f" strategy.md 呈给开发人员审阅——直接输出 Markdown 正文，"
                  f"不要输出 JSON。")

        rc, out = agent.run_agent(prompt, workdir=p1,
                                  log_stem=str(p1 / "logs" / "P1S_R1"),
                                  timeout_sec=1800)
        if rc != 0:
            _log.console_line(f"[porter] P1S: agent 调用失败（rc={rc}，见 P1/logs/P1S_R1.log）")
            return 1

        # 提取正文：去掉 ANSI 色码；若 agent 用 ```markdown 包裹则剥壳
        import re
        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", out).strip()
        m = re.search(r"```markdown\s*(.*?)```", text, re.DOTALL)
        if m and len(m.group(1)) > len(text) // 2:
            text = m.group(1).strip()

        # 截断过程回显：正文结束后 opencode 会追加回答签名（"> build · <model>"）
        # 与过程记录（"$ cmd" shell 回显、"→ Read ..." 工具调用）。截断点取
        # 最早出现的标记行。
        sig = re.search(r"^> .+·.+$", text, re.MULTILINE)      # 回答签名行
        shell = re.search(r"^\$ .+$", text, re.MULTILINE)      # shell 提示符行
        toolrec = re.search(r"^→ .+$", text, re.MULTILINE)     # 工具调用记录行
        cut_candidates = [m_.start() for m_ in (sig, shell, toolrec) if m_]
        if cut_candidates:
            text = text[:min(cut_candidates)].rstrip()

        # agent 也可能在运行期间自行写入 strategy.md（而非把全文作为
        # 最终回答）：若其写入的内容更长，以文件内容为准（更完整干净）
        if out_path.exists():
            agent_written = out_path.read_text(encoding="utf-8").strip()
            if len(agent_written) > len(text):
                _log.console_line(f"[porter] P1S: agent 已自行写入 strategy.md"
                      f"（{len(agent_written)} 字符），采用文件内容")
                text = agent_written

        if len(text) < 400:
            _log.console_line(f"[porter] P1S: ⚠️ agent 输出过短（{len(text)} 字符），"
                  f"疑似异常——请检查 P1/logs/P1S_R1.log 后重跑")
            return 1

        out_path.write_text(text, encoding="utf-8")
        _log.console_line(f"[porter] P1S: strategy.md 已生成（{len(text)} 字符）")

    # 样例库：草稿 + 知识报告（生成/复用两路径都执行；幂等）。
    # 失败仅警告，不阻断主产物。
    try:
        res = _draft_to_temp(ws, proj, driver_root, out_path)
        rpt = _write_knowledge_report(ws, proj, res)
        _log.console_line(f"[porter] P1S: 样例草稿 {res['status']}（{res['driver']}）；"
              f"价值判定见 {rpt}")
    except (OSError, KeyError, TypeError) as e:
        _log.console_line(f"[porter] P1S: ⚠️ 样例草稿/报告失败（不影响策略）：{e}")

    _log.console_line(f"[porter] P1S: 待人工审阅：{out_path} —— 放行后即可运行 p1-divide；"
          f"不满意可删除该文件重跑，或直接人工编辑")
    return 0
