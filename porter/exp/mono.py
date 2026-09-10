"""mono.py — exp-mono：模块迁移 loop（实验子命令，直连 P1 结尾）。

形态（2026-09-10 拆分改造）：每模块两段任务——**研究者 agent**（只读
目标树，产出结构化研究交付物）+ **翻译者 agent**（消费交付物，写码+
执行式测试，四段复合 gate 验证）。研究/翻译各自独立 session、独立
预算、独立模型（config.models.reasoning / coding）。平台事实全部来自
工作区数据面（runner.json / scaffold manifest / module.json），本文件
零目标 OS / 目标语言假设。

每模块流程：
  1. 幂等：ledger 里该模块 research.status / status 为 pass → 分别跳过
  2. 研究任务：prompt = SKILL + 规格文件 + 词典/契约登记表/泊车现文 +
     依赖产物清单；产出 exp-mono/research/<module>.md（正文+JSON 块）；
     编排器浅校验（词表/必填/证据路径警告）不过 → 同 session 回灌修
     （≤2 次）；过 → 机器收割（mappings→词典 / contracts→登记表 /
     parking→泊车）
  3. 翻译任务：prompt = SKILL + 规格 + 交付物全文 + 契约登记表 + 泊车 +
     依赖产物；run_agent_seq 同 session 多轮；静态段 = 四段复合 gate
     （① 产物守卫 ② 构建 ③ 启动 ④ 单测），修复段预算下限保护 +
     停滞元反馈；done 后声明面核对，不过 → 同 session 自动重试 ≤2
  4. gate 全绿 + 记账完备 → ledger=pass + 目标树按白名单 commit；
     blocked / 失败 / 预算耗尽 → 停车 rc 1（ledger 断点续）

预算（秒）：研究 = clamp(600, 行数×1.5, 2400)；翻译 = clamp(900,
行数×1.3, 4200)（CLI 可覆盖）。终局（order 全 pass）：全量单测（阻断）
+ 启动冒烟（非阻断留档）+ report.md。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ..common import agent
from ..common import scope as _scope
from ..env import probe as probe_mod
from .. import log as _log

SKILL_RESEARCH = "EXP-research"
SKILL_TRANSLATE = "EXP-mono-migrate"
GATE_DESC = "四段复合静态检查（① 产物守卫 ② 构建 ③ 启动 ④ 单测）"
FIX_FLOOR_SEC = 300          # 修复段预算下限（一次性宽限，防主段吃光）
STALL_META_ROUNDS = 1        # 停滞元反馈轮数（同签名连败时的换思路警告）
DECL_RETRIES = 2             # 声明面核对不过的同 session 自动重试上限
RESEARCH_RETRIES = 2         # 交付物校验不过的同 session 回灌上限
_COMMENT_PREFIXES = ("//", "/*", "*", "*/", "#")


# ---------- 通用小件 ----------

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _budget_sec(loc: int) -> int:
    """翻译任务预算（静态段时长在预算之外）。"""
    return max(900, min(4200, int(loc * 1.3)))


def _budget_research(loc: int) -> int:
    """研究任务预算（只读研究，读多写少）。"""
    return max(600, min(2400, int(loc * 1.5)))


def _declaration_problems(parsed: dict, marker_delta: int | None,
                          marker: str | None) -> list[str]:
    """done 声明面的记账完备性核对（个数不设限；核对的是一致性）。

    - migrated_functions 中每个单元必须出现在 tests ∪ untested（不许
      静默漏测）；
    - 同一单元不得同时出现在 tests 与 untested；
    - 声明的每个 test 必须是真实落地的带标记测试（标记计数核对；
      marker 未配置时跳过计数核对）；
    - 条目形状：tests 须含非空 fn/aspect/name，untested 须含
      fn/reason。
    """
    probs: list[str] = []
    tests = parsed.get("tests") or []
    untested = parsed.get("untested") or []
    migrated = parsed.get("migrated_functions") or []

    def _s(v) -> bool:
        return isinstance(v, str) and bool(v.strip())

    tested_fns, untested_fns = [], []
    for t in tests:
        if not isinstance(t, dict) or not (_s(t.get("fn"))
                                           and _s(t.get("aspect"))
                                           and _s(t.get("name"))):
            probs.append(f"tests 条目形状非法（须含非空 fn/aspect/name）"
                         f"：{str(t)[:120]}")
            continue
        tested_fns.append(t["fn"].strip())
    for u in untested:
        if not isinstance(u, dict) or not (_s(u.get("fn"))
                                           and _s(u.get("reason"))):
            probs.append(f"untested 条目形状非法（须含非空 fn/reason）"
                         f"：{str(u)[:120]}")
            continue
        untested_fns.append(u["fn"].strip())
    both = sorted(set(tested_fns) & set(untested_fns))
    if both:
        probs.append("同一单元同时出现在 tests 与 untested："
                     + "、".join(both))
    covered = set(tested_fns) | set(untested_fns)
    missing = sorted({f.strip() for f in migrated if _s(f)
                      and f.strip() not in covered})
    if missing:
        probs.append("记账不完备——migrated 中未出现在 tests ∪ untested："
                     + "、".join(missing))
    if tests and marker is not None and marker_delta is not None \
            and marker_delta < len(tests):
        probs.append(f"声明 {len(tests)} 个测试但测试标记增量仅 "
                     f"{marker_delta}——每个声明的测试必须是真实落地"
                     "的带标记执行式测试")
    return probs


def _text_lines(path: Path) -> int:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _code_lines(path: Path) -> int:
    """非注释、非空行计数（注释前缀按 C 系与脚本系通行习语通吃）。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    n = 0
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith(_COMMENT_PREFIXES):
            continue
        n += 1
    return n


def _driver_files(home: Path, ext: str | None) -> list[Path]:
    if not home.is_dir():
        return []
    return sorted(p for p in home.rglob("*")
                  if p.is_file() and (ext is None or p.suffix == ext))


def _count_code(home: Path, ext: str | None) -> int:
    return sum(_code_lines(p) for p in _driver_files(home, ext))


def _count_marker(home: Path, ext: str | None, marker: str | None) -> int:
    if not marker:
        return 0
    n = 0
    for p in _driver_files(home, ext):
        try:
            n += p.read_text(encoding="utf-8", errors="replace").count(marker)
        except OSError:
            continue
    return n


def _git_status(root: Path) -> set[str]:
    """目标树 git status 路径集（porcelain 解析；失败 → 空集）。"""
    try:
        proc = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    out: set[str] = set()
    for ln in (proc.stdout or "").splitlines():
        path = ln[3:].strip() if len(ln) > 3 else ""
        if "->" in path:
            path = path.split("->", 1)[1].strip()
        if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path:
            out.add(path)
    return out


def _shell_ut(cmd: str, cwd: Path, env: dict, timeout_sec: int,
              log_path: Path) -> tuple[int, str]:
    """单测命令执行器（runner 驱动；全文落盘）。"""
    _log.console_line(f"[porter] exp-mono: 执行单测 "
                      f"{cmd[:110]}{'…' if len(cmd) > 110 else ''}")
    try:
        proc = subprocess.run(["bash", "-c", cmd], cwd=str(cwd), env=env,
                              capture_output=True, text=True,
                              timeout=timeout_sec)
        rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
            f"\nTIMEOUT after {timeout_sec}s"
        rc = -1
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(out, encoding="utf-8", errors="replace")
    return rc, out


