"""kb.py — 知识库骨架：目录模型 + 域注册表 + 薄 INDEX + 通用晋升。

目录模型（2026-09 起 vcs 统一管理版——知识库随工作区 git 入库）：
  knowledge/base/     工具随附的一般知识（任意目标 OS 可用；git 跟踪）
  knowledge/<name>/   全局知识库（跨迁移复用素材；p0 --kb use 的种子源，
                      sync_to_global 回写目标）
  <ws>/knowledge/     本次迁移的知识库目录（p0 时由 base 复制或全局库
                      种子化；project.json["kb_dir"] 记录其名字）
  <ws>/knowledge/temp/  草稿区（随工作区 git 入库——每步知识产出可追溯）

  本次迁移的知识库 = <ws>/knowledge/temp/ ∪ <ws>/knowledge/（非 knowledge
  域条目）∪ knowledge/base/（注入面只读）。
  信任分层 = 物理分区：temp 未审；其余已审。
  旧布局（全局 knowledge/<name> 直用）向后兼容：工作区无 knowledge/
  子目录时回落旧行为。

域注册表（"分类 = 子目录"的落地）。加一个域 = 此表登记一行 +
skills/kb-guide.md 补一节 + 调用点对照表补一行——单点改动：
  maps     API 映射表（一驱动@目标一张整表）
  gaps     gap 处置记录（一个 API 一个文件；文件名即 API 名）
  runbook  目标 OS 操作手册（一主题/一坑一文件）
  splits   拆分策略样例（splits/strategies/，一驱动一文件）
  pitfalls 踩坑记录（一坑一文件；方法教训亦入此域，标签区分）
  failures 失败签名（一签名一文件：症状→归责→建议动作；
           消费者 = 错误处理模块求解循环 porter/loop/errorloop.py）

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
from .. import log as _log

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
    "failures": {"subdir": "failures",
                 "desc": "失败签名：症状 → 归责回路 → 建议动作"
                         "（一签名一文件；错误处理求解循环的检索面）"},
}


def domain_temp(domain: str, ws: Path | None = None,
                kb_dir: Path | None = None) -> Path:
    """域草稿分区目录。ws/kb_dir 二选一（都缺省 = 旧全局布局）。"""
    return temp_root(ws=ws, kb_dir=kb_dir) / DOMAINS[domain]["subdir"]


def domain_kb(domain: str, kb_dir: Path) -> Path:
    return kb_dir / DOMAINS[domain]["subdir"]


def domain_base(domain: str) -> Path:
    return BASE_DIR / DOMAINS[domain]["subdir"]


# ---------- 工作区知识库布局（<ws>/knowledge/） ----------

def kb_ws_dir(ws: Path) -> Path | None:
    """工作区知识库根（<ws>/knowledge；不存在 → None=旧布局/无 kb）。"""
    d = Path(ws) / "knowledge"
    return d if d.is_dir() else None


def temp_root(ws: Path | None = None, kb_dir: Path | None = None) -> Path:
    """temp 草稿区根。

    新布局（kb 在工作区）：kb_dir 是 <ws>/knowledge（父目录有
    project.json）→ <kb_dir>/temp；否则 ws 下有 knowledge/ →
    <ws>/knowledge/temp。旧布局/都缺省 → 全局 TEMP_DIR（运行时取模块
    属性，tests 可打桩）。
    """
    if kb_dir is not None and (Path(kb_dir).parent / "project.json").is_file():
        return Path(kb_dir) / "temp"
    if ws is not None:
        d = kb_ws_dir(ws)
        if d is not None:
            return d / "temp"
    return TEMP_DIR


def validate_kb_arg(mode: str, name: str) -> str | None:
    """--kb 参数校验；返回错误消息（None=合法）。"""
    if mode not in ("new", "use"):
        return f"非法模式 {mode!r}（须 new|use）"
    if not _KB_NAME_RE.match(name or "") or name in _KB_RESERVED:
        return f"非法目录名 {name!r}（单段名，不得为 base/temp）"
    return None


def sync_to_global(ws: Path, name: str | None = None) -> bool:
    """工作区知识库 → 全局 knowledge/<name>（跨迁移复用素材）。

    排除 temp/（草稿）与 .hits.json（遥测旁车）。best-effort；用于
    p1/p2/kb promote 后让下次迁移 `--kb use <name>` 能看到沉淀。
    """
    d = kb_ws_dir(ws)
    if d is None:
        return False
    if not name:
        try:
            proj = json.loads((Path(ws) / "project.json").read_text(
                encoding="utf-8"))
            name = proj.get("kb_dir")
        except (OSError, json.JSONDecodeError):
            name = None
    if not name or not _KB_NAME_RE.match(name or "") or name in _KB_RESERVED:
        return False
    dst = KB_ROOT / name
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for src in d.iterdir():
            if src.name in ("temp", HITS_SIDECAR):
                continue
            if src.is_dir():
                shutil.copytree(src, dst / src.name, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst / src.name)
        return True
    except OSError:
        return False


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


def render_catalog(parts: list[tuple[str, Path]],
                   with_rule: bool = True,
                   extra_hits: dict[Path, dict[str, int]] | None = None
                   ) -> str:
    """渲染知识目录注入块（parts = [(标签, 目录), ...]，调用方已筛非空）。

    行格式：- <文件> —— <一句话描述>（hits N>0 时附，N = INDEX 已折叠
    + 旁车未折叠的合并值——extra_hits 按分区目录给未折叠部分）。
    INDEX 缺失/损坏 → 该分区退化为文件名清单（排除 README/INDEX）。
    返回空串 = 无可注入。
    """
    sections: list[str] = []
    for label, d in parts:
        idx = load_index(d) if d.is_dir() else None
        if idx is not None:
            extra = (extra_hits or {}).get(d) or {}
            lines = []
            for e in idx:
                if not isinstance(e, dict):
                    continue
                hits = int(e.get("hits", 0) or 0) \
                    + int(extra.get(_row_file(e), 0))
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
    head = "## 知识库条目目录（按需自取）\n\n" + "\n\n".join(sections)
    return head + ("\n\n" + IRON_RULE if with_rule else "")


def catalog_block(kb_dir: Path | None, domains: list[str],
                  include_temp: bool = True,
                  with_rule: bool = True,
                  temp_base: Path | None = None) -> str:
    """调用点注入块：各域的已审分区（知识库目录）+ 草稿分区（temp）。

    已审在前、草稿在后（冲突以已审为准）；目录为空不注入。
    已审分区的 hits 显示 = INDEX 行（晋升时折叠）+ 旁车 .hits.json
    （运行时回报，未折叠）的合并值。temp_base 缺省用全局 TEMP_DIR
    （运行时取模块属性，tests 可打桩）。
    """
    tb = temp_base if temp_base is not None else TEMP_DIR
    parts: list[tuple[str, Path]] = []
    extra: dict[Path, dict[str, int]] | None = None
    if kb_dir is not None:
        side = load_hits_sidecar(kb_dir)
        if side:
            extra = {}
        for dom in domains:
            d = domain_kb(dom, kb_dir)
            if _has_entries(d):
                parts.append((f"{dom}（已审）", d))
                if extra is not None:
                    prefix = f"{dom}/"
                    m = {k[len(prefix):]: v for k, v in side.items()
                         if k.startswith(prefix)}
                    if m:
                        extra[d] = m
    if include_temp:
        for dom in domains:
            d = tb / DOMAINS[dom]["subdir"]
            if _has_entries(d):
                parts.append((f"{dom}（草稿，未经人审，冲突以已审为准）", d))
    return render_catalog(parts, with_rule=with_rule, extra_hits=extra)


def load_guide() -> str:
    """总纲 skill 文本（skills/kb-guide.md；缺失降级为空）。"""
    try:
        p = TOOL_ROOT / "skills" / "kb-guide.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        return ""


def kb_face(ws: Path, domains: list[str], include_temp: bool = True) -> str:
    """KB 面注入文本 = 总纲 skill + 相应域的条目目录。

    无任何可注入条目 → ""（调用方省略知识面；规则 0 不空转）。
    """
    cat = catalog_block(kb_dir_for(ws), domains,
                        include_temp=include_temp, with_rule=False,
                        temp_base=temp_root(ws=ws))
    if not cat:
        return ""
    guide = load_guide()
    return (guide + "\n\n---\n\n" + cat) if guide else cat


def record_consulted(kb_dir: Path | None, domain: str,
                     files: list) -> int:
    """kb_consulted 回报 → 旁车 .hits.json 计数（corpus 正式分区只在
    晋升时写——运行时遥测不弄脏 git 跟踪的知识库目录）。

    只计该域 INDEX 已登记的条目（幻影文件不计数）；temp 草稿不计数。
    并发丢失更新按遥测级接受（丢计数不丢知识）。
    """
    if kb_dir is None or not files:
        return 0
    idx = load_index(domain_kb(domain, kb_dir)) or []
    listed = {_row_file(e) for e in idx if isinstance(e, dict)}
    counts = load_hits_sidecar(kb_dir)
    n = 0
    for f in files:
        f = str(f)
        if f in listed:
            k = sidecar_key(domain, f)
            counts[k] = counts.get(k, 0) + 1
            n += 1
    if n:
        save_hits_sidecar(kb_dir, counts)
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
    """工作区 → 本次知识库目录。

    新布局优先：<ws>/knowledge/ 存在即用（vcs 统一管理版）；
    否则回落旧布局（project.json 的 kb_dir 相对 KB_ROOT 解析）。
    project.json 缺失/损坏 → None。
    """
    d = kb_ws_dir(ws)
    if d is not None:
        return d
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
              git_ignore: bool = False,
              ws: Path | None = None) -> Path | None:
    """p0 --kb 参数处理。返回知识库目录；非法输入打印原因返回 None。

    工作区模式（ws 给出，vcs 统一管理版）：
      mode=new：新建 <ws>/knowledge/（已存在 → 拒绝）；empty=False
        复制 base 内容，True 建空目录。git 策略不适用（随工作区 git
        统一入库），git_ignore 忽略。
      mode=use：复制全局 knowledge/<name>/ → <ws>/knowledge/（种子化；
        沉淀回全局用 sync_to_global）。
    旧模式（ws 缺省，向后兼容）：
      mode=new：新建 knowledge/<name>/（已存在 → 拒绝，提示改用 use）；
        empty=False 复制 base；git_ignore=True 追加 .gitignore。
      mode=use：指定既有 knowledge/<name>/。
    """
    err = validate_kb_arg(mode, name)
    if err:
        _log.console_line(f"[porter] --kb: {err}")
        return None
    if ws is not None:
        d = Path(ws) / "knowledge"
        if d.exists():
            _log.console_line(f"[porter] --kb: {d} 已存在——工作区知识库"
                  "只建一次（复用 project.json 记录；删除该目录方可重建）")
            return None
        if mode == "new":
            if empty:
                d.mkdir(parents=True)
                _log.console_line(f"[porter] --kb: 已新建空知识库目录 {d}")
            else:
                if not BASE_DIR.is_dir():
                    _log.console_line(f"[porter] --kb: base 分区缺失"
                          f"（{BASE_DIR}）——无法复制，请检查工具仓")
                    return None
                shutil.copytree(BASE_DIR, d)
                _log.console_line(f"[porter] --kb: 已新建知识库目录 {d}"
                      "（复制 base）")
            return d
        src = KB_ROOT / name
        if not src.is_dir():
            _log.console_line(f"[porter] --kb: 全局知识库目录不存在 {src}")
            return None
        shutil.copytree(src, d)
        _log.console_line(f"[porter] --kb: 已从全局库种子化 {src} → {d}")
        return d
    d = KB_ROOT / name
    if mode == "new":
        if d.exists():
            _log.console_line(f"[porter] --kb: {d} 已存在——要复用它请用 "
                  f"`--kb use {name}`")
            return None
        if empty:
            d.mkdir(parents=True)
            _log.console_line(f"[porter] --kb: 已新建空知识库目录 {d}")
        else:
            if not BASE_DIR.is_dir():
                _log.console_line(f"[porter] --kb: base 分区缺失（{BASE_DIR}）——"
                      "无法复制，请检查工具仓")
                return None
            shutil.copytree(BASE_DIR, d)
            _log.console_line(f"[porter] --kb: 已新建知识库目录 {d}（复制 base）")
        if git_ignore and not _gitignore_kb(name):
            _log.console_line(f"[porter] --kb: ⚠️ .gitignore 追加失败（{d} 未忽略，"
                  "请手工处理）")
        return d
    if not d.is_dir():
        _log.console_line(f"[porter] --kb: 知识库目录不存在 {d}")
        return None
    _log.console_line(f"[porter] --kb: 使用既有知识库目录 {d}")
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


# ---------- hits 旁车（运行时遥测不写 corpus 正式分区） ----------

HITS_SIDECAR = ".hits.json"


def hits_path(kb_dir: Path) -> Path:
    return Path(kb_dir) / HITS_SIDECAR


def sidecar_key(domain: str, file: str) -> str:
    return f"{domain}/{file}"


def load_hits_sidecar(kb_dir: Path) -> dict[str, int]:
    """旁车计数表 {<域>/<file>: n}（缺失/损坏 → 空）。"""
    try:
        data = json.loads(hits_path(kb_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items()
            if isinstance(v, (int, float))}


def save_hits_sidecar(kb_dir: Path, counts: dict[str, int]) -> None:
    p = hits_path(kb_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(counts, ensure_ascii=False, indent=2,
                            sort_keys=True) + "\n", encoding="utf-8")


def fold_sidecar_hits(kb_dir: Path, domain: str,
                      files: list[str]) -> dict[str, int]:
    """晋升折叠：把 <域>/<file> 的旁车计数取出并从旁车清除。

    返回 {file: n}（无计数的 file 不在返回值中）。corpus 的 INDEX 行
    在晋升时把返回值并入 hits——正式分区只在晋升时写的唯一例外来源。
    """
    counts = load_hits_sidecar(kb_dir)
    out: dict[str, int] = {}
    for f in {str(f) for f in files}:
        k = sidecar_key(domain, f)
        if k in counts:
            out[f] = counts.pop(k)
    if out:
        save_hits_sidecar(kb_dir, counts)
    return out


# ---------- 通用晋升（薄格式域） ----------

def promote_entries(domain: str, files: list[str] | None,
                    kb_dir: Path) -> tuple[int, list[str]]:
    """temp/<域> → <知识库目录>/<域>：搬文件 + INDEX 行。

    适用于薄格式域（gaps/runbook/pitfalls 等）；maps/splits 沿用各自
    专用晋升逻辑直至迁移完成。files=None 搬全部分区条目；INDEX 未
    登记的散文件不搬。返回 (搬运数, 消息列表)。
    """
    src, dst = domain_temp(domain, kb_dir=kb_dir), domain_kb(domain, kb_dir)
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
    folded = fold_sidecar_hits(kb_dir, domain, moved)  # 晋升折叠旁车计数
    didx = load_index(dst) or []
    for e in rows:
        didx = upsert_entry(didx, str(e["file"]), str(e.get("desc", "")))
        for de in didx:  # 同名再晋升保留较高热度 + 旁车折叠
            if isinstance(de, dict) and de.get("file") == e.get("file"):
                de["hits"] = (max(int(de.get("hits", 0)),
                                  int(e.get("hits", 0)))
                              + int(folded.get(str(e["file"]), 0)))
    save_index(dst, didx)
    return len(moved), moved
