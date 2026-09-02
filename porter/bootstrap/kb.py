"""kb.py — 知识库骨架：目录模型 + 域注册表 + 薄 INDEX + 通用晋升。

目录模型（三区，均在工具仓 knowledge/ 下）：
  knowledge/base/    工具随附的一般知识（任意目标 OS 可用；git 跟踪）
  knowledge/temp/    草稿区（骨架 README 跟踪，内容全部 gitignore）——
                     agent 可写的未审分区；与已审内容冲突时以后者为准
  knowledge/<name>/  一次迁移（或用户自维语料）的知识库目录——p0 时由
                     用户显式新建（copy base 或空）或指定既有目录；
                     project.json["kb_dir"] 记录其名字

  本次迁移的知识库 = knowledge/temp/ ∪ knowledge/<name>/
  信任分层 = 物理分区：temp 未审；<name>/ 与 base/ 已审。

域注册表（"分类 = 子目录"的落地）。加一个域 = 此表登记一行 +
skills/kb-guide.md 补一节 + 调用点对照表补一行——单点改动：
  maps     API 映射表（一驱动@目标一张整表）
  gaps     gap 处置记录（一个 API 一个文件；文件名即 API 名）
  runbook  目标 OS 操作手册（一主题/一坑一文件）
  splits   拆分策略样例（splits/strategies/，一驱动一文件）
  pitfalls 踩坑记录（一坑一文件；方法教训亦入此域，标签区分）

薄 INDEX（全域统一的条目目录，供 agent 检索）：
  [{"file": "<条目文件名>", "desc": "<一句话内容描述>",
    "hits": <被咨询次数>}]
  旧域（maps/splits）迁移完成前允许在行内携带域专属扩展字段；
  新域（gaps/runbook/candidates 等）一律只用薄格式。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from ..common.agent import TOOL_ROOT

KB_ROOT = TOOL_ROOT / "knowledge"
BASE_DIR = KB_ROOT / "base"
TEMP_DIR = KB_ROOT / "temp"

# 知识库目录命名（目录名；禁路径分隔与保留名）
_KB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_KB_RESERVED = {"base", "temp"}

# 域注册表（分类学的唯一事实源）
DOMAINS: dict[str, dict] = {
    "maps": {"subdir": "maps",
             "desc": "API 映射表：Linux API → 目标 OS 对应物"
                     "（一驱动@目标一张整表）"},
    "gaps": {"subdir": "gaps",
             "desc": "API 缺口处置记录：某 Linux API 无对应物时怎么绕、"
                     "fill 成败如何（一个 API 一个文件，文件名即 API 名）"},
    "runbook": {"subdir": "runbook",
                "desc": "目标 OS 操作手册：怎么构建/启动/跑测试"
                        "（一主题/一坑一文件）"},
    "splits": {"subdir": "splits/strategies",
               "desc": "拆分策略样例：某次迁移的 strategy.md 原样"
                       "（一驱动一文件）"},
    "pitfalls": {"subdir": "pitfalls",
                 "desc": "踩坑记录：平台/模拟器坑与方法教训"
                         "（一坑一文件，条目标签区分）"},
}


def domain_temp(domain: str) -> Path:
    return TEMP_DIR / DOMAINS[domain]["subdir"]


def domain_kb(domain: str, kb_dir: Path) -> Path:
    return kb_dir / DOMAINS[domain]["subdir"]


def domain_base(domain: str) -> Path:
    return BASE_DIR / DOMAINS[domain]["subdir"]


# ---------- 消费面：目录注入 + kb_consulted 记账 ----------

IRON_RULE = ("使用规则：以上为知识条目目录（INDEX）。命中相关条目后自行读取"
             "全文；条目是历次迁移的历史主张——evidence 的 file:line 在树"
             "演进后可能失效，必须在当前源码树/环境中重新核实后才可采用，"
             "禁止照抄未核实结论。可在输出 JSON 中附 kb_consulted 字段"
             "（数组：本次实际读过的条目文件名）。")


def _row_file(e: dict) -> str:
    return str(e.get("file") or e.get("entry_file") or "?").strip()


def _row_desc(e: dict) -> str:
    return str(e.get("desc") or e.get("title") or "").strip()


def _has_entries(d: Path) -> bool:
    if not d.is_dir():
        return False
    return any(f.name not in ("README.md", "INDEX.json")
               for f in d.iterdir())


def render_catalog(parts: list[tuple[str, Path]]) -> str:
    """渲染知识目录注入块（parts = [(标签, 目录), ...]，调用方已筛非空）。

    行格式：- <文件> —— <一句话描述>（hits N>0 时附）。INDEX 缺失/损坏
    → 该分区退化为文件名清单（排除 README/INDEX）。返回空串 = 无可注入。
    """
    sections: list[str] = []
    for label, d in parts:
        idx = load_index(d) if d.is_dir() else None
        if idx is not None:
            lines = []
            for e in idx:
                if not isinstance(e, dict):
                    continue
                hits = int(e.get("hits", 0) or 0)
                suffix = f"（hits {hits}）" if hits > 0 else ""
                lines.append(f"- {_row_file(e)} —— {_row_desc(e)}{suffix}")
        else:
            lines = [f"- {f.name}"
                     for f in sorted(d.glob("*.md"))
                     if f.name not in ("README.md",)]
            if not lines:
                continue
        sections.append(f"### {label}\n目录: {d.resolve()}\n"
                        + "\n".join(lines))
    if not sections:
        return ""
    return ("## 知识库条目目录（按需自取）\n\n"
            + "\n\n".join(sections) + "\n\n" + IRON_RULE)


def catalog_block(kb_dir: Path | None, domains: list[str],
                  include_temp: bool = True) -> str:
    """调用点注入块：各域的已审分区（知识库目录）+ 草稿分区（temp）。

    已审在前、草稿在后（冲突以已审为准）；目录为空不注入。
    """
    parts: list[tuple[str, Path]] = []
    if kb_dir is not None:
        for dom in domains:
            d = domain_kb(dom, kb_dir)
            if _has_entries(d):
                parts.append((f"{dom}（已审）", d))
    if include_temp:
        for dom in domains:
            d = domain_temp(dom)
            if _has_entries(d):
                parts.append((f"{dom}（草稿，未经人审，冲突以已审为准）", d))
    return render_catalog(parts)


def record_consulted(kb_dir: Path | None, domain: str,
                     files: list) -> int:
    """kb_consulted 回报 → 已审分区 INDEX hits+1（temp 草稿不计数）。"""
    if kb_dir is None or not files:
        return 0
    d = domain_kb(domain, kb_dir)
    idx = load_index(d)
    if not idx:
        return 0
    n = bump_hits(idx, [str(f) for f in files])
    if n:
        save_index(d, idx)
    return n


# ---------- 知识库目录解析 ----------

def kb_dir_of(proj: dict) -> Path | None:
    """project.json 内容 → 本次知识库目录（无 kb_dir 记录 → None）。"""
    raw = proj.get("kb_dir") if isinstance(proj, dict) else None
    if not raw or not isinstance(raw, str):
        return None
    p = Path(raw)
    return p if p.is_absolute() else KB_ROOT / p


def kb_dir_for(ws: Path) -> Path | None:
    """工作区 → 本次知识库目录（读 project.json；缺失/损坏 → None）。"""
    try:
        proj = json.loads((Path(ws) / "project.json").read_text(
            encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return kb_dir_of(proj)


def _gitignore_kb(name: str) -> bool:
    """把 knowledge/<name>/ 追加进工具仓 .gitignore（已存在则跳过）。"""
    gi = TOOL_ROOT / ".gitignore"
    try:
        line = f"knowledge/{name}/"
        cur = gi.read_text(encoding="utf-8") if gi.exists() else ""
        if line in cur.splitlines():
            return True
        gi.write_text(cur.rstrip("\n") + ("\n" if cur else "") + line + "\n",
                      encoding="utf-8")
        return True
    except OSError:
        return False


def select_kb(mode: str, name: str, empty: bool = False,
              git_ignore: bool = False) -> Path | None:
    """p0 --kb 参数处理。返回知识库目录；非法输入打印原因返回 None。

    mode=new：新建 knowledge/<name>/（已存在 → 拒绝，提示改用 use）；
      empty=False 复制 base 内容（缺省），True 建空目录。
      git_ignore=True 时追加 .gitignore（缺省 track，不动 git）。
    mode=use：指定既有 knowledge/<name>/。
    """
    if mode not in ("new", "use"):
        print(f"[porter] --kb: 非法模式 {mode!r}（须 new|use）")
        return None
    if not _KB_NAME_RE.match(name or "") or name in _KB_RESERVED:
        print(f"[porter] --kb: 非法目录名 {name!r}（单段名，不得为 "
              "base/temp）")
        return None
    d = KB_ROOT / name
    if mode == "new":
        if d.exists():
            print(f"[porter] --kb: {d} 已存在——要复用它请用 "
                  f"`--kb use {name}`")
            return None
        if empty:
            d.mkdir(parents=True)
            print(f"[porter] --kb: 已新建空知识库目录 {d}")
        else:
            if not BASE_DIR.is_dir():
                print(f"[porter] --kb: base 分区缺失（{BASE_DIR}）——"
                      "无法复制，请检查工具仓")
                return None
            shutil.copytree(BASE_DIR, d)
            print(f"[porter] --kb: 已新建知识库目录 {d}（复制 base）")
        if git_ignore and not _gitignore_kb(name):
            print(f"[porter] --kb: ⚠️ .gitignore 追加失败（{d} 未忽略，"
                  "请手工处理）")
        return d
    if not d.is_dir():
        print(f"[porter] --kb: 知识库目录不存在 {d}")
        return None
    print(f"[porter] --kb: 使用既有知识库目录 {d}")
    return d


# ---------- 薄 INDEX 助手 ----------

def load_index(d: Path) -> list | None:
    """解析 <目录>/INDEX.json（裸数组）；缺失/损坏返回 None。"""
    p = Path(d) / "INDEX.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, list) else None


def save_index(d: Path, entries: list) -> None:
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    (d / "INDEX.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def upsert_entry(entries: list, file: str, desc: str) -> list:
    """薄 INDEX 行 upsert（保留既有 hits；同名改写 desc）。"""
    for e in entries:
        if isinstance(e, dict) and e.get("file") == file:
            e["desc"] = desc
            return entries
    entries.append({"file": file, "desc": desc, "hits": 0})
    return entries


def bump_hits(entries: list, files: list[str]) -> int:
    """kb_consulted 回报：给列出的条目 hits+1。返回实际命中条数。"""
    fs = {str(f) for f in files}
    n = 0
    for e in entries:
        if isinstance(e, dict) and e.get("file") in fs:
            e["hits"] = int(e.get("hits", 0)) + 1
            n += 1
    return n


# ---------- 通用晋升（薄格式域） ----------

def promote_entries(domain: str, files: list[str] | None,
                    kb_dir: Path) -> tuple[int, list[str]]:
    """temp/<域> → <知识库目录>/<域>：搬文件 + INDEX 行。

    适用于薄格式域（gaps/runbook/pitfalls 等）；maps/splits 沿用各自
    专用晋升逻辑直至迁移完成。files=None 搬全部分区条目；INDEX 未
    登记的散文件不搬。返回 (搬运数, 消息列表)。
    """
    src, dst = domain_temp(domain), domain_kb(domain, kb_dir)
    idx = load_index(src) or []
    rows = [e for e in idx if isinstance(e, dict) and e.get("file")]
    if files is not None:
        fs = {str(f) for f in files}
        rows = [e for e in rows if e.get("file") in fs]
    rows = [e for e in rows if (src / str(e["file"])).exists()]
    if not rows:
        return 0, []
    dst.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for e in rows:
        dst_f = dst / str(e["file"])
        dst_f.parent.mkdir(parents=True, exist_ok=True)  # 嵌套命名空间（gaps/runbook）
        shutil.move(str(src / str(e["file"])), str(dst_f))
        moved.append(str(e["file"]))
    mset = set(moved)
    save_index(src, [e for e in idx
                     if not (isinstance(e, dict)
                             and e.get("file") in mset)])
    didx = load_index(dst) or []
    for e in rows:
        didx = upsert_entry(didx, str(e["file"]), str(e.get("desc", "")))
        for de in didx:  # 同名再晋升保留较高热度
            if isinstance(de, dict) and de.get("file") == e.get("file"):
                de["hits"] = max(int(de.get("hits", 0)),
                                 int(e.get("hits", 0)))
    save_index(dst, didx)
    return len(moved), moved