# ---------- 研究交付物（research deliverable）----------

VERDICTS = ("equivalent", "adapt", "helper", "bypass", "parked")
REQUIRED_SECTIONS = ("适配架构与整合方案", "可测面评估")


def _split_deliverable(text: str) -> tuple[str, dict | None]:
    """分离研究交付物正文与结构化 JSON 块（仿 scope.split_strategy_output）。

    规则：取最后一个能解析为 dict 且含 "module" 键的 ```json 围栏块
    作为结构化部分抽出；无合规块 → (原文, None)。
    """
    pat = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
    for m in reversed(list(pat.finditer(text))):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("module"), str):
            md = pat.sub("", text).rstrip() + "\n"
            return md, parsed
    return text, None


def _path_token_hits(tok: str, roots: list[Path]) -> bool:
    """证据 token 是否解析到真实文件：剥 :line/-range 后缀后按序试根。

    绝对路径直接判；相对路径依次拼 roots。解析不到不算错误（调用方
    只收集警告）——研究者可能引用带上下文的复合描述。
    """
    cand = re.split(r":[0-9][0-9,\-]*", tok.strip(), 1)[0]
    cand = cand.strip("`'\"，,;；")
    if not cand or "/" not in cand:
        return False
    p = Path(cand)
    if p.is_absolute():
        return p.is_file()
    return any((root / cand).is_file() for root in roots)


def _validate_research(text: str, module: str, target_os, driver_home_rel,
                       ws) -> dict:
    """研究交付物浅校验：结构化骨架完备 + 词表合法；证据路径仅警告。

    原则（与声明面核对一致）：校验一致性不校验语义。problems 非空 =
    打回重修；warnings 只记录（进 ledger 供人审）。
    """
    problems: list[str] = []
    warnings: list[str] = []
    md, data = _split_deliverable(text)
    if data is None:
        return {"ok": False,
                "problems": ['交付物缺少可解析的 ```json 块'
                             '（须为含 "module" 键的 dict 对象）'],
                "warnings": [], "data": None, "prose": md}
    if data.get("module") != module:
        problems.append(f"module 字段 {data.get('module')!r} ≠ 本模块 "
                        f"{module!r}")
    # 叙事必写节（硬性要求 3）：标题字面存在即可，无内容要求——
    # 纯数据模块允许写"无特殊决策/直译"，但不许略节。
    for sec in REQUIRED_SECTIONS:
        if sec not in md:
            problems.append(f"叙事缺必写节标题：「{sec}」"
                            "（无实际内容时明确写'无特殊决策/直译'）")
    roots = [Path(target_os), Path(target_os) / driver_home_rel, Path(ws)]

    def _entries(key: str, required: tuple[str, ...]) -> list[dict]:
        v = data.get(key)
        if not isinstance(v, list):
            problems.append(f"缺 {key} 数组（无内容也须给空数组）")
            return []
        out: list[dict] = []
        for i, e in enumerate(v):
            if not isinstance(e, dict):
                problems.append(f"{key}[{i}] 须为对象")
                continue
            missing = [f for f in required
                       if not (isinstance(e.get(f), str) and e[f].strip())]
            if missing:
                problems.append(f"{key}[{i}] 缺必填字段或为空："
                                + "、".join(missing))
                continue
            out.append(e)
        return out

    mappings = _entries("mappings", ("symbol", "verdict", "usage",
                                     "evidence"))
    contracts = _entries("contracts", ("name", "signature", "anchor"))
    _entries("prunes", ("item", "reason"))
    parking = _entries("parking", ("item", "missing", "handling"))
    _entries("read_list", ("path",))
    _entries("negatives", ("claim", "evidence"))
    _entries("open_questions", ("question",))
    if not problems and not mappings:
        problems.append("mappings 为空——本模块至少须核实并裁定一个"
                        "源侧 API（纯数据模块也必然触碰接口）")
    for i, m in enumerate(mappings):
        if m["verdict"].strip() not in VERDICTS:
            problems.append(f"mappings[{i}].verdict {m['verdict']!r} 不在"
                            f"词表 {VERDICTS}")

    def _warn_unresolved(kind: str, idx: int, val: str) -> None:
        toks = [t for t in re.split(r"[\s+；;，,]+", val) if "/" in t]
        if toks and not any(_path_token_hits(t, roots) for t in toks):
            warnings.append(f"{kind}[{idx}] 证据路径未能解析到真实文件："
                            f"{val[:80]}")

    for i, m in enumerate(mappings):
        _warn_unresolved("mappings", i, m["evidence"])
    for i, c in enumerate(contracts):
        _warn_unresolved("contracts", i, c["anchor"])
    _ = parking            # 计数交收割器；此处仅形状校验
    return {"ok": not problems, "problems": problems,
            "warnings": warnings, "data": data, "prose": md}


def _section_exists(text: str, section: str) -> bool:
    return any(ln.strip() == section for ln in text.splitlines())


def _harvest_research(exp_dir: Path, module: str, data: dict) -> dict:
    """收割研究交付物的结构化部分（机器排版、幂等、零语义加工）。

    - mappings → mapping-notes.md（模块节追加）
    - contracts → contracts.md（契约登记表，不存在则建）
    - parking → parking.md（`<module> 研究轮` 节追加）
    幂等判据 = 模块节标题行已存在 → 该文件跳过（防重复追加）。
    """
    counts = {"dictionary": 0, "contracts": 0, "parking": 0}
    section = f"## {module}"

    def _append(path: Path, header: str, lines: list[str],
                init: str = "") -> bool:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            text = init
        if _section_exists(text, header):
            return False
        path.write_text(text.rstrip() + "\n\n" + header + "\n"
                        + "\n".join(lines) + "\n", encoding="utf-8")
        return True

    lines = []
    for m in data.get("mappings") or []:
        note = str(m.get("notes") or "").strip()
        tail = f"（注意：{note}）" if note else ""
        lines.append(f"- {m['symbol']} | {m['verdict']} | {m['usage']}"
                     f" | {m['evidence']}{tail}")
    if lines and _append(exp_dir / "mapping-notes.md", section, lines):
        counts["dictionary"] = len(lines)

    lines = []
    for c in data.get("contracts") or []:
        consumers = str(c.get("consumers") or "").strip() or "—"
        conflict = str(c.get("conflicts_with") or "").strip() or "无"
        lines.append(f"- **{c['name']}**：`{c['signature']}`"
                     f"（锚 {c['anchor']}；消费方 {consumers}；"
                     f"冲突 {conflict}）")
    if lines and _append(exp_dir / "contracts.md", section, lines,
                         init="# 契约登记表\n\n"
                              "> 跨模块共享的类型/钩子签名——研究任务提案、机器"
                              "收割追加。后继模块研究前必查：新契约若与既有条目"
                              "冲突，在交付物 conflicts_with 显式标记。\n"):
        counts["contracts"] = len(lines)

    lines = []
    for e in data.get("parking") or []:
        refill = str(e.get("refill_when") or "").strip() or "待定"
        lines.append(f"- **{e['item']}**：缺 {e['missing']}；临时处置："
                     f"{e['handling']}；回填条件：{refill}")
    if lines and _append(exp_dir / "parking.md",
                         f"## {module} 研究轮", lines):
        counts["parking"] = len(lines)
    return counts


