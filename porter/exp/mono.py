"""mono.py — exp-mono：单体模块迁移 loop（实验子命令，直连 P1 结尾）。

形态：一个迁移者 agent 一次迭代迁一个模块（研究+翻译一体）；不做
映射/探针/判据等中间层——这正是实验点（验证单体 agent 形态）。平台
事实全部来自工作区数据面（runner.json / scaffold manifest /
module.json），本文件零目标 OS / 目标语言假设。

每模块流程：
  1. 幂等：exp-mono/ledger.json 里该模块 pass → 跳过
  2. prompt = SKILL + 模块上下文（职责/规格文件/依赖模块已迁产物）
             + 迁移词典现内容（接力）+ 泊车记录 + Linux 参考树
  3. run_agent_seq：同 session 多轮；静态段 = 三段复合 gate
     （① 产物守卫 ② 构建 ③ 单测），便宜先行失败短路，结果回灌
  4. done + 三段绿 → ledger=pass + 目标树按白名单 commit；
     blocked / 失败 / 预算耗尽 → 停车 rc 1（ledger 断点续）

预算（秒）= clamp(900, 模块源行数×1.3, 4200)。
终局（order 全 pass）：全量单测（阻断）+ 启动冒烟（非阻断留档）
+ report.md。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ..common import agent
from ..common import scope as _scope
from ..env import probe as probe_mod
from .. import log as _log

SKILL_NAME = "EXP-mono-migrate"
GATE_DESC = "三段复合静态检查（① 产物守卫 ② 构建 ③ 单测）"
_COMMENT_PREFIXES = ("//", "/*", "*", "*/", "#")


# ---------- 通用小件 ----------

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _budget_sec(loc: int) -> int:
    """行数驱动的 agent 预算（静态段时长在预算之外）。"""
    return max(900, min(4200, int(loc * 1.3)))


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


# ---------- 三段复合 gate ----------

def _guard_products(home: Path, driver_home_rel: str, manifest: dict,
                    snap: dict, loc: int, target_root: Path) -> list[str]:
    """① 产物守卫：代码增量 + 构建单元登记 + 改动范围。"""
    problems: list[str] = []
    ext = manifest.get("source_ext") or None
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
    cmd = (str(tpl)
           .replace("{PORTER_TARGET_OS_ROOT}", str(target_os.resolve()))
           .replace("{PORTER_DRIVER_HOME}", driver_home_rel))
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
        ut_ok, ut_detail, _lp = _run_ut(exp_dir, target_os, runner,
                                        driver_home_rel,
                                        label=f"exp_{module}_ut")
        if not ut_ok:
            return False, f"③ 单测 FAIL：{ut_detail}"
        return True, ("三段全绿：产物守卫通过；构建成功；单测通过"
                      "（测试记账完备性在 done 后由编排器核对声明面）")

    return {"describe": GATE_DESC, "fn": _fn}


# ---------- prompt 组装 ----------

def _module_prompt(ws: Path, exp_dir: Path, module: str, mod_json: dict,
                   spec_files: list[tuple[Path, int]], ledger: dict,
                   manifest: dict, proj: dict, target_os: Path,
                   driver_home_rel: str, deps: list[str],
                   notes_text: str, parking_text: str) -> str:
    skill = agent.load_skill(SKILL_NAME)
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
    dep_lines = []
    for d in deps:
        entry = (ledger.get("modules") or {}).get(d) or {}
        files = entry.get("files") or []
        dep_lines.append(f"- {d}：" + ("、".join(f"`{f}`" for f in files)
                                       if files else "（无登记产物清单）"))
    spec_listing = "\n".join(f"  - `{p}`（{n} 行）"
                             for p, n in spec_files)
    existing = "、".join(f"`{p.name}`"
                         for p in _driver_files(home, ext)) or "（空）"
    return (f"{skill}\n\n---\n\n## 背景数据（平台事实，以数据面为准）\n"
            f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
            f"- 驱动落点 driver_home：`{home}`"
            f"（相对目标树 `{driver_home_rel}`）\n"
            f"- 源文件扩展名：`{ext}`（登记与测试标记扫描按此过滤）\n"
            + "\n".join(reg_lines)
            + ("\n" if reg_lines else "")
            + f"- Linux 参考源树（只读规格）：`{proj.get('linux_driver')}`\n"
            f"- 迁移词典（接力，先读再增补）："
            f"`{exp_dir / 'mapping-notes.md'}` 现内容：\n\n"
            f"{notes_text.strip() or '（空——你是第一位迁移者，从零建词典）'}\n\n"
            f"- 泊车记录（需平台侧能力，交人工处理）："
            f"`{exp_dir / 'parking.md'}` 现内容：\n\n"
            f"{parking_text.strip() or '（空）'}\n\n"
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
            "不双挂）+ 声明的每个测试真实存在 + 执行式优先；数量本身"
            "不是指标，覆盖台账交人审\n\n"
            f"## 任务\n按 SKILL 纪律把本模块规格翻译进 driver_home"
            "（新文件名贴近模块职责，登记后纳入构建）；构建与单测由外部"
            "执行——需要验证时按运行协议请求静态段；完成时输出 done JSON。")


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
    rows = ["| 模块 | 状态 | 源行数 | 预算s | 轮数 | agent时长s | 墙钟s | "
            "声明单元 | 已测 | 豁免 | 标记增量 | 词典增量行 | commit |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    detail = []
    for m in order:
        e = (ledger.get("modules") or {}).get(m) or {}
        commit = e.get("commit")
        ch = commit[0][:10] if isinstance(commit, list) and commit else \
            (str(commit)[:10] if commit else "—")
        migrated = e.get("migrated_functions") or []
        tests = e.get("tests") or []
        untested = e.get("untested") or []
        rows.append(
            f"| {m} | {e.get('status', '未跑')} | {e.get('loc', '—')} | "
            f"{e.get('budget_sec', '—')} | {e.get('rounds', '—')} | "
            f"{e.get('agent_sec', '—')} | {e.get('wall_sec', '—')} | "
            f"{len(migrated)} | {len(tests)} | {len(untested)} | "
            f"{e.get('marker_delta', '—')} | {e.get('dict_delta', '—')} | "
            f"{ch} |")
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


# ---------- 单模块执行 ----------

def _run_module(ws: Path, exp_dir: Path, module: str, proj: dict,
                runner: dict, manifest: dict, deps: dict,
                ledger: dict, budget_override: int | None = None,
                session_override: str | None = None) -> int:
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
    budget = int(budget_override) if budget_override else _budget_sec(loc)
    snap = {"code_lines": _count_code(home, ext),
            "marker": _count_marker(home, ext, marker),
            "status": _git_status(target_os)}
    prompt = _module_prompt(
        ws, exp_dir, module, mod_json,
        [(p, _text_lines(p)) for p in spec_files], ledger, manifest, proj,
        target_os, driver_home_rel, deps.get(module) or [],
        notes_path.read_text(encoding="utf-8", errors="replace"),
        parking_path.read_text(encoding="utf-8", errors="replace"))
    if session_override:
        prev = (ledger.get("modules") or {}).get(module) or {}
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
                       "工作**；词典/登记/测试已就位的直接复用），按运行"
                       "协议输出完整 done JSON（六字段：status/files/notes/"
                       "migrated_functions/tests/untested）。")
    _log.console_line(f"[porter] exp-mono: 迁移模块 {module}"
                      f"（源 {loc} 行，预算 {budget}s，起始测试标记 "
                      f"{snap['marker']}）")
    gate = _make_gate(exp_dir, target_os, runner, manifest, module, loc,
                      driver_home_rel, snap)
    t0 = time.time()
    outcome = agent.run_agent_seq(
        prompt, workdir=target_os,
        log_stem=str(exp_dir / "logs" / f"MOD_{module}"),
        static=gate,
        gen_schema={"status": "str", "files": "list", "notes": "str",
                    "migrated_functions": "list", "tests": "list",
                    "untested": "list"},
        final_static=True,
        agent_budget_sec=budget,
        resume_session=session_override,
        task={"phase": "exp-mono", "module": module, "step": "migrate",
              "task_id": f"exp-mono.module.{module}"})
    wall = round(time.time() - t0, 1)
    parsed = outcome.get("parsed") or {}
    blocked = parsed.get("status") == "blocked"
    ok = outcome.get("status") == "done" and not blocked
    marker_delta = (_count_marker(home, ext, marker) - snap["marker"]) \
        if marker else None
    decl_probs: list[str] = []
    if ok:
        decl_probs = _declaration_problems(parsed, marker_delta, marker)
        if decl_probs:
            ok = False
            _log.console_line(f"[porter] exp-mono: {module} 声明面记账"
                              "不完备（" + "；".join(decl_probs)
                              + "）——不予 pass，停车 rc 1")
    entry = {"status": "pass" if ok else (blocked and "blocked")
             or ("decl-mismatch" if decl_probs else outcome.get("status")),
             "loc": loc, "budget_sec": budget,
             "rounds": len(outcome.get("rounds") or []),
             "agent_sec": outcome.get("total_agent_sec"),
             "wall_sec": wall,
             "dict_delta": _text_lines(notes_path) - notes_before,
             "files": parsed.get("files") or [],
             "migrated_functions": parsed.get("migrated_functions") or [],
             "tests": parsed.get("tests") or [],
             "untested": parsed.get("untested") or [],
             "marker_delta": marker_delta,
             "session_id": outcome.get("session_id"),
             "seq_status": outcome.get("status"),
             "time": datetime.now().isoformat(timespec="seconds")}
    if decl_probs:
        entry["decl_problems"] = decl_probs
    if blocked:
        entry["notes"] = str(parsed.get("notes", ""))[:400]
        _log.console_line(f"[porter] exp-mono: {module} 被 agent 报 blocked："
                          f"{entry['notes']}——停车（词典/平台能力问题"
                          "走人工，不硬编）")
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
                          f"{entry['agent_sec']}s，墙钟 {wall}s，"
                          f"词典 +{entry['dict_delta']} 行）")
    else:
        _log.console_line(f"[porter] exp-mono: {module} 未通过"
                          f"（seq={entry['seq_status']}）——停车 rc 1，"
                          "人工介入后重跑（ledger 断点续）")
    ledger.setdefault("modules", {})[module] = entry
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
                 session: str | None = None) -> int:
    ws = Path(ws).resolve()
    needs = ["project.json", "runner.json", "P1/modules/deps.json",
             "P2/reports/scaffold_manifest.json"]
    missing = [n for n in needs if not (ws / n).exists()]
    if missing:
        _log.console_line(f"[porter] exp-mono: 前置缺失："
                          + "、".join(missing) + "——rc 2")
        return 2
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
    exp_dir = ws / "exp-mono"
    (exp_dir / "logs").mkdir(parents=True, exist_ok=True)
    ledger = _load_ledger(exp_dir)
    for name, text in (
            ("ledger.json", None),
            ("mapping-notes.md",
             "# 迁移词典（JIT 接力）\n\n"
             "> 每条一行：`符号 | verdict | 目标用法 | 证据 file:line`。\n"
             "> 迁移者当场在目标树核实后追加；后继模块先读再用。\n"),
            ("parking.md",
             "# 泊车记录\n\n"
             "> 需要平台侧能力/人工裁定的条目记在此（条目 + 原因 + "
             "临时处置）；编排器每模块把现内容注入 prompt。\n")):
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
    else:
        targets = list(order)
    first_session = session          # 会话续接只作用于本次实际运行的
    for m in targets:                # 首个模块（后续模块各自新会话）
        if (ledger["modules"].get(m) or {}).get("status") == "pass":
            _log.console_line(f"[porter] exp-mono: {m} 已 pass——跳过")
            continue
        if _run_module(ws, exp_dir, m, proj, runner, manifest, edges,
                       ledger, budget_override=budget,
                       session_override=first_session) != 0:
            first_session = None
            _write_report(ws, exp_dir, ledger, order, proj, manifest)
            return 1
        first_session = None
    terminal = None
    if all((ledger["modules"].get(m) or {}).get("status") == "pass"
           for m in order):
        terminal = _terminal(ws, exp_dir, runner, proj, manifest)
    _write_report(ws, exp_dir, ledger, order, proj, manifest, terminal)
    done = sum(1 for m in order
               if (ledger["modules"].get(m) or {}).get("status") == "pass")
    _log.console_line(f"[porter] exp-mono: 结束（{done}/{len(order)} pass）")
    if terminal and not terminal["ut_ok"]:
        return 1
    return 0