def _load_models(config_path=None) -> tuple[str | None, str | None]:
    """读取 config 的 models.reasoning / models.coding（exp-mono 双模型）。

    返回 (reasoning, coding) 或 (None, 错误消息)。校验：两键存在非空、
    含 provider 前缀（'/'——裸模型名会在 provider 侧报神秘 server error，
    此处前置拦截）。两值允许一致。旧 "model" 键与此无关（其他流程用）。
    """
    path = (Path(config_path) if config_path
            else agent.TOOL_ROOT / "porter" / "config.json")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as ex:
        return None, f"config 不可读（{path}）：{ex!r}"
    models = cfg.get("models") if isinstance(cfg, dict) else None
    if not isinstance(models, dict):
        return None, ("config 缺 models 配置块——exp-mono 需要 "
                      "models.reasoning（研究）与 models.coding（翻译）")
    out = []
    for key in ("reasoning", "coding"):
        v = models.get(key)
        if not (isinstance(v, str) and v.strip()):
            return None, (f"config.models.{key} 缺失或为空——请在 "
                          "config.json 配置后重跑")
        if "/" not in v:
            return None, (f"config.models.{key} = {v!r} 缺 provider 前缀"
                          "（须形如 provider/model）")
        out.append(v.strip())
    return out[0], out[1]


# ---------- 三段复合 gate ----------

def _guard_products(home: Path, driver_home_rel: str, manifest: dict,
                    snap: dict, loc: int, target_root: Path) -> list[str]:
    """① 产物守卫：代码增量 + 构建单元登记 + 改动范围。"""
    problems: list[str] = []
    ext = manifest.get("source_ext") or None
    if not snap.get("growth_guard", True):
        _log.console_line("[porter] exp-mono: 续跑无代码基线——本轮跳过"
                          "增量守卫（build/单测/记账照常）")
    else:
        now_lines = _count_code(home, ext)
        need = max(8, loc // 20)
        grew = now_lines - snap["code_lines"]
        if grew < need:
            problems.append(
                f"① 产物守卫：driver_home 非注释代码增量 {grew} 行 < 阈值 "
                f"{need} 行（模块源 {loc} 行的 5%，下限 8）——真实迁移尚未"
                "发生，先完成代码翻译再请求验证")
    integ = manifest.get("integration") or {}
    list_file, entry_template = integ.get("list_file"), \
        integ.get("entry_template")
    if ext and list_file and entry_template:
        reg = home / list_file
        content = ""
        try:
            content = reg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            problems.append(f"① 产物守卫：构建单元登记清单 {reg} 不可读")
        list_dir = (home / list_file).parent
        missing = []
        for f in _driver_files(home, ext):
            rel = f.relative_to(home)
            if rel.as_posix() == list_file or f.parent != list_dir:
                continue        # 清单自身 / 非同级文件不在本级登记面
            entry = str(entry_template).replace("{stem}", f.stem)
            if entry not in content:
                missing.append(f"{rel.as_posix()}（缺登记 `{entry}`）")
        if missing:
            problems.append("① 产物守卫：以下新文件未在 "
                            f"{list_file} 登记（按模板 "
                            f"`{entry_template}` 渲染后应逐字出现）：\n"
                            + "\n".join(f"  - {m}" for m in missing))
    wiring = set(manifest.get("commit_paths") or [])

    def _allowed(p: str) -> bool:
        return (p == driver_home_rel
                or p.startswith(driver_home_rel + "/")
                or p in wiring)

    offenders = sorted(p for p in _git_status(target_root) - snap["status"]
                       if not _allowed(p))
    if offenders:
        problems.append(
            "① 产物守卫：改动越出白名单（driver_home 与数据面登记的"
            "接线文件）：\n" + "\n".join(f"  - {p}" for p in offenders)
            + "\n请把改动收回 driver_home（或回滚越界文件）后重验")
    return problems


def _run_ut(exp_dir: Path, target_os: Path, runner: dict,
            driver_home_rel: str, label: str,
            which: str = "driver") -> tuple[bool, str, Path]:
    """③ 单测执行：which=driver 用 driver_scope_cmd（缺键降级全量 cmd）；
    which=full 一律用全量 cmd。判据 = rc0 + success_pattern 命中 +
    fail_pattern 不命中。"""
    ut = runner.get("unit_test") or {}
    tpl = ut.get("driver_scope_cmd") if which == "driver" else None
    src = "driver_scope_cmd"
    if not tpl:
        tpl = ut.get("cmd")
        src = "cmd"
    if not tpl:
        return False, "runner 缺单测命令（driver_scope_cmd 与 cmd 均无）", \
            exp_dir / "logs" / f"{label}.log"
    cmd = re.sub(r"(?<!\$)\{PORTER_TARGET_OS_ROOT\}",
                 lambda _m: str(target_os.resolve()), str(tpl))
    cmd = re.sub(r"(?<!\$)\{PORTER_DRIVER_HOME\}",
                 lambda _m: driver_home_rel, cmd)
    env = {**os.environ,
           "PORTER_TARGET_OS_ROOT": str(target_os.resolve()),
           "PORTER_DRIVER_HOME": driver_home_rel,
           **(runner.get("env") or {})}
    log_path = exp_dir / "logs" / f"{label}.log"
    rc, out = _shell_ut(cmd, cwd=target_os, env=env,
                        timeout_sec=int(ut.get("timeout_sec") or 3600),
                        log_path=log_path)
    succ, fail = ut.get("success_pattern") or "", ut.get("fail_pattern") or ""
    ok = rc == 0
    detail = f"[{src}] rc={rc}"
    if succ:
        hit = succ in out
        ok = ok and hit
        detail += f" success_pattern={'hit' if hit else 'MISS'}"
    if fail:
        bad = fail in out
        ok = ok and not bad
        detail += f" fail_pattern={'hit' if bad else 'no-hit'}"
    return ok, f"{detail}（全文：{log_path}）", log_path


def _make_gate(exp_dir: Path, target_os: Path, runner: dict, manifest: dict,
               module: str, loc: int, driver_home_rel: str,
               snap: dict) -> dict:
    """组装三段复合静态段（供 run_agent_seq 的 static 参数）。"""

    def _fn() -> tuple[bool, str]:
        home = target_os / driver_home_rel
        problems = _guard_products(home, driver_home_rel, manifest, snap,
                                   loc, target_os)
        if problems:
            return False, "\n".join(problems)
        b = probe_mod.probe_build(exp_dir, target_os, runner,
                                  label=f"exp_{module}_build")
        if not b["ok"]:
            build_log = exp_dir / "logs" / f"exp_{module}_build.log"
            return False, (f"② 构建 FAIL：{b.get('detail', '')}"
                           f"（全文：{build_log}）")
        # ③ 启动（阻断）：带上目标树当前全部已迁移代码（每模块 pass 即
        # commit），全树构建后启动自检。比单测便宜，先行短路。
        boot = probe_mod.probe_boot(exp_dir, target_os, runner,
                                    label=f"exp_{module}_boot")
        if not boot.get("ok"):
            boot_log = exp_dir / "logs" / f"T3_exp_{module}_boot.log"
            return False, (f"③ 启动 FAIL：{boot.get('detail', '')}"
                           f"（全文：{boot_log}；排 panic 看启动串口日志）")
        ut_ok, ut_detail, _lp = _run_ut(exp_dir, target_os, runner,
                                        driver_home_rel,
                                        label=f"exp_{module}_ut")
        if not ut_ok:
            return False, f"④ 单测 FAIL：{ut_detail}"
        return True, ("四段全绿：产物守卫通过；构建成功；启动成功"
                      "（含全部已迁移代码）；单测通过"
                      "（测试记账完备性在 done 后由编排器核对声明面）")

    return {"describe": GATE_DESC, "fn": _fn}


# ---------- prompt 组装 ----------

def _dep_lines(ledger: dict, deps: list[str]) -> list[str]:
    lines = []
    for d in deps:
        entry = (ledger.get("modules") or {}).get(d) or {}
        files = entry.get("files") or []
        lines.append(f"- {d}：" + ("、".join(f"`{f}`" for f in files)
                                   if files else "（无登记产物清单）"))
    return lines


def _research_prompt(ws: Path, exp_dir: Path, module: str, mod_json: dict,
                     spec_files: list[tuple[Path, int]], ledger: dict,
                     manifest: dict, proj: dict, target_os: Path,
                     driver_home_rel: str, deps: list[str],
                     notes_text: str, parking_text: str,
                     registry_text: str, deliverable_path: Path) -> str:
    skill = agent.load_skill(SKILL_RESEARCH)
    home = target_os / driver_home_rel
    ext = manifest.get("source_ext")
    integ = manifest.get("integration") or {}
    ts = manifest.get("test_substrate") or {}
    marker = ts.get("marker") or "（数据面未提供）"
    how = ts.get("how") or "（数据面未提供惯例说明）"
    reg_lines = []
    if integ.get("list_file") and integ.get("entry_template"):
        reg_lines = [
            f"- 登记约定：清单文件 `{home / integ['list_file']}`；登记行"
            f"模板 `{integ['entry_template']}`（`{{stem}}` = 文件名去"
            "扩展名）——构建入口事实，整合方案须与之相容"]
    existing = "、".join(f"`{p.name}`"
                         for p in _driver_files(home, ext)) or "（空）"
    dep_lines = _dep_lines(ledger, deps)
    spec_listing = "\n".join(f"  - `{p}`（{n} 行）" for p, n in spec_files)
    return (f"{skill}\n\n---\n\n## 背景数据（平台事实，以数据面为准）\n"
            f"- 目标 OS 源码树：`{target_os}` = 你的工作目录"
            "（**只读研究——禁止改动目标树任何文件**）\n"
            f"- 驱动落点 driver_home（供检索先例，不写入）：`{home}`\n"
            f"- driver_home 现有文件（先例与词汇的第一现场）："
            f"{existing}\n"
            + "\n".join(reg_lines)
            + ("\n" if reg_lines else "")
            + f"- 测试基质：标记 `{marker}`；惯例：{how}"
            "——可测面评估的依据\n"
            f"- Linux 参考源树（只读规格）：`{proj.get('linux_driver')}`\n"
            f"- 迁移词典（前序研究的裁定，先读再用——你**不直接写词典**，"
            f"裁定写进交付物由编排器收割）：\n\n"
            f"{notes_text.strip() or '（空——你是第一位研究者）'}\n\n"
            f"- 契约登记表（**提案前必查**——冲突须在 conflicts_with "
            f"显式标记）：\n\n"
            f"{registry_text.strip() or '（空——尚无契约登记）'}\n\n"
            f"- 泊车记录（既有平台能力缺口裁定）：\n\n"
            f"{parking_text.strip() or '（空）'}\n\n"
            f"## 本模块任务\n"
            f"- 模块：`{module}` —— {mod_json.get('function', '')}\n"
            f"- 模块规格文件（逐个精读）：\n{spec_listing}\n"
            f"- 依赖模块已迁产物（研究其对外接口面，理解既有词汇与"
            "契约）：\n"
            + ("\n".join(dep_lines) if dep_lines else "  （无依赖模块）")
            + f"\n- 研究交付物写入路径（唯一允许的写入）："
            f"`{deliverable_path}`\n\n"
            f"## 任务\n按 SKILL 纪律完成本模块研究，把交付物（一个文件："
            "叙事正文 + 末尾 JSON 围栏块）写入上述路径；完成时按运行协议"
            "输出 done JSON（status / deliverable=交付物绝对路径 / notes"
            " 三字段）。")


def _translate_prompt(ws: Path, exp_dir: Path, module: str, mod_json: dict,
                      spec_files: list[tuple[Path, int]], ledger: dict,
                      manifest: dict, proj: dict, target_os: Path,
                      driver_home_rel: str, deps: list[str],
                      parking_text: str, registry_text: str,
                      deliverable_text: str) -> str:
    skill = agent.load_skill(SKILL_TRANSLATE)
    home = target_os / driver_home_rel
    ext = manifest.get("source_ext")
    integ = manifest.get("integration") or {}
    ts = manifest.get("test_substrate") or {}
    marker = ts.get("marker") or "（数据面未提供——单测标记判定降级）"
    how = ts.get("how") or "（数据面未提供惯例说明）"
    reg_lines = []
    if integ.get("list_file") and integ.get("entry_template"):
        reg_lines = [
            f"- 构建单元登记：清单文件 `{home / integ['list_file']}`；"
            f"登记行模板 `{integ['entry_template']}`（`{{stem}}` = 文件名"
            "去扩展名；新文件必须按模板登记，否则不进入构建）"]
    wiring = [str(p) for p in (manifest.get("commit_paths") or [])]
    dep_lines = _dep_lines(ledger, deps)
    spec_listing = "\n".join(f"  - `{p}`（{n} 行）"
                             for p, n in spec_files)
    existing = "、".join(f"`{p.name}`"
                         for p in _driver_files(home, ext)) or "（空）"
    return (f"{skill}\n\n---\n\n## 背景数据（平台事实，以数据面为准）\n"
            f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
            f"- 驱动落点 driver_home：`{home}"
            f"（相对目标树 `{driver_home_rel}`）\n"
            f"- 源文件扩展名：`{ext}`（登记与测试标记扫描按此过滤）\n"
            "- 接线白名单（driver_home 之外允许改动的全部文件，"
            "无则不得越界改动）："
            + ("、".join(f"`{p}`" for p in wiring) or "（无）") + "\n"
            + "\n".join(reg_lines)
            + ("\n" if reg_lines else "")
            + f"- Linux 参考源树（只读规格）：`{proj.get('linux_driver')}`\n"
            "- 契约登记表（跨模块共享签名，全局约束）：\n\n"
            f"{registry_text.strip() or '（空）'}\n\n"
            "- 泊车记录（需平台侧能力，交人工处理；既有泊车处置必须遵守）：\n\n"
            f"{parking_text.strip() or '（空）'}\n\n"
            f"## 本模块研究交付物（最高优先级输入，先逐节消费再动笔）\n\n"
            f"{deliverable_text.strip() or '（空——研究交付物缺失，兜底研究权自研并上报）'}\n\n"
            f"## 本模块任务\n"
            f"- 模块：`{module}` —— {mod_json.get('function', '')}\n"
            f"- 模块规格文件（逐个精读后翻译）：\n{spec_listing}\n"
            f"- 依赖模块已迁产物（**只追加、不得改动**）：\n"
            + ("\n".join(dep_lines) if dep_lines else "  （无依赖模块）")
            + f"\n- driver_home 现有文件（追加式约束，不动既有内容）："
            f"{existing}\n\n"
            f"## 测试与记账契约（执行式优先，不设个数指标）\n"
            f"- 测试标记：`{marker}`——执行式测试须带此标记，编排器按"
            "模块前后计数差核对声明\n"
            f"- 基质惯例：{how}\n"
            "- 记账三字段（done JSON 必填，缺一不可）：\n"
            "  - `migrated_functions`：本模块迁移的**有真实行为、可验证"
            "的单元**清单（函数为主；纯定义内容可为可断言的类型布局/"
            "常量组）\n"
            "  - `tests`：`[{\"fn\",\"aspect\",\"name\"}]`——每个条目 = "
            "一个真实落地的**执行式**测试（调用被测单元并断言行为；"
            "期望值须引用源规格行号）；`name` 与产物中的测试名一致\n"
            "  - `untested`：`[{\"fn\",\"reason\"}]`——迁移了但不测的"
            "单元与理由（trivial 透传/平台绑定/纯声明）\n"
            "- 硬要求 = 记账完备（migrated ⊆ tests ∪ untested，同一单元"
            "不双挂）+ 声明的每个测试真实存在（编排器按标记计数核对）；"
            "执行式优先与期望值源行号锚是质量要求（SKILL 纪律四），"
            "数量本身不是指标，覆盖台账交人审\n\n"
            f"## 任务\n按 SKILL 纪律、以研究交付物为准把本模块规格翻译进"
            " driver_home（新文件名贴近模块职责，登记后纳入构建）；构建与"
            "单测由外部执行——需要验证时按运行协议请求静态段；完成时输出"
            " done JSON。")


# ---------- ledger / 报告 ----------

def _load_ledger(exp_dir: Path) -> dict:
    led = _read_json(exp_dir / "ledger.json")
    modules = (led or {}).get("modules")
    return {"modules": modules} if isinstance(modules, dict) else \
        {"modules": {}}


def _save_ledger(exp_dir: Path, ledger: dict) -> None:
    (exp_dir / "ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _write_report(ws: Path, exp_dir: Path, ledger: dict, order: list[str],
                  proj: dict, manifest: dict,
                  terminal: dict | None = None) -> None:
    rows = ["| 模块 | 状态 | 研究 | 源行数 | 预算s | 轮数 | agent时长s | "
            "墙钟s | 声明单元 | 已测 | 豁免 | 标记增量 | commit |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    detail = []
    for m in order:
        e = (ledger.get("modules") or {}).get(m) or {}
        res = e.get("research") or {}
        commit = e.get("commit")
        ch = commit[0][:10] if isinstance(commit, list) and commit else \
            (str(commit)[:10] if commit else "—")
        migrated = e.get("migrated_functions") or []
        tests = e.get("tests") or []
        untested = e.get("untested") or []
        research_cell = "—"
        if res:
            hv = res.get("harvest") or {}
            research_cell = (f"{res.get('status', '未跑')}"
                             f"（{res.get('agent_sec', '—')}s，词典+"
                             f"{hv.get('dictionary', 0)}/契约+"
                             f"{hv.get('contracts', 0)}）")
        rows.append(
            f"| {m} | {e.get('status', '未跑')} | {research_cell} | "
            f"{e.get('loc', '—')} | "
            f"{e.get('budget_sec', '—')} | {e.get('rounds', '—')} | "
            f"{e.get('agent_sec', '—')} | {e.get('wall_sec', '—')} | "
            f"{len(migrated)} | {len(tests)} | {len(untested)} | "
            f"{e.get('marker_delta', '—')} | {ch} |")
        if e.get("status") == "pass" or tests or untested:
            detail.append(f"### {m}（{e.get('status', '—')}）\n")
            if migrated:
                detail.append("- 迁移单元："
                              + "、".join(str(f) for f in migrated))
            if tests:
                detail.append("- 已测（执行式）：")
                detail.extend(f"  - `{t.get('fn')}` —— {t.get('aspect')}"
                              f"（测试 `{t.get('name')}`）"
                              for t in tests if isinstance(t, dict))
            if untested:
                detail.append("- 豁免（不测，理由供人审）：")
                detail.extend(f"  - `{u.get('fn')}` —— {u.get('reason')}"
                              for u in untested if isinstance(u, dict))
            detail.append("")
    identity = manifest.get("driver") or _scope.driver_name_of(proj)
    term_lines = ["（未到达——order 未全 pass 或中途停车）"]
    if terminal:
        term_lines = [f"- 全量单测：{'PASS' if terminal.get('ut_ok') else 'FAIL'}"
                      f" —— {terminal.get('ut_detail', '')}",
                      f"- 启动冒烟（非阻断留档）："
                      f"{'PASS' if (terminal.get('boot') or {}).get('ok') else 'FAIL'}"
                      f" —— {(terminal.get('boot') or {}).get('detail', '—')}"]
    body = (
        "# exp-mono 迁移报告\n\n"
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}\n"
        f"- 驱动身份：{identity}\n"
        f"- 目标树：`{proj.get('target_os')}`（driver_home="
        f"`{manifest.get('driver_home')}`）\n"
        f"- Linux 参考树：`{proj.get('linux_driver')}`\n"
        f"- 进度：{sum(1 for m in order if (ledger.get('modules') or {}).get(m, {}).get('status') == 'pass')}"
        f"/{len(order)} 模块 pass\n"
        "- 测试口径：不设个数指标；记账完备（migrated ⊆ tests ∪ "
        "untested）+ 声明与标记计数一致性已由编排器机械核对，"
        "覆盖质量（行为面选取/豁免理由/期望值证据链）由人审下表。\n\n"
        "## 模块汇总\n\n" + "\n".join(rows) + "\n\n"
        "## 函数覆盖台账\n\n"
        + ("\n".join(detail) if detail else "（尚无模块记账）") + "\n\n"
        "## 终局验证\n\n" + "\n".join(f"- {ln}" for ln in term_lines) + "\n")
    (exp_dir / "report.md").write_text(body, encoding="utf-8")


# ---------- 任务执行 ----------

def _run_research(ws: Path, exp_dir: Path, module: str, proj: dict,
                  manifest: dict, deps: dict, ledger: dict,
                  budget_override: int | None = None,
                  session_override: str | None = None,
                  model: str | None = None) -> int:
    mdir = ws / "P1" / "modules" / module
    mod_json = _read_json(mdir / "module.json") or {}
    spec_files = sorted(p for p in mdir.iterdir()
                        if p.is_file() and p.suffix in (".c", ".h"))
    loc = sum(_text_lines(p) for p in spec_files)
    target_os = Path(proj["target_os"])
    driver_home_rel = str(manifest["driver_home"])
    research_dir = exp_dir / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    deliverable = research_dir / f"{module}.md"
    budget = int(budget_override) if budget_override else _budget_research(loc)

    def _read(name: str) -> str:
        return (exp_dir / name).read_text(encoding="utf-8",
                                          errors="replace")

    prompt = _research_prompt(
        ws, exp_dir, module, mod_json,
        [(p, _text_lines(p)) for p in spec_files], ledger, manifest, proj,
        target_os, driver_home_rel, deps.get(module) or [],
        _read("mapping-notes.md"), _read("parking.md"),
        _read("contracts.md") if (exp_dir / "contracts.md").exists() else "",
        deliverable)
    _log.console_line(f"[porter] exp-mono: 研究模块 {module}"
                      f"（源 {loc} 行，预算 {budget}s）")
    t0 = time.time()
    session = session_override
    attempts = 0
    outcome: dict = {}
    verdict: dict | None = None
    problems: list[str] = []
    while True:
        attempts += 1
        outcome = agent.run_agent_seq(
            prompt, workdir=target_os,
            log_stem=str(exp_dir / "logs" / f"RES_{module}"),
            static=None,
            gen_schema={"status": "str", "deliverable": "str",
                        "notes": "str"},
            agent_budget_sec=budget,
            model=model,
            resume_session=session,
            task={"phase": "exp-mono", "module": module, "step": "research",
                  "task_id": f"exp-mono.research.{module}"})
        session = outcome.get("session_id") or session
        parsed = outcome.get("parsed") or {}
        if outcome.get("status") != "done" or parsed.get("status") == "blocked":
            break
        if deliverable.exists():
            verdict = _validate_research(
                deliverable.read_text(encoding="utf-8", errors="replace"),
                module, target_os, driver_home_rel, ws)
            problems = verdict["problems"]
        else:
            problems = [f"交付物文件不存在：{deliverable}"]
        if not problems:
            break
        if attempts > RESEARCH_RETRIES:
            break
        _log.console_line(f"[porter] exp-mono: {module} 研究交付物校验"
                          f"未过（第 {attempts} 次）——同 session 回灌修")
        prompt = ("## 研究交付物校验未通过\n"
                  + "\n".join(f"- {p}" for p in problems)
                  + f"\n请修复交付物文件 `{deliverable}`（只修上述问题，"
                  "不要重做已完成的研究），然后按运行协议重新输出 done "
                  "JSON。")
    wall = round(time.time() - t0, 1)
    parsed = outcome.get("parsed") or {}
    blocked = parsed.get("status") == "blocked"
    ok = verdict is not None and verdict["ok"] \
        and outcome.get("status") == "done"
    harvest: dict = {}
    if ok and verdict:
        harvest = _harvest_research(exp_dir, module, verdict["data"])
    entry = ledger.setdefault("modules", {}).setdefault(module, {})
    entry["research"] = {
        "status": "pass" if ok else ("blocked" if blocked else (
            "invalid-deliverable"
            if outcome.get("status") == "done" and problems
            else outcome.get("status"))),
        "loc": loc, "budget_sec": budget,
        "rounds": len(outcome.get("rounds") or []),
        "agent_sec": outcome.get("total_agent_sec"),
        "wall_sec": wall,
        "deliverable": str(deliverable),
        "validate_attempts": attempts,
        "warnings": (verdict or {}).get("warnings") or [],
        "problems": problems if not ok else [],
        "harvest": harvest,
        "session_id": session,
        "seq_status": outcome.get("status"),
        "time": datetime.now().isoformat(timespec="seconds")}
    _save_ledger(exp_dir, ledger)
    if ok:
        _log.console_line(f"[porter] exp-mono: {module} 研究 PASS"
                          f"（agent {entry['research']['agent_sec']}s，"
                          f"墙钟 {wall}s，收割 词典+{harvest.get('dictionary', 0)}"
                          f"/契约+{harvest.get('contracts', 0)}"
                          f"/泊车+{harvest.get('parking', 0)}）")
    elif blocked:
        entry["research"]["notes"] = str(parsed.get("notes", ""))[:400]
        _log.console_line(f"[porter] exp-mono: {module} 研究被报 blocked："
                          f"{entry['research']['notes']}——停车 rc 1")
    else:
        _log.console_line(f"[porter] exp-mono: {module} 研究未通过"
                          f"（{problems[:2] or outcome.get('status')}）"
                          "——停车 rc 1，人工介入后重跑（ledger 断点续）")
    return 0 if ok else 1


def _run_translate(ws: Path, exp_dir: Path, module: str, proj: dict,
                   runner: dict, manifest: dict, deps: dict,
                   ledger: dict, budget_override: int | None = None,
                   session_override: str | None = None,
                   model: str | None = None) -> int:
    mdir = ws / "P1" / "modules" / module
    mod_json = _read_json(mdir / "module.json") or {}
    spec_files = sorted(p for p in mdir.iterdir()
                        if p.is_file() and p.suffix in (".c", ".h"))
    loc = sum(_text_lines(p) for p in spec_files)
    target_os = Path(proj["target_os"])
    driver_home_rel = str(manifest["driver_home"])
    home = target_os / driver_home_rel
    marker = (manifest.get("test_substrate") or {}).get("marker")
    ext = manifest.get("source_ext") or None
    notes_path = exp_dir / "mapping-notes.md"
    parking_path = exp_dir / "parking.md"
    notes_before = _text_lines(notes_path)
    deliverable = exp_dir / "research" / f"{module}.md"
    deliverable_text = ""
    if deliverable.exists():
        deliverable_text = deliverable.read_text(encoding="utf-8",
                                                 errors="replace")
    else:
        _log.console_line(f"[porter] exp-mono: {module} 研究交付物缺失"
                          f"（{deliverable}）——翻译以兜底研究权自研并上报")
    registry_text = ""
    if (exp_dir / "contracts.md").exists():
        registry_text = (exp_dir / "contracts.md").read_text(
            encoding="utf-8", errors="replace")
    budget = int(budget_override) if budget_override else _budget_sec(loc)
    prev = (ledger.get("modules") or {}).get(module) or {}
    # 续跑基线重建：delta 语义跨调用累计——基线应回溯到**首次**开始该
    # 模块时的快照（存于上次 ledger 条目），而非本次调用起点（否则
    # resume 时已完成工作被当零点，增量/marker 守卫全部误杀）。
    # 旧条目无 snap_base 时：marker 可由 snap_end-marker_delta 反推；
    # 代码基线不可知 → 本轮禁用增量守卫（build/ut/记账仍守）。
    cur_code = _count_code(home, ext)
    cur_marker = _count_marker(home, ext, marker)
    base_code: int | None = None
    base_marker: int | None = None
    if session_override and prev.get("status") not in (None, "pass"):
        sb = prev.get("snap_base") or {}
        if sb.get("marker") is not None:
            base_marker = sb["marker"]      # 新式条目：精确基线
        elif prev.get("marker_delta") is not None:
            base_marker = 0     # 旧式条目：绝对基线不可知——保守取 0
            #（方向安全：宁可高估 delta 也不误杀；骨架自带 marker 的
            #  少许膨胀由记账完备性另一侧约束）
        if sb.get("code_lines") is not None:
            base_code = sb["code_lines"]
    snap = {"code_lines": base_code if base_code is not None else cur_code,
            "marker": base_marker if base_marker is not None else cur_marker,
            "growth_guard": base_code is not None or not session_override,
            "status": _git_status(target_os)}
    prompt = _translate_prompt(
        ws, exp_dir, module, mod_json,
        [(p, _text_lines(p)) for p in spec_files], ledger, manifest, proj,
        target_os, driver_home_rel, deps.get(module) or [],
        parking_path.read_text(encoding="utf-8", errors="replace"),
        registry_text, deliverable_text)
    if session_override:
        if prev.get("status") not in (None, "pass"):
            resume_bits = [f"- 上次状态：{prev.get('status')}"]
            if prev.get("decl_problems"):
                resume_bits.append(
                    "- 上次 done 的声明面核对未通过（须补全记账或补测试）："
                    + "；".join(prev["decl_problems"]))
            if prev.get("notes"):
                resume_bits.append(f"- 上次 notes：{prev['notes']}")
            prompt += ("\n\n---\n\n## 续跑指令（同会话续接，产物已在树中）\n"
                       + "\n".join(resume_bits)
                       + "\n请在此基础上修复/补全（**不要重做已完成的"
                       "工作**；交付物裁定/登记/测试已就位的直接复用），按"
                       "运行协议输出完整 done JSON（六字段：status/files/"
                       "notes/migrated_functions/tests/untested）。")
    _log.console_line(f"[porter] exp-mono: 翻译模块 {module}"
                      f"（源 {loc} 行，预算 {budget}s，起始测试标记 "
                      f"{snap['marker']}）")
    gate = _make_gate(exp_dir, target_os, runner, manifest, module, loc,
                      driver_home_rel, snap)
    t0 = time.time()
    session = session_override
    outcome: dict = {}
    parsed: dict = {}
    decl_retries = 0
    total_agent_sec = 0.0
    total_rounds = 0
    decl_probs: list[str] = []
    while True:
        outcome = agent.run_agent_seq(
            prompt, workdir=target_os,
            log_stem=str(exp_dir / "logs" / f"MOD_{module}"),
            static=gate,
            gen_schema={"status": "str", "files": "list", "notes": "str",
                        "migrated_functions": "list", "tests": "list",
                        "untested": "list"},
            final_static=True,
            agent_budget_sec=budget,
            model=model,
            resume_session=session,
            fix_floor_sec=FIX_FLOOR_SEC,
            stall_meta_rounds=STALL_META_ROUNDS,
            task={"phase": "exp-mono", "module": module, "step": "translate",
                  "task_id": f"exp-mono.translate.{module}"})
        session = outcome.get("session_id") or session
        total_agent_sec += outcome.get("total_agent_sec") or 0.0
        total_rounds += len(outcome.get("rounds") or [])
        parsed = outcome.get("parsed") or {}
        blocked = parsed.get("status") == "blocked"
        ok = outcome.get("status") == "done" and not blocked
        marker_delta = (_count_marker(home, ext, marker) - snap["marker"]) \
            if marker else None
        decl_probs = []
        if ok:
            decl_probs = _declaration_problems(parsed, marker_delta, marker)
            if decl_probs:
                ok = False
        if ok or decl_retries >= DECL_RETRIES or blocked \
                or outcome.get("status") != "done":
            break
        # 声明面核对不过 → 同 session 自动重试（实证 14/14 一轮自修；
        # 进程内闭环消灭最大类人工介入）
        decl_retries += 1
        _log.console_line(f"[porter] exp-mono: {module} 声明面记账不完备"
                          f"——自动重试第 {decl_retries}/{DECL_RETRIES} 次")
        prompt = ("## 上次 done 的声明面核对未通过\n"
                  + "\n".join(f"- {p}" for p in decl_probs)
                  + "\n请**只补记账或补测试**（不要重做已完成的迁移），"
                  "然后按运行协议重新输出完整 done JSON（六字段：status/"
                  "files/notes/migrated_functions/tests/untested）。")
    wall = round(time.time() - t0, 1)
    blocked = parsed.get("status") == "blocked"
    ok = outcome.get("status") == "done" and not blocked and not decl_probs
    entry = ledger.setdefault("modules", {}).setdefault(module, {})
    entry.update({
        "status": "pass" if ok else (blocked and "blocked")
        or ("decl-mismatch" if decl_probs else outcome.get("status")),
        "loc": loc, "budget_sec": budget,
        "rounds": total_rounds,
        "agent_sec": round(total_agent_sec, 1),
        "wall_sec": wall,
        "dict_delta": _text_lines(notes_path) - notes_before,
        "files": parsed.get("files") or [],
        "migrated_functions": parsed.get("migrated_functions") or [],
        "tests": parsed.get("tests") or [],
        "untested": parsed.get("untested") or [],
        "marker_delta": marker_delta,
        "session_id": outcome.get("session_id"),
        "seq_status": outcome.get("status"),
        "time": datetime.now().isoformat(timespec="seconds")})
    if decl_retries:
        entry["decl_retries"] = decl_retries
    if decl_probs:
        entry["decl_problems"] = decl_probs
    entry["snap_base"] = {"code_lines": snap["code_lines"],
                          "marker": snap["marker"]}
    entry["snap_end"] = {"code_lines": _count_code(home, ext),
                         "marker": _count_marker(home, ext, marker)
                         if marker else None}
    # notes 持久化：矛盾上报/兜底裁定/泊车遵守是翻译者唯一的口头
    # 知识通道——不只 blocked，done 也要留档（防蒸发，人审 ledger
    # /report 消化）
    if str(parsed.get("notes", "")).strip():
        entry["notes"] = str(parsed.get("notes", ""))[:400]
    if blocked:
        _log.console_line(f"[porter] exp-mono: {module} 被 agent 报 blocked："
                          f"{entry.get('notes', '')}——停车（交付物/平台"
                          "能力问题走人工，不硬编）")
    if ok:
        try:
            from ..common import vcs as _vcs
            entry["commit"] = _vcs.commit_target(
                ws, f"exp-mono({module}): {module} migrated "
                f"(build+ut green)",
                paths=[driver_home_rel, *(manifest.get("commit_paths")
                                          or [])],
                phase="exp-mono") or []
        except Exception as ex:
            _log.console_line(f"[porter] exp-mono: 目标树 commit 失败"
                              f"（{ex!r}）——ledger 照记 pass")
        _log.console_line(f"[porter] exp-mono: {module} PASS"
                          f"（轮数 {entry['rounds']}，agent "
                          f"{entry['agent_sec']}s，墙钟 {wall}s）")
    else:
        _log.console_line(f"[porter] exp-mono: {module} 未通过"
                          f"（seq={entry['seq_status']}）——停车 rc 1，"
                          "人工介入后重跑（ledger 断点续）")
    _save_ledger(exp_dir, ledger)
    return 0 if ok else 1


# ---------- 终局 ----------

def _terminal(ws: Path, exp_dir: Path, runner: dict, proj: dict,
              manifest: dict) -> dict:
    target_os = Path(proj["target_os"])
    driver_home_rel = str(manifest["driver_home"])
    ut_ok, ut_detail, _lp = _run_ut(exp_dir, target_os, runner,
                                    driver_home_rel, label="final_ut_full",
                                    which="full")
    try:
        boot = probe_mod.probe_boot(exp_dir, target_os, runner,
                                    label="final_boot")
    except Exception as ex:
        boot = {"ok": False, "detail": f"boot 冒烟异常：{ex!r}"}
    _log.console_line(f"[porter] exp-mono: 终局全量单测 "
                      f"{'PASS' if ut_ok else 'FAIL'}；启动冒烟（非阻断）"
                      f" {'PASS' if boot.get('ok') else 'FAIL'}")
    return {"ut_ok": ut_ok, "ut_detail": ut_detail, "boot": boot}


# ---------- 主入口 ----------

def run_exp_mono(ws: Path, module: str | None = None,
                 budget: int | None = None,
                 session: str | None = None,
                 budget_research: int | None = None,
                 budget_translate: int | None = None,
                 module_research: str | None = None) -> int:
    ws = Path(ws).resolve()
    research_only = module_research is not None
    needs = ["project.json", "runner.json", "P1/modules/deps.json",
             "P2/reports/scaffold_manifest.json"]
    missing = [n for n in needs if not (ws / n).exists()]
    if missing:
        _log.console_line(f"[porter] exp-mono: 前置缺失："
                          + "、".join(missing) + "——rc 2")
        return 2
    models = _load_models()
    if models[0] is None:
        _log.console_line(f"[porter] exp-mono: {models[1]}——rc 2")
        return 2
    model_research, model_coding = models
    proj = _read_json(ws / "project.json") or {}
    runner = _read_json(ws / "runner.json") or {}
    deps = _read_json(ws / "P1" / "modules" / "deps.json") or {}
    manifest = _read_json(ws / "P2" / "reports" /
                          "scaffold_manifest.json") or {}
    order = deps.get("order") or []
    if not order or not manifest.get("driver_home"):
        _log.console_line("[porter] exp-mono: deps.json 无 order 或 "
                          "manifest 无 driver_home——rc 2")
        return 2
    if module and module not in order:
        _log.console_line(f"[porter] exp-mono: 模块 {module} 不在 order 中"
                          "——rc 2")
        return 2
    if module_research is not None and module_research not in order:
        _log.console_line(f"[porter] exp-mono: 模块 {module_research} 不在 "
                          "order 中——rc 2")
        return 2
    exp_dir = ws / "exp-mono"
    (exp_dir / "logs").mkdir(parents=True, exist_ok=True)
    (exp_dir / "research").mkdir(parents=True, exist_ok=True)
    ledger = _load_ledger(exp_dir)
    for name, text in (
            ("ledger.json", None),
            ("mapping-notes.md",
             "# 迁移词典（JIT 接力）\n\n"
             "> 每条一行：`符号 | verdict | 目标用法 | 证据 file:line`。\n"
             "> 研究任务产出裁定、编排器收割追加；后继研究先读再用。\n"),
            ("parking.md",
             "# 泊车记录\n\n"
             "> 需要平台侧能力/人工裁定的条目记在此（条目 + 原因 + "
             "临时处置）；编排器每模块把现内容注入 prompt。\n"),
            ("contracts.md",
             "# 契约登记表\n\n"
             "> 跨模块共享的类型/钩子签名——研究任务提案、机器收割"
             "追加。后继模块研究前必查：新契约若与既有条目冲突，在"
             "交付物 conflicts_with 显式标记。\n")):
        p = exp_dir / name
        if not p.exists():
            if name == "ledger.json":
                _save_ledger(exp_dir, ledger)
            else:
                p.write_text(text, encoding="utf-8")
    edges = deps.get("edges") or {}
    if module:
        unmet = [d for d in edges.get(module) or []
                 if (ledger["modules"].get(d) or {}).get("status") != "pass"]
        if unmet:
            _log.console_line(f"[porter] exp-mono: {module} 的依赖未全部 "
                              f"pass（缺 {'、'.join(unmet)}）——只允许按序"
                              "迁移，rc 2")
            return 2
        targets = [module]
    elif research_only:
        targets = [module_research]
    else:
        targets = list(order)
    _log.console_line(f"[porter] exp-mono: 模型分层 reasoning="
                      f"{model_research} coding={model_coding}")
    first_session = session          # 会话续接只作用于本次实际运行的
    stop = False                     # 首个任务（后续任务各自新会话）
    for m in targets:
        entry = ledger["modules"].get(m) or {}
        res_status = (entry.get("research") or {}).get("status")
        # 研究续跑规则：仅当上次研究尝试过且未过（预算杀/校验败）才
        # 续接会话；研究从未跑过 → session 留给翻译任务（保留旧翻译
        # 断点续语义）
        research_session = first_session \
            if (first_session and res_status not in (None, "pass")) else None
        if res_status != "pass":
            if _run_research(ws, exp_dir, m, proj, manifest, edges,
                             ledger, budget_override=budget_research,
                             session_override=research_session,
                             model=model_research) != 0:
                _write_report(ws, exp_dir, ledger, order, proj, manifest)
                return 1
            if research_session is not None:
                first_session = None     # 已被研究续跑消费
        else:
            _log.console_line(f"[porter] exp-mono: {m} 研究已 pass——跳过")
        if research_only:
            _log.console_line(f"[porter] exp-mono: {m} 仅研究模式——"
                              "跳过翻译")
            continue
        if (ledger["modules"].get(m) or {}).get("status") == "pass":
            _log.console_line(f"[porter] exp-mono: {m} 已 pass——跳过")
            first_session = None
            continue
        if _run_translate(ws, exp_dir, m, proj, runner, manifest, edges,
                          ledger,
                          budget_override=budget_translate
                          or budget,
                          session_override=first_session,
                          model=model_coding) != 0:
            stop = True
            break
        first_session = None
    if stop:
        _write_report(ws, exp_dir, ledger, order, proj, manifest)
        return 1
    terminal = None
    if not research_only and all(
            (ledger["modules"].get(m) or {}).get("status") == "pass"
            for m in order):
        terminal = _terminal(ws, exp_dir, runner, proj, manifest)
    _write_report(ws, exp_dir, ledger, order, proj, manifest, terminal)
    done = sum(1 for m in order
               if (ledger["modules"].get(m) or {}).get("status") == "pass")
    _log.console_line(f"[porter] exp-mono: 结束（{done}/{len(order)} pass）")
    if terminal and not terminal["ut_ok"]:
        return 1
    return 0
