"""accept.py — exp-accept：整驱动系统验收（实验子命令，接 exp-mono 终态）。

4 层判定（全阻断）：
  L1 编译 / L2 单测 —— 命令与判据机械绑定 runner 键（冻结复用，
                       agent 不可改，origin=frozen）
  L3 启动（设备+驱动）—— 执行命令机械绑定（runner.boot /
                       inject_device 冻结模板）；驱动反应锚点由 agent
                       从迁移产物代码**引用**（cite 带树内 file:line，
                       人审，origin=proposed）
  L4 端到端 —— agent 生成（人审）：载荷样例 / 工作负载 / 收据 /
                       期望值 / 负向用例 / 脚手架接线

相位：
  --draft      P2b 直连形态（文件即信号 + 同 session 增量续接 + 同轮
               质量微反馈 + 静态证据核查回炉，零启动消耗）→ 合并冻结
               判据 → gates 审批关口 → exit 3
  （人审：answers.md `## @exp-accept.plan` + `verdict: approve`）
  --execute    幂等 prepare（脚手架落树 + 接线 + 独立 commit）→
               成本升序跑矩阵（L1 → L3 裸/注入 → L4；红集汇总后
               L2 殿后）→ 红则 infra 分类（瞬态重试 1 次）→ 自动修环
  --diagnose   断点续修（--budget/--session 续接超时会话）
  --redraft    数据面缺陷重开草案（带失败证据，须重新人审）

诚实闸门（机械，不信自报）：收据 = 内容派生（literal 或
fill_sha256 字节×长度的通用数学原语，语义活在数据里）；工作负载
运行证明行缺席即红；跨单元 must-not（防日志串判/收据泄漏）；判据
在 execute 期只读已放行版本（指纹）；脚手架对修码 agent 冻结
（漂移即还原）；git 白名单（driver_home ∪ 登记接线文件）。

本文件零目标 OS / 驱动假设：命令 / 判据 / 脚手架全部来自工作区
数据面（runner.json / acceptance.json / scaffold manifest）。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ..common import agent
from ..env import probe as probe_mod
from ..exp import mono as _mono
from .. import log as _log

SKILL_DRAFT = "EXP-accept-draft"
SKILL_FIX = "EXP-accept-fix"
GATE_ID = "exp-accept.plan"
GATE_FAIL_ID = "exp-accept.draft.fail"
DRAFT_ROUNDS = 4            # 方案回炉有界轮数（证据核查类失败消耗轮）
QUALITY_TRIES = 3           # 同轮文件质量微反馈（不烧轮）
DRAFT_BUDGET_SEC = 1800     # 草案 agent 总预算缺省
DIAG_BUDGET_SEC = 2400      # 修环 agent 总预算缺省
_CALL_CAP_SEC = 1200        # 单次 provider 调用时长上限
CRIT_LAYERS = ("L1", "L2", "L3", "L4")
FROZEN_BOOT_IDS = ("B_build", "B_bare", "B_inject", "B_unit")


def _read_json(path: Path):
    return _mono._read_json(path)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sha16(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _tree_head(tree: Path) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=60)
        return proc.stdout.strip() if proc.returncode == 0 else "nogit"
    except (OSError, subprocess.TimeoutExpired):
        return "nogit"


def _safe_rel(rel: str) -> bool:
    """树内相对路径白名单形：非绝对、无 `..` 段。"""
    if not isinstance(rel, str) or not rel.strip():
        return False
    p = Path(rel)
    if p.is_absolute() or any(seg == ".." for seg in p.parts):
        return False
    return True


# ---------- 判定核心（纯函数） ----------

def resolve_expect(exp) -> str | None:
    """期望值解析：literal 字面量；fill_sha256 = 字节×长度 的 sha256
    （通用数学原语——"应读到什么"的语义由数据声明，工具不做解读）。"""
    if not isinstance(exp, dict):
        return None
    if isinstance(exp.get("literal"), str) and exp["literal"]:
        return exp["literal"]
    fh = exp.get("fill_sha256")
    if isinstance(fh, dict):
        b, n = fh.get("byte"), fh.get("length")
        if isinstance(b, int) and not isinstance(b, bool) \
                and 0 <= b <= 255 and isinstance(n, int) \
                and not isinstance(n, bool) and n > 0:
            return hashlib.sha256(bytes([b]) * n).hexdigest()
    return None


def judge_criteria(criteria: list[dict], results: dict) -> list[dict]:
    """判据 → 判定记录。results = {boot_id: {rc, log, log_state, green}}。
    单元未运行 → ok=None（pending，不计红）。"""
    out: list[dict] = []
    for c in criteria:
        r = results.get(c.get("boot"))
        rec = {"id": c.get("id"), "layer": c.get("layer"),
               "boot": c.get("boot"), "origin": c.get("origin"),
               "ok": None, "detail": ""}
        if r is None:
            rec["detail"] = "单元未运行（pending）"
            out.append(rec)
            continue
        kind, pol = c.get("kind"), c.get("polarity")
        if kind == "rc":
            ok = (r["rc"] == 0) == (pol == "hit")
            rec.update(ok=ok, detail=f"rc={r['rc']}")
        elif kind == "log":
            hit = re.search(c.get("expr") or "", r.get("log") or "") \
                is not None
            ok = hit == (pol == "hit")
            rec.update(ok=ok, detail=f"hits={'yes' if hit else 'no'}"
                                     f" log_state={r.get('log_state')}")
        elif kind == "receipt":
            ms = list(re.finditer(c.get("expr") or "", r.get("log") or ""))
            if not ms:
                rec.update(ok=False, detail="收据行未出现")
            else:
                m = ms[-1]                     # 多次出现取末次（末位语义）
                val = (m.group(1) if m.groups() else m.group(0)).strip()
                expv = resolve_expect(c.get("expect"))
                ok = expv is not None and val == expv
                rec.update(ok=ok,
                           detail=f"receipt={val[:20]}… "
                                  f"expect={'一致' if ok else '不符'}")
        else:
            rec.update(ok=False, detail=f"未知判据 kind {kind}")
        out.append(rec)
    return out


# ---------- 草案 schema 校验（agent 部分） ----------

def _boot_log_spec_ok(b: dict) -> bool:
    if b.get("log_is_stdout") is True:
        return not b.get("log_file")
    return isinstance(b.get("log_file"), str) and bool(b["log_file"].strip())


def validate_draft(draft, runner: dict) -> list[str]:
    """草案（agent 部分）schema 校验。返回错误清单（空 = 合格）。"""
    errs: list[str] = []
    if not isinstance(draft, dict):
        return ["草案不是 JSON 对象"]

    def _s(v) -> bool:
        return isinstance(v, str) and bool(v.strip())

    # inject_args_key
    inj = runner.get("inject_device") or {}
    examples = inj.get("example_args") or {}
    iak = draft.get("inject_args_key")
    if iak is not None and (not _s(iak) or iak not in examples):
        errs.append(f"inject_args_key 非法（须为 example_args 的键："
                    f"{sorted(examples)}）")
    if inj and len(examples) > 1 and iak is None:
        errs.append("example_args 有多键——inject_args_key 必填")

    # boots（模板单元，agent 只造这类）
    boots = draft.get("boots")
    if not isinstance(boots, list) or not boots:
        errs.append("boots 须为非空数组（至少一个模板启动单元）")
        boots = []
    ids = set()
    for i, b in enumerate(boots):
        tag = f"boots[{i}]"
        if not isinstance(b, dict) or not _s(b.get("id")):
            errs.append(f"{tag}: id 缺失")
            continue
        bid = b["id"].strip()
        if bid in ids or bid in FROZEN_BOOT_IDS:
            errs.append(f"{tag}: id 重复或占用冻结名 `{bid}`")
        ids.add(bid)
        if not _s(b.get("cmd")):
            errs.append(f"{tag}({bid}): cmd 缺失")
        if not _boot_log_spec_ok(b):
            errs.append(f"{tag}({bid}): 日志定位缺失（log_file 或 "
                        "log_is_stdout=true 二选一）")
        if b.get("timeout_sec") is not None and \
                (not isinstance(b["timeout_sec"], int)
                 or isinstance(b["timeout_sec"], bool)
                 or b["timeout_sec"] <= 0):
            errs.append(f"{tag}({bid}): timeout_sec 须为正整数")
        if b.get("args") is not None and not isinstance(b["args"], str):
            errs.append(f"{tag}({bid}): args 须为字符串")

    # scaffold
    scaff = draft.get("scaffold")
    scaff_files: dict[str, str] = {}
    if scaff is not None:
        if not isinstance(scaff, dict):
            errs.append("scaffold 须为对象")
        else:
            files = scaff.get("files")
            if not isinstance(files, list):
                errs.append("scaffold.files 须为数组")
                files = []
            for i, f in enumerate(files):
                if not isinstance(f, dict) or not _safe_rel(f.get("path")):
                    errs.append(f"scaffold.files[{i}]: path 非法（须为树内"
                                "安全相对路径）")
                    continue
                if not isinstance(f.get("content"), str):
                    errs.append(f"scaffold.files[{i}]({f['path']}): "
                                "content 缺失")
                    continue
                scaff_files[f["path"]] = f["content"]
            wc = scaff.get("wiring_cmd")
            if wc is not None and not _s(wc):
                errs.append("scaffold.wiring_cmd 须为非空字符串")
            wp = scaff.get("wiring_paths")
            if wc is not None:
                if not isinstance(wp, list) or not wp \
                        or not all(_safe_rel(x) for x in wp):
                    errs.append("wiring_cmd 在场时 wiring_paths 必填"
                                "（树内安全相对路径数组）")
            if wc is None and isinstance(wp, list) and wp:
                errs.append("wiring_paths 只在 wiring_cmd 在场时有效")

    # criteria（proposed）
    crit = draft.get("criteria")
    if not isinstance(crit, list) or not crit:
        errs.append("criteria 须为非空数组")
        crit = []
    cids = set()
    for i, c in enumerate(crit):
        tag = f"criteria[{i}]"
        if not isinstance(c, dict) or not _s(c.get("id")):
            errs.append(f"{tag}: id 缺失")
            continue
        cid = c["id"].strip()
        if cid in cids:
            errs.append(f"{tag}({cid}): id 重复")
        cids.add(cid)
        probs = []
        if c.get("layer") not in ("L3", "L4"):
            probs.append("layer 须为 L3|L4（L1/L2 为冻结层）")
        if c.get("boot") not in ids and c.get("boot") not in \
                ("B_bare", "B_inject"):
            probs.append(f"boot `{c.get('boot')}` 不存在（可用：草案 "
                         f"boots 或 B_bare/B_inject）")
        if c.get("kind") not in ("log", "receipt"):
            probs.append("kind 须为 log|receipt（rc 为冻结专用）")
        if c.get("polarity") not in ("hit", "miss"):
            probs.append("polarity 须为 hit|miss")
        expr = c.get("expr")
        if not _s(expr):
            probs.append("expr 缺失")
        else:
            try:
                re.compile(expr)
            except re.error as e:
                probs.append(f"expr 非法正则: {e}")
        if c.get("kind") == "receipt":
            if c.get("polarity") != "hit":
                probs.append("receipt 判据 polarity 须为 hit")
            if resolve_expect(c.get("expect")) is None:
                probs.append("receipt 须带合法 expect（literal 或 "
                             "fill_sha256{byte,length}）")
        cite = c.get("cite")
        if not isinstance(cite, dict):
            probs.append("cite 缺失（tree 或 scaffold 二选一）")
        else:
            t, s = cite.get("tree"), cite.get("scaffold")
            if isinstance(t, dict):
                if not _safe_rel(t.get("path")) or not isinstance(
                        t.get("line"), int) or isinstance(t.get("line"), bool) \
                        or t["line"] <= 0 or not _s(t.get("quote")):
                    probs.append("cite.tree 须含 path/line(正整数)/quote")
            elif isinstance(s, dict):
                if not _s(s.get("file")) or s["file"] not in scaff_files \
                        or not _s(s.get("contains")):
                    probs.append("cite.scaffold 须含 file（在 "
                                "scaffold.files 中）与 contains")
            else:
                probs.append("cite 须为 tree 或 scaffold 之一")
        if not _s(c.get("evidence")):
            probs.append("evidence（人读理由）缺失")
        if probs:
            errs.append(f"{tag}({cid}): " + "；".join(probs))
    return errs


# ---------- 证据核查（反幻觉：引用必须在场） ----------

def check_cites(tree: Path, draft: dict) -> list[str]:
    """对草案逐条 criteria 核实 cite：tree 引用允许 ±2 行窗口命中
    quote；scaffold 引用须在对应脚手架文件内容中命中 contains。"""
    errs: list[str] = []
    scaff = {f.get("path"): f.get("content")
             for f in (draft.get("scaffold") or {}).get("files") or []
             if isinstance(f, dict)}
    for c in draft.get("criteria") or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        cite = c.get("cite") or {}
        t, s = cite.get("tree"), cite.get("scaffold")
        if isinstance(t, dict):
            p = tree / str(t.get("path"))
            try:
                lines = p.read_text(encoding="utf-8",
                                    errors="replace").splitlines()
            except OSError:
                errs.append(f"{c['id']}: cite 文件不可读 "
                            f"`{t.get('path')}`（相对目标树）")
                continue
            ln = int(t.get("line") or 0)
            quote = str(t.get("quote") or "")
            window = lines[max(0, ln - 3):ln + 2]
            if not any(quote in w for w in window):
                errs.append(f"{c['id']}: cite 未命中——{t.get('path')}:"
                            f"{ln} 前后 ±2 行内无 `{quote[:60]}`"
                            "（锚点必须是树中真实存在的语句）")
        elif isinstance(s, dict):
            content = scaff.get(s.get("file"))
            if content is None or str(s.get("contains")) not in content:
                errs.append(f"{c['id']}: scaffold cite 未命中——"
                            f"{s.get('file')} 内容中无 "
                            f"`{str(s.get('contains'))[:60]}`")
    return errs


def dry_assemble(draft: dict, runner: dict, target_os: Path,
                 driver_home_rel: str) -> list[str]:
    """模板干装配：占位符可替换、判据可引用到单元。零启动消耗。"""
    errs: list[str] = []
    ids = {b.get("id") for b in draft.get("boots") or []
           if isinstance(b, dict)}
    inj = runner.get("inject_device") or {}
    examples = inj.get("example_args") or {}
    iak = draft.get("inject_args_key") or (next(iter(examples))
                                           if len(examples) == 1 else None)
    for b in draft.get("boots") or []:
        if not isinstance(b, dict) or not b.get("cmd"):
            continue
        cmd = _subst(str(b["cmd"]), target_os, driver_home_rel,
                     b.get("args") if b.get("args") is not None
                     else (examples.get(iak) if "<DEVICE_ARGS>"
                           in str(b["cmd"]) else None))
        if "<DEVICE_ARGS>" in cmd:
            errs.append(f"boots({b.get('id')}): <DEVICE_ARGS> 占位符无"
                        "替换值（args 缺失且 example_args 无键可取）")
    for c in draft.get("criteria") or []:
        if isinstance(c, dict) and c.get("boot") not in ids \
                and c.get("boot") not in ("B_bare", "B_inject"):
            errs.append(f"criteria({c.get('id')}): 引用了不存在的单元 "
                        f"`{c.get('boot')}`")
    return errs


def _subst(tpl: str, target_os: Path, driver_home_rel: str,
           args: str | None) -> str:
    """占位符替换：单花括号 {PORTER_*} 负向后顾（shell 形态 ${VAR}
    经 env 展开，勿咬）；<DEVICE_ARGS> 全量替换。"""
    cmd = re.sub(r"(?<!\$)\{PORTER_TARGET_OS_ROOT\}",
                 lambda _m: str(target_os.resolve()), str(tpl))
    cmd = re.sub(r"(?<!\$)\{PORTER_DRIVER_HOME\}",
                 lambda _m: driver_home_rel, cmd)
    if args is not None:
        cmd = cmd.replace("<DEVICE_ARGS>", args)
    return cmd


# ---------- 冻结层合并（L1/L2/L3 命令机械绑定 runner 键） ----------

def _esc(s) -> str | None:
    return re.escape(str(s)) if s else None


def frozen_boots(runner: dict, inject_key: str | None) -> list[dict]:
    b = runner.get("build") or {}
    bo = runner.get("boot") or {}
    ut = runner.get("unit_test") or {}
    inj = runner.get("inject_device") or {}
    units = [
        {"id": "B_build", "origin": "frozen", "from": "runner.build",
         "cmd": b.get("cmd"), "timeout_sec": b.get("timeout_full_sec"),
         "log_is_stdout": True, "success": b.get("success_pattern")},
        {"id": "B_bare", "origin": "frozen", "from": "runner.boot",
         "cmd": bo.get("cmd"), "timeout_sec": bo.get("timeout_sec"),
         "log_file": bo.get("log_file"),
         "log_is_stdout": bo.get("log_is_stdout"),
         "success": bo.get("success_pattern"),
         "panic": bo.get("panic_pattern")},
        {"id": "B_unit", "origin": "frozen", "from": "runner.unit_test",
         "cmd": ut.get("cmd"), "timeout_sec": ut.get("timeout_sec"),
         "log_is_stdout": True, "success": ut.get("success_pattern"),
         "fail": ut.get("fail_pattern")},
    ]
    if inj:
        units.append({"id": "B_inject", "origin": "frozen",
                      "from": "runner.inject_device",
                      "mechanism": inj.get("mechanism", "env"),
                      "args_key": inject_key})
    return units


def frozen_criteria(runner: dict) -> list[dict]:
    b = runner.get("build") or {}
    bo = runner.get("boot") or {}
    ut = runner.get("unit_test") or {}
    inj = runner.get("inject_device") or {}
    crit: list[dict] = []

    def add(cid, layer, boot, kind, pol, expr, ev):
        crit.append({"id": cid, "layer": layer, "boot": boot, "kind": kind,
                     "polarity": pol, "expr": expr, "expect": None,
                     "evidence": ev, "origin": "frozen"})

    add("L1.build.rc", "L1", "B_build", "rc", "hit", None,
        "runner.build 退出码")
    if b.get("success_pattern"):
        add("L1.build.success", "L1", "B_build", "log", "hit",
            _esc(b["success_pattern"]), "runner.build.success_pattern")
    add("L3.bare.rc", "L3", "B_bare", "rc", "hit", None,
        "runner.boot 退出码")
    if bo.get("success_pattern"):
        add("L3.bare.success", "L3", "B_bare", "log", "hit",
            _esc(bo["success_pattern"]), "runner.boot.success_pattern")
    if bo.get("panic_pattern"):
        add("L3.bare.no_panic", "L3", "B_bare", "log", "miss",
            _esc(bo["panic_pattern"]), "runner.boot.panic_pattern")
    if inj:
        add("L3.inject.rc", "L3", "B_inject", "rc", "hit", None,
            "注入启动退出码")
        if bo.get("success_pattern"):
            add("L3.inject.success", "L3", "B_inject", "log", "hit",
                _esc(bo["success_pattern"]), "注入启动健康特征")
        if inj.get("driver_success_pattern"):
            add("L3.inject.driver_success", "L3", "B_inject", "log", "hit",
                _esc(inj["driver_success_pattern"]),
                "runner.inject_device.driver_success_pattern（冻结）")
        if inj.get("driver_fail_pattern"):
            add("L3.inject.driver_fail", "L3", "B_inject", "log", "miss",
                _esc(inj["driver_fail_pattern"]),
                "runner.inject_device.driver_fail_pattern（冻结）")
    add("L2.unit.rc", "L2", "B_unit", "rc", "hit", None,
        "runner.unit_test 退出码")
    if ut.get("success_pattern"):
        add("L2.unit.success", "L2", "B_unit", "log", "hit",
            _esc(ut["success_pattern"]), "runner.unit_test.success_pattern")
    if ut.get("fail_pattern"):
        add("L2.unit.no_fail", "L2", "B_unit", "log", "miss",
            _esc(ut["fail_pattern"]), "runner.unit_test.fail_pattern")
    return crit


def merge_frozen(draft: dict, runner: dict) -> tuple[dict, str | None]:
    """草案 + 冻结层 → acceptance.json。返回 (文档, inject_key)。"""
    inj = runner.get("inject_device") or {}
    examples = inj.get("example_args") or {}
    iak = draft.get("inject_args_key")
    if iak is None and len(examples) == 1:
        iak = next(iter(examples))
    boots = frozen_boots(runner, iak)
    for b in draft.get("boots") or []:
        if isinstance(b, dict):
            boots.append({"id": b.get("id"), "origin": "proposed",
                          "cmd": b.get("cmd"), "args": b.get("args"),
                          "timeout_sec": b.get("timeout_sec"),
                          "log_file": b.get("log_file"),
                          "log_is_stdout": b.get("log_is_stdout"),
                          "success": b.get("success_pattern"),
                          "panic": b.get("panic_pattern")})
    criteria = frozen_criteria(runner)
    for c in draft.get("criteria") or []:
        if isinstance(c, dict):
            criteria.append({**c, "origin": "proposed"})
    doc = {"status": "draft", "generated": _now(),
           "inject_args_key": iak,
           "boots": boots,
           "scaffold": draft.get("scaffold") or {"files": []},
           "criteria": criteria}
    return doc, iak


# ---------- 单元执行（runner 驱动 + 日志快照 + infra 重试） ----------

def _expand_unit(entry: dict, runner: dict, target_os: Path,
                 driver_home_rel: str) -> dict:
    """boot 条目 → 可执行 spec。B_inject 在此展开冻结模板机制。"""
    bo = runner.get("boot") or {}
    if entry.get("from") == "runner.inject_device":
        inj = runner.get("inject_device") or {}
        args = (inj.get("example_args") or {}).get(entry.get("args_key"))
        if args is None:
            raise ValueError(f"B_inject: example_args 无键 "
                             f"`{entry.get('args_key')}`")
        if entry.get("mechanism", "env") == "cmd":
            suffix = str(inj.get("cmd_suffix") or "").replace(
                "<DEVICE_ARGS>", args)
            return {"id": "B_inject", "cmd": f"{bo['cmd']} {suffix}",
                    "extra_env": None, "timeout_sec": bo.get("timeout_sec"),
                    "log_file": bo.get("log_file"),
                    "log_is_stdout": bo.get("log_is_stdout"),
                    "success": bo.get("success_pattern"),
                    "panic": bo.get("panic_pattern")}
        extra = {k: v.replace("<DEVICE_ARGS>", args)
                 for k, v in (inj.get("env") or {}).items()}
        return {"id": "B_inject", "cmd": bo["cmd"], "extra_env": extra,
                "timeout_sec": bo.get("timeout_sec"),
                "log_file": bo.get("log_file"),
                "log_is_stdout": bo.get("log_is_stdout"),
                "success": bo.get("success_pattern"),
                "panic": bo.get("panic_pattern")}
    cmd = _subst(str(entry.get("cmd") or ""), target_os, driver_home_rel,
                 entry.get("args"))
    return {"id": entry.get("id"), "cmd": cmd,
            "extra_env": None, "timeout_sec": entry.get("timeout_sec"),
            "log_file": entry.get("log_file"),
            "log_is_stdout": entry.get("log_is_stdout"),
            "success": entry.get("success"),
            "panic": entry.get("panic")}


def _run_unit_once(spec: dict, exp_dir: Path, target_os: Path,
                   runner: dict) -> dict:
    env = probe_mod._base_env(target_os, runner, spec.get("extra_env"))
    label = str(spec["id"])
    if not spec.get("log_is_stdout"):
        # 清旧日志防串判（probe._boot_once 同款惯例）：上一次启动的
        # 日志若残留，瞬态判定与判据都会读到陈旧内容
        lp, _mode = probe_mod._resolve_log(target_os, spec)
        if lp is not None:
            try:
                lp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    rc, out = probe_mod._run(spec["cmd"], cwd=target_os, env=env,
                             timeout_sec=int(spec.get("timeout_sec")
                                             or 600),
                             log_path=exp_dir / "logs" / f"T3_{label}.log")
    if spec.get("log_is_stdout"):
        log, state = probe_mod._strip_ansi(out), "stdout"
    else:
        lp, mode = probe_mod._resolve_log(target_os, spec)
        log = ""
        if lp is not None:
            try:
                if lp.exists():
                    log = probe_mod._strip_ansi(
                        lp.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                log = ""
        state = mode if log else f"{mode}:missing_or_empty"
    green = rc == 0
    parts = [f"rc={rc}"]
    if spec.get("success"):
        hit = spec["success"] in log
        green = green and hit
        parts.append(f"success={'hit' if hit else 'MISS'}")
    if spec.get("panic"):
        bad = spec["panic"].lower() in log.lower()
        green = green and not bad
        parts.append(f"panic={'hit' if bad else 'no-hit'}")
    if spec.get("fail"):
        bad = spec["fail"] in log
        green = green and not bad
        parts.append(f"fail={'hit' if bad else 'no-hit'}")
    snap = exp_dir / "logs" / f"{label}.log"
    try:
        snap.write_text(log or "", encoding="utf-8")
    except OSError:
        pass
    return {"rc": rc, "log": log, "log_state": state, "green": green,
            "detail": " ".join(parts), "snapshot": str(snap)}


def _run_unit(spec: dict, exp_dir: Path, target_os: Path,
              runner: dict) -> dict:
    """执行一次单元；infra 签名（rc≠0 ∧ 日志缺失/空）→ 重试 1 次。"""
    res = _run_unit_once(spec, exp_dir, target_os, runner)
    res["attempts"] = 1
    if res["rc"] != 0 and res["log_state"].endswith("missing_or_empty"):
        _log.console_line(f"[porter] exp-accept: 单元 {spec['id']} 疑似"
                          "瞬态（rc≠0 ∧ 日志空）——重试 1 次")
        res2 = _run_unit_once(spec, exp_dir, target_os, runner)
        res2["attempts"] = 2
        res = res2
    return res


# ---------- 草案相位（P2b 直连：文件即信号） ----------

def _draft_prompt(ws: Path, exp_dir: Path, proj: dict, runner: dict,
                  manifest: dict, mono_ledger: dict,
                  evidence_text: str) -> str:
    skill = agent.load_skill(SKILL_DRAFT)
    bo = runner.get("boot") or {}
    inj = runner.get("inject_device") or {}
    b = runner.get("build") or {}
    ut = runner.get("unit_test") or {}
    mono_dir = ws / "exp-mono"
    kb_face = ""
    try:
        from ..bootstrap import kb as _kb
        kb_face = _kb.kb_face(ws, ["pitfalls"]) or ""
    except Exception:
        kb_face = ""
    n_pass = sum(1 for m, e in (mono_ledger.get("modules") or {}).items()
                 if (e or {}).get("status") == "pass")
    runner_md = ""
    if (ws / "runner.md").exists():
        runner_md = (f"- runner 调用手册（**先精读**，含各命令来龙去脉与"
                     f"坑史）：`{ws / 'runner.md'}`\n")
    draft_path = exp_dir / "acceptance.draft.json"
    return (f"{skill}\n\n---\n\n## 背景数据（平台事实，以数据面为准）\n"
            f"- 目标 OS 源码树（你的工作目录）：`{proj.get('target_os')}`\n"
            f"- 驱动目录 driver_home：`{manifest.get('driver_home')}`\n"
            f"- 前序迁移：exp-mono {n_pass}/{len(mono_ledger.get('modules') or {})}"
            f" 模块 pass；报告 `{mono_dir / 'report.md'}`；泊车记录 "
            f"`{mono_dir / 'parking.md'}`；迁移词典 "
            f"`{mono_dir / 'mapping-notes.md'}`（三者先读）\n"
            f"{runner_md}"
            f"- 冻结命令（L1/L2/L3 机械绑定，你不可改）：\n"
            f"  - build：`{b.get('cmd')}`（成功特征 `{b.get('success_pattern')}`）\n"
            f"  - boot：`{bo.get('cmd')}`（成功 `{bo.get('success_pattern')}` / "
            f"panic `{bo.get('panic_pattern')}` / 日志 "
            f"`{bo.get('log_file') or 'stdout'}`）\n"
            f"  - inject_device：mechanism `{inj.get('mechanism')}`；"
            f"example_args `{json.dumps(inj.get('example_args') or {}, ensure_ascii=False)}`；"
            f"driver_success_pattern `{inj.get('driver_success_pattern')}`\n"
            f"  - unit_test：`{ut.get('cmd')}`\n"
            + (f"\n## 知识库 pitfalls 目录\n{kb_face}\n" if kb_face else "")
            + evidence_text
            + f"\n## 输出契约\n- 完整 JSON（裸 JSON，勿包 markdown 代码块）"
              f"写到：`{draft_path}`\n- schema 字段与 cite 规则见 SKILL；"
              "写完文件后在消息里简述即可（编排器读文件不读消息）。\n")


def _rework_message(errs: list[str], stem: str) -> str:
    return ("---\n\n## 上一轮方案的问题（修正后**重写整个文件**）\n- "
            + "\n- ".join(errs[:12])
            + f"\n\n证据核查详情可查日志 `{stem}.log`。")


def _register_plan_gate(ws: Path, acc_path: Path) -> None:
    from ..loop import gates as gates_mod
    sha = _sha16(acc_path)
    led = gates_mod.GateLedger(ws).load()
    g = led.find(GATE_ID)
    if g is None:
        led.add(id=GATE_ID, lane="checkpoint", kind="approval",
                gate_type="decision", phase="exp-accept",
                question=("系统验收方案审批（这是对'驱动作为系统工作'的"
                          "完成定义签字）。评审摘要见 "
                          "exp-accept/review.md——frozen 部分为冻结复用，"
                          "proposed 部分为 agent 提案（锚点引用/端到端"
                          "负载/期望值/负向例/脚手架），逐条过目。批准"
                          "绑定方案指纹——文件变更后批准自动失效。"),
                context_files=["exp-accept/acceptance.json",
                               "exp-accept/review.md"],
                answer_form=[
                    {"field": "verdict", "type": "enum",
                     "options": ["approve", "reject"], "required": True}],
                artifact_path=str(acc_path.relative_to(ws)),
                artifact_sha=sha)
    else:
        # 既有关口（redraft/重开）——重置为待答并刷新指纹
        g.update({"status": "open", "artifact_sha": sha, "answer": None,
                  "answered_by": None, "answered_at": None,
                  "resolution": None})
        g.setdefault("history", []).append(
            {"time": _now(), "event": "re-registered",
             "detail": f"新方案草案，指纹刷新 {sha}"})
        led.save()
    gates_mod.render_human_questions(ws)


def _write_review(exp_dir: Path, doc: dict, runner: dict) -> None:
    lines = ["# exp-accept 验收方案评审摘要", "",
             f"- 生成时间：{doc.get('generated')}",
             f"- inject_args_key：`{doc.get('inject_args_key')}`", "",
             "## 冻结层（机械绑定 runner 键，无需审）", ""]
    frozen = [c for c in doc.get("criteria") or []
              if c.get("origin") == "frozen"]
    lines += [f"- 判据 {len(frozen)} 条：L1/L2 命令与特征、L3 裸/注入"
              "启动健康与冻结消费锚，全部逐字取自 runner.json", "",
              "## agent 提案（本次评审焦点）", "",
              "### 启动单元（proposed）", "",
              "| id | 命令模板 | 日志定位 |", "|---|---|---|"]
    for b in doc.get("boots") or []:
        if b.get("origin") != "proposed":
            continue
        logspec = ("stdout" if b.get("log_is_stdout")
                   else f"file:{b.get('log_file')}")
        lines.append(f"| {b.get('id')} | `{str(b.get('cmd'))[:120]}` "
                     f"| {logspec} |")
    scaff = doc.get("scaffold") or {}
    lines += ["", "### 脚手架", ""]
    for f in scaff.get("files") or []:
        lines.append(f"- 落树文件 `{f.get('path')}`（{len(f.get('content') or '')} 字符）")
    if scaff.get("wiring_cmd"):
        lines.append(f"- 接线命令：`{str(scaff['wiring_cmd'])[:160]}`")
        lines.append(f"- 接线涉及路径：{scaff.get('wiring_paths')}")
    lines += ["", "### 判据（proposed）", "",
              "| id | 层 | 单元 | kind/极性 | expr | expect | 证据 |",
              "|---|---|---|---|---|---|---|"]
    for c in doc.get("criteria") or []:
        if c.get("origin") != "proposed":
            continue
        exp = c.get("expect")
        exp_s = ("—" if not exp else
                 json.dumps(exp, ensure_ascii=False)[:60])
        cite = c.get("cite") or {}
        cite_s = (f"{cite.get('tree', {}).get('path')}:"
                  f"{cite.get('tree', {}).get('line')}"
                  if "tree" in cite else
                  f"scaffold:{cite.get('scaffold', {}).get('file')}")
        lines.append(f"| {c.get('id')} | {c.get('layer')} | {c.get('boot')} "
                     f"| {c.get('kind')}/{c.get('polarity')} "
                     f"| `{str(c.get('expr'))[:80]}` | {exp_s} "
                     f"| {cite_s} |")
    for c in doc.get("criteria") or []:
        if c.get("origin") == "proposed":
            lines += ["", f"#### {c.get('id')}",
                      f"- 理由：{c.get('evidence')}"]
    lines += ["", "## 放行方式", "",
              "answers.md 追加：", "",
              "```",
              f"## @{GATE_ID}",
              "verdict: approve",
              "```", "",
              "或 `python3 porter/main.py gate answer "
              f"{GATE_ID} --set verdict=approve --output-dir <ws>`。"]
    (exp_dir / "review.md").write_text("\n".join(lines) + "\n",
                                       encoding="utf-8")


def _run_draft(ws: Path, exp_dir: Path, proj: dict, runner: dict,
               manifest: dict, mono_ledger: dict, ledger: dict,
               budget: int | None, session: str | None,
               evidence_text: str = "") -> int:
    import os
    if os.environ.get("PORTER_NO_AGENT"):
        _log.console_line("[porter] exp-accept: 草案需要 agent"
                          "（PORTER_NO_AGENT=1）——rc 2")
        return 2
    target_os = Path(proj["target_os"])
    draft_path = exp_dir / "acceptance.draft.json"
    base_prompt = _draft_prompt(ws, exp_dir, proj, runner, manifest,
                                mono_ledger, evidence_text)
    total = int(budget or DRAFT_BUDGET_SEC)
    used = 0.0
    session_id = session
    feedback = ""
    for rnd in range(1, DRAFT_ROUNDS + 1):
        _log.console_line(f"[porter] exp-accept: 草案第 {rnd}/"
                          f"{DRAFT_ROUNDS} 轮（设计 → 静态核查）")
        draft_path.unlink(missing_ok=True)      # 防脏读（上轮残留）
        draft, quality_note = None, ""
        for attempt in range(1, QUALITY_TRIES + 1):
            remaining = total - int(used)
            if remaining <= 0:
                _log.console_line(f"[porter] exp-accept: 草案预算耗尽"
                                  f"（session={session_id}）——可用 "
                                  "--session 续跑，rc 1")
                ledger["draft"] = {"rounds": rnd - 1, "session_id":
                                   session_id, "budget_exhausted": True}
                _save_ledger(exp_dir, ledger)
                return 1
            if rnd == 1 and attempt == 1:
                message = base_prompt
            elif attempt == 1:
                message = feedback
            else:
                message = quality_note
            stem = str(exp_dir / "logs" /
                       f"DRAFT_r{rnd}_R{attempt}")
            t0 = time.time()
            rc, out = agent._opencode_json_runner(
                message, workdir=target_os, log_stem=stem,
                timeout_sec=min(remaining, _CALL_CAP_SEC),
                session_id=session_id,
                task={"phase": "exp-accept", "step": "draft", "attempt":
                      rnd, "task_id": "exp-accept.draft"})
            used += time.time() - t0
            # 超时/失败也捞 session id（agent.py 捞取设计：被杀会话
            # 仍可 --session 续接）
            salvaged = (agent._parse_events(out) or {}).get("session_id")
            if salvaged:
                session_id = salvaged
            if rc != 0:
                ledger["draft"] = {"rounds": rnd, "session_id": session_id,
                                   "provider_rc": rc}
                _save_ledger(exp_dir, ledger)
                _log.console_line(f"[porter] exp-accept: provider 会话终态"
                                  f"失败 rc={rc}（session={session_id}"
                                  "——诊断日志后可 --session 续跑）rc 1")
                return 1
            if session_id is None:
                _log.console_line("[porter] exp-accept: 事件流无 session "
                                  "id（检查 opencode 登录/版本）——rc 1")
                ledger["draft"] = {"rounds": rnd, "session_id": None}
                _save_ledger(exp_dir, ledger)
                return 1
            draft = _read_json(draft_path)
            if draft is not None:
                errs = validate_draft(draft, runner)
                if not errs:
                    break
                quality_note = ("---\n\n## 上一次方案文件的校验缺陷（修订后"
                                "重写整个文件）\n- "
                                + "\n- ".join(errs[:12]))
                draft = None
            else:
                quality_note = (f"---\n\n## 上一次输出的问题\n方案文件不可"
                                f"读/未写：`{draft_path}`。把**完整**方案"
                                "（裸 JSON）写到该文件。")
        if draft is None:
            feedback = quality_note
            continue
        # 静态证据核查 + 模板干装配
        errs = check_cites(target_os, draft) + dry_assemble(
            draft, runner, target_os, str(manifest.get("driver_home")))
        if not errs:
            doc, _iak = merge_frozen(draft, runner)
            acc_path = exp_dir / "acceptance.json"
            acc_path.write_text(json.dumps(doc, ensure_ascii=False,
                                           indent=2) + "\n",
                                encoding="utf-8")
            _write_review(exp_dir, doc, runner)
            ledger["draft"] = {"rounds": rnd, "session_id": session_id,
                               "time": _now()}
            _save_ledger(exp_dir, ledger)
            _register_plan_gate(ws, acc_path)
            _log.console_line(f"[porter] exp-accept: 草案就绪（冻结 "
                              f"{sum(1 for c in doc['criteria'] if c['origin'] == 'frozen')}"
                              f" + 提案 {sum(1 for c in doc['criteria'] if c['origin'] == 'proposed')}"
                              f" 条判据）→ 评审摘要 {exp_dir / 'review.md'}"
                              "——人审放行（exit 3）")
            return 3
        feedback = _rework_message(errs, stem)
    from ..loop import gates as gates_mod
    ledger["draft"] = {"rounds": DRAFT_ROUNDS, "session_id": session_id,
                       "exhausted": True, "time": _now()}
    _save_ledger(exp_dir, ledger)
    return gates_mod.panic(ws, {
        "id": GATE_FAIL_ID, "kind": "retry", "gate_type": "failure",
        "phase": "exp-accept",
        "question": (f"验收方案 {DRAFT_ROUNDS} 轮回炉仍未通过静态核查"
                     "（schema/证据引用/模板装配）。各轮证据见 "
                     "exp-accept/logs/DRAFT_*。人工诊断后可用 --session "
                     f"续接（上次会话 {session_id}）或重跑 --draft。"),
        "context_files": ["exp-accept/acceptance.draft.json"],
        "answer_form": [
            {"field": "note", "type": "text", "required": False,
             "hint": "诊断笔记（如锚点漂移/正则缺陷）"}],
    })


# ---------- 放行处理 ----------

def _approval_state(ws: Path, exp_dir: Path, ledger: dict) -> str:
    """missing | draft | approved。approved 时冻结指纹入 ledger。"""
    from ..loop import gates as gates_mod
    acc_path = exp_dir / "acceptance.json"
    doc = _read_json(acc_path)
    if doc is None:
        return "missing"
    if doc.get("status") == "approved":
        return "approved"
    gates_mod.process_answered_gates(ws)
    led = gates_mod.GateLedger(ws).load()
    g = led.find(GATE_ID)
    ok = bool(g and g.get("status") in ("applied", "resolved")
              and str((g.get("answer") or {}).get("verdict", ""))
              .lower() in ("approve", "release", "放行", "通过"))
    if not ok or doc.get("status") != "draft":
        return "draft"
    #  belts-and-braces：作答指向的指纹须与当前文件一致（作答后文件
    #  被改过 → 关口重置为待答，人重新审阅当前内容）
    cur = _sha16(acc_path)
    if g.get("artifact_sha") and cur != g.get("artifact_sha"):
        _log.console_line("[porter] exp-accept: 作答后方案文件已变更"
                          "（指纹不符）——关口重置为待答，请重新审阅"
                          "表态 rc 3")
        g.update({"status": "open", "artifact_sha": cur, "answer": None,
                  "answered_by": None, "answered_at": None,
                  "resolution": None})
        led.save()
        gates_mod.render_human_questions(ws)
        return "draft"
    sha = cur
    doc["status"] = "approved"
    doc["approved_time"] = _now()
    acc_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2)
                        + "\n", encoding="utf-8")
    # status 字段变更会改变文件——放行后指纹以 approved 态为准
    ledger["approval"] = {"sha": _sha16(acc_path), "time": _now()}
    gates_mod.resolve_applied(led, GATE_ID, "exp-accept 方案放行")
    gates_mod.render_human_questions(ws)
    _log.console_line("[porter] exp-accept: 方案已放行（指纹冻结）")
    return "approved"


# ---------- execute：脚手架 / 矩阵 / 修环 ----------

def _apply_scaffold(ws: Path, exp_dir: Path, target_os: Path,
                    acc: dict, ledger: dict) -> bool:
    scaff = acc.get("scaffold") or {}
    files = scaff.get("files") or []
    if not files and not scaff.get("wiring_cmd"):
        ledger["scaffold"] = {"applied": True, "files": [],
                              "time": _now()}
        return True
    if (ledger.get("scaffold") or {}).get("applied"):
        # 幂等：文件与冻结内容一致 → 跳过
        drift = [f["path"] for f in files
                 if not (target_os / f["path"]).exists()
                 or (target_os / f["path"]).read_text(
                     encoding="utf-8", errors="replace") != f["content"]]
        if not drift:
            return True
        _log.console_line(f"[porter] exp-accept: 脚手架漂移 {drift}——"
                          "按冻结内容重写")
    for f in files:
        p = target_os / f["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"], encoding="utf-8")
    if scaff.get("wiring_cmd"):
        cmd = _subst(str(scaff["wiring_cmd"]), target_os, "", None)
        rc, _out = probe_mod._run(
            cmd, cwd=target_os,
            env=probe_mod._base_env(target_os, {}),
            timeout_sec=600,
            log_path=exp_dir / "logs" / "scaffold_wiring.log")
        if rc != 0:
            _log.console_line(f"[porter] exp-accept: 脚手架接线失败 "
                              f"rc={rc}（全文 {exp_dir / 'logs' / 'scaffold_wiring.log'}）"
                              "——数据面缺陷，建议 --redraft，rc 1")
            return False
    ledger["scaffold"] = {"applied": True,
                          "files": [f["path"] for f in files],
                          "time": _now()}
    try:
        from ..common import vcs as _vcs
        paths = [f["path"] for f in files] + \
            list(scaff.get("wiring_paths") or [])
        ledger["scaffold"]["commit"] = _vcs.commit_target(
            ws, "exp-accept(scaffold): guest workload scaffolding",
            paths=paths, phase="exp-accept") or []
    except Exception as ex:
        _log.console_line(f"[porter] exp-accept: 脚手架 commit 失败"
                          f"（{ex!r}）——ledger 照记")
    return True


def _scaffold_drift(target_os: Path, acc: dict) -> list[str]:
    return [f["path"] for f in (acc.get("scaffold") or {}).get("files") or []
            if not (target_os / f["path"]).exists()
            or (target_os / f["path"]).read_text(
                encoding="utf-8", errors="replace") != f["content"]]


def _restore_scaffold(target_os: Path, acc: dict) -> None:
    for f in (acc.get("scaffold") or {}).get("files") or []:
        p = target_os / f["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"], encoding="utf-8")


def _allowed_paths(manifest: dict) -> list[str]:
    dh = str(manifest.get("driver_home") or "")
    return [dh] + list(manifest.get("commit_paths") or [])


def _scope_offenders(target_os: Path, baseline: set[str],
                     manifest: dict) -> list[str]:
    def _allowed(p: str) -> bool:
        return any(p == a or p.startswith(a.rstrip("/") + "/")
                   for a in _allowed_paths(manifest) if a)
    return sorted(p for p in _mono._git_status(target_os) - baseline
                  if not _allowed(p))


def _red_summary(judged: list[dict], results: dict) -> str:
    lines = []
    for j in judged:
        if j.get("ok") is not False:
            continue
        r = results.get(j.get("boot")) or {}
        lines.append(f"- {j['id']} [{j.get('layer')}/{j.get('boot')}] "
                     f"{j.get('detail')}（日志快照 {r.get('snapshot', '—')}，"
                     "自行 tail/grep）")
    return "\n".join(lines)


def _diagnose_prompt(acc: dict, judged: list[dict], results: dict,
                     manifest: dict, red_text: str, kb_face: str) -> str:
    skill = agent.load_skill(SKILL_FIX)
    return (f"{skill}\n\n---\n\n## 失败现场（本轮待解红项）\n{red_text}\n\n"
            "## 判据定义（量尺，已冻结——改量尺须报 plan-defect）\n"
            + "\n".join(f"- {c.get('id')}: layer={c.get('layer')} "
                        f"boot={c.get('boot')} kind={c.get('kind')} "
                        f"polarity={c.get('polarity')} "
                        f"expr=`{c.get('expr')}`"
                        for c in acc.get("criteria") or []
                        if any(j.get("id") == c.get("id")
                               and j.get("ok") is False for j in judged))
            + "\n\n## 改动范围（硬约束）\n"
            f"- 允许：驱动目录 `{manifest.get('driver_home')}` 与数据面"
            f"登记的接线文件 {_allowed_paths(manifest)[1:] or '（无）'}\n"
            "- 脚手架文件已冻结：漂移会被自动还原并把守卫失败反馈给你\n"
            "- 数据面（判据/命令/脚本本身有错）→ 输出 action="
            "plan-defect，勿硬修\n"
            + (f"\n## 知识库目录\n{kb_face}\n" if kb_face else ""))


def _run_diagnose(ws: Path, exp_dir: Path, proj: dict, runner: dict,
                  manifest: dict, acc: dict, judged: list[dict],
                  results: dict, ledger: dict,
                  budget: int | None, session: str | None) -> tuple[str, dict]:
    """修环：agent 修码（范围守卫）+ 静态段复跑受影响单元重判。
    返回 (solved|parked|plan-defect|no-agent|failed, outcome)。"""
    import os
    target_os = Path(proj["target_os"])
    if os.environ.get("PORTER_NO_AGENT"):
        return "no-agent", {}
    red_ids = [j["id"] for j in judged if j.get("ok") is False]
    red_boots = sorted({j["boot"] for j in judged
                        if j.get("ok") is False and j.get("boot")})
    crit_by_id = {c.get("id"): c for c in acc.get("criteria") or []}
    baseline = _mono._git_status(target_os)
    kb_face = ""
    try:
        from ..bootstrap import kb as _kb
        kb_face = _kb.kb_face(ws, ["failures", "pitfalls"]) or ""
    except Exception:
        pass

    def _static() -> tuple[bool, str]:
        problems: list[str] = []
        drift = _scaffold_drift(target_os, acc)
        if drift:
            _restore_scaffold(target_os, acc)
            problems.append("脚手架漂移已还原：" + "、".join(drift)
                            + "（脚手架冻结，数据面问题走 plan-defect）")
        offenders = _scope_offenders(target_os, baseline, manifest)
        if offenders:
            problems.append("改动越出白名单（driver_home ∪ 登记接线文件）："
                            + "、".join(offenders))
        if problems:
            return False, "\n".join(problems)
        sub_results = dict(results)
        for bid in red_boots:
            entry = next(b for b in acc["boots"] if b.get("id") == bid)
            spec = _expand_unit(entry, runner, target_os,
                                str(manifest.get("driver_home")))
            sub_results[bid] = _run_unit(spec, exp_dir, target_os, runner)
        sub_judged = judge_criteria(
            [crit_by_id[i] for i in red_ids if i in crit_by_id],
            sub_results)
        red_left = [j for j in sub_judged if j["ok"] is False]
        text = ("复跑受影响单元重判：红项 "
                f"{len(red_ids) - len(red_left)}/{len(red_ids)} 转绿\n"
                + _red_summary(sub_judged, sub_results))
        return (not red_left), text

    prompt = _diagnose_prompt(acc, judged, results, manifest,
                              _red_summary(judged, results), kb_face)
    outcome = agent.run_agent_seq(
        prompt, workdir=target_os,
        log_stem=str(exp_dir / "logs" / "DIAG"),
        static={"describe": "复跑受影响单元并重判（含脚手架漂移还原"
                            "与改动范围守卫）", "fn": _static},
        gen_schema={"status": "str", "circuit": "str", "action": "str",
                    "evidence": "list", "summary": "str"},
        final_static=True,
        agent_budget_sec=int(budget or DIAG_BUDGET_SEC),
        resume_session=session,
        task={"phase": "exp-accept", "step": "diagnose",
              "task_id": "exp-accept.diagnose"})
    ledger.setdefault("diagnose", []).append({
        "time": _now(), "red": red_ids,
        "status": outcome.get("status"),
        "session_id": outcome.get("session_id"),
        "rounds": len(outcome.get("rounds") or []),
        "agent_sec": outcome.get("total_agent_sec")})
    _save_ledger(exp_dir, ledger)      # 修环历史即时落盘（续修依据）
    parsed = outcome.get("parsed") or {}
    action = str(parsed.get("action") or "")
    if outcome.get("status") == "done" and \
            parsed.get("status") != "blocked":
        summary = str(parsed.get("summary") or "fix")[:80]
        try:
            from ..common import vcs as _vcs
            _vcs.commit_target(
                ws, f"exp-accept(fix): {summary}",
                paths=_allowed_paths(manifest), phase="exp-accept")
        except Exception as ex:
            _log.console_line(f"[porter] exp-accept: 修码 commit 失败"
                              f"（{ex!r}）——继续")
        return "solved", outcome
    if action == "plan-defect":
        return "plan-defect", outcome
    return "parked", outcome


def _park(exp_dir: Path, title: str, note: str) -> None:
    p = exp_dir / "parking.md"
    body = p.read_text(encoding="utf-8", errors="replace") if p.exists() \
        else "# 泊车记录\n\n> 需要人工裁定/平台侧能力的事项。\n"
    p.write_text(body + f"\n## {title} ({_now()})\n{note}\n",
                 encoding="utf-8")


def _write_report(ws: Path, exp_dir: Path, acc: dict, ledger: dict,
                  units: dict, judged: list[dict],
                  verdict: str | None) -> None:
    by_layer: dict[str, list] = {}
    for j in judged:
        by_layer.setdefault(j.get("layer") or "?", []).append(j)
    lines = ["# exp-accept 系统验收报告", "",
             f"- 生成时间：{_now()}",
             f"- 驱动身份：{acc.get('driver') or (ledger.get('identity') or '—')}",
             f"- 结论：{'全绿（4 层通过）' if verdict == 'all-green' else (verdict or '未完成')}",
             "", "## 四层判定", "", "| 层 | 判据数 | 通过 | 红 | 待定 |",
             "|---|---|---|---|---|"]
    for layer in CRIT_LAYERS:
        js = by_layer.get(layer) or []
        lines.append(f"| {layer} | {len(js)} "
                     f"| {sum(1 for j in js if j.get('ok') is True)} "
                     f"| {sum(1 for j in js if j.get('ok') is False)} "
                     f"| {sum(1 for j in js if j.get('ok') is None)} |")
    lines += ["", "## 执行单元", "",
              "| 单元 | 来源 | rc | 健康 | 尝试 | 日志快照 |",
              "|---|---|---|---|---|---|"]
    for b in acc.get("boots") or []:
        u = units.get(b.get("id")) or {}
        if not u:
            continue
        lines.append(f"| {b.get('id')} | {b.get('origin')} "
                     f"| {u.get('rc')} | {'绿' if u.get('green') else '红'} "
                     f"| {u.get('attempts', 1)} | `{u.get('snapshot', '—')}` |")
    lines += ["", "## 判据明细", "",
              "| 判据 | 层 | 单元 | 来源 | 结果 | 说明 |", "|---|---|---|---|---|---|"]
    for j in judged:
        mark = "PASS" if j.get("ok") else ("PEND" if j.get("ok") is None
                                           else "FAIL")
        lines.append(f"| {j.get('id')} | {j.get('layer')} "
                     f"| {j.get('boot')} | {j.get('origin')} | {mark} "
                     f"| {j.get('detail')} |")
    hist = ledger.get("diagnose") or []
    if hist:
        lines += ["", "## 修环历史", ""]
        for h in hist:
            lines.append(f"- {_h_line(h)}")
    sc = ledger.get("scaffold") or {}
    if sc.get("applied"):
        ch = sc.get("commit") or []
        lines += ["", "## 脚手架", "",
                  f"- 文件：{sc.get('files')}",
                  f"- commit：{ch[0][:10] if ch else '—'}"]
    (exp_dir / "report.md").write_text("\n".join(lines) + "\n",
                                       encoding="utf-8")


def _h_line(h: dict) -> str:
    ch = h.get("commit")
    return (f"{h.get('time')} 红 {len(h.get('red') or [])} 项 → "
            f"{h.get('status')}（agent {h.get('agent_sec')}s，"
            f"session {h.get('session_id')}）")


def _save_ledger(exp_dir: Path, ledger: dict) -> None:
    ledger["time"] = _now()
    (exp_dir / "ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _run_execute(ws: Path, exp_dir: Path, proj: dict, runner: dict,
                 manifest: dict, acc: dict, ledger: dict,
                 budget: int | None, session: str | None) -> int:
    target_os = Path(proj["target_os"])
    driver_home_rel = str(manifest.get("driver_home"))
    # 指纹核验（approved 态）
    cur = _sha16(exp_dir / "acceptance.json")
    if cur != (ledger.get("approval") or {}).get("sha"):
        _log.console_line("[porter] exp-accept: acceptance.json 与放行"
                          "指纹不符（方案被改过？）——须重新人审：把 "
                          "status 改回 draft 后重跑放行，或 --redraft，"
                          "rc 2")
        return 2
    if not _apply_scaffold(ws, exp_dir, target_os, acc, ledger):
        return 1
    _save_ledger(exp_dir, ledger)
    criteria = acc.get("criteria") or []
    prev_head = None
    while True:
        head = _tree_head(target_os)
        # 执行序：L1 → L3 裸/注入 → L4 模板单元 →（无红才）L2 殿后
        order = ["B_build", "B_bare", "B_inject"] + \
            [b["id"] for b in acc.get("boots") or []
             if b.get("origin") == "proposed"] + ["B_unit"]
        results: dict = {}
        units = ledger.setdefault("units", {})
        red_seen = False
        for bid in order:
            entry = next((b for b in acc.get("boots") or []
                          if b.get("id") == bid), None)
            if entry is None:
                continue
            if bid == "B_unit" and red_seen:
                _log.console_line("[porter] exp-accept: 已有红项——"
                                  "L2 全量单测本轮跳过（修好后再跑）")
                break
            cached = units.get(bid) or {}
            if cached.get("green") and cached.get("tree_head") == head:
                _log.console_line(f"[porter] exp-accept: {bid} 已绿且"
                                  "树未变——复用缓存结果")
                results[bid] = {"rc": cached.get("rc"),
                                "log": _read_log(cached.get("snapshot")),
                                "log_state": cached.get("log_state"),
                                "green": True,
                                "detail": cached.get("detail"),
                                "snapshot": cached.get("snapshot"),
                                "reused": True}
            else:
                _log.console_line(f"[porter] exp-accept: 执行单元 {bid}")
                spec = _expand_unit(entry, runner, target_os,
                                    driver_home_rel)
                res = _run_unit(spec, exp_dir, target_os, runner)
                rec = {k: v for k, v in res.items() if k != "log"}
                rec.update(tree_head=head, time=_now())
                units[bid] = rec
                _save_ledger(exp_dir, ledger)
                results[bid] = res
            judged = judge_criteria(criteria, results)
            if any(j.get("ok") is False for j in judged):
                red_seen = True
        judged = judge_criteria(criteria, results)
        ledger["criteria"] = {j["id"]: {"ok": j["ok"],
                                        "detail": j["detail"]}
                              for j in judged}
        _save_ledger(exp_dir, ledger)
        reds = [j for j in judged if j.get("ok") is False]
        for j in judged:
            mark = "PASS" if j["ok"] else ("PEND" if j["ok"] is None
                                           else "FAIL")
            _log.console_line(f"[porter] exp-accept: {str(j.get('id')):<44} "
                              f"{mark}  {j.get('detail', '')[:80]}")
        if not reds and all((units.get(b) or {}).get("green")
                            for b in results):
            ledger["verdict"] = "all-green"
            _save_ledger(exp_dir, ledger)
            _write_report(ws, exp_dir, acc, ledger, units, judged,
                          "all-green")
            _log.console_line("[porter] exp-accept: 4 层全绿——验收通过")
            return 0
        if not reds:
            # 有单元未跑（如 B_unit 被跳过）但无红——不应发生，防御
            _log.console_line("[porter] exp-accept: 存在未执行单元——"
                              "继续执行序")
            reds = [{"id": "(pending)"}]
        if prev_head is not None and head == prev_head:
            _park(exp_dir, "修环无进展",
                  "修环后树头未变且仍有红项——诊断未产生可验证修复。"
                  f"红项：{'、'.join(j['id'] for j in reds)}")
            _write_report(ws, exp_dir, acc, ledger, units, judged, None)
            return 1
        _log.console_line(f"[porter] exp-accept: 红项 {len(reds)} 个——"
                          "进入自动修环")
        state, diag_out = _run_diagnose(ws, exp_dir, proj, runner,
                                        manifest, acc, judged, results,
                                        ledger, budget, session)
        session = None                # 会话只续接首轮
        diag_sid = diag_out.get("session_id") if diag_out else None
        if state == "plan-defect":
            _park(exp_dir, "数据面缺陷（待 redraft）",
                  "修环判定失败根因在验收方案数据面（判据/命令/脚手架"
                  "本身）——人工用 --redraft 修订方案并重新人审。")
            _write_report(ws, exp_dir, acc, ledger, units, judged, None)
            return 1
        if state != "solved":
            _park(exp_dir, "修环未解",
                  f"自动修环终态 {state}。红项："
                  f"{'、'.join(j['id'] for j in reds)}；详情见 "
                  "exp-accept/logs/DIAG*。"
                  + (f"人工诊断后可 --diagnose --session {diag_sid} 续修。"
                     if diag_sid else "人工诊断后 --diagnose 续修。"))
            _write_report(ws, exp_dir, acc, ledger, units, judged, None)
            return 1
        prev_head = head


# ---------- 主入口 ----------

def run_exp_accept(ws: Path, mode: str = "resume",
                   budget: int | None = None,
                   session: str | None = None) -> int:
    ws = Path(ws).resolve()
    needs = ["project.json", "runner.json",
             "P2/reports/scaffold_manifest.json",
             "exp-mono/ledger.json"]
    missing = [n for n in needs if not (ws / n).exists()]
    if missing:
        _log.console_line(f"[porter] exp-accept: 前置缺失："
                          + "、".join(missing) + "——rc 2")
        return 2
    proj = _read_json(ws / "project.json") or {}
    runner = _read_json(ws / "runner.json") or {}
    manifest = _read_json(ws / "P2" / "reports" /
                          "scaffold_manifest.json") or {}
    mono_ledger = _read_json(ws / "exp-mono" / "ledger.json") or {}
    mods = mono_ledger.get("modules") or {}
    if not mods or not all((e or {}).get("status") == "pass"
                           for e in mods.values()):
        _log.console_line("[porter] exp-accept: exp-mono 未全 pass——"
                          "先完成迁移（本流程只接终态）rc 2")
        return 2
    if not manifest.get("driver_home") or not proj.get("target_os"):
        _log.console_line("[porter] exp-accept: manifest 无 driver_home "
                          "或 project 无 target_os——rc 2")
        return 2
    exp_dir = ws / "exp-accept"
    (exp_dir / "logs").mkdir(parents=True, exist_ok=True)
    ledger = _read_json(exp_dir / "ledger.json") or {}
    if not (exp_dir / "parking.md").exists():
        (exp_dir / "parking.md").write_text(
            "# 泊车记录\n\n> 需要人工裁定/平台侧能力的事项。\n",
            encoding="utf-8")
    state = _approval_state(ws, exp_dir, ledger)
    _save_ledger(exp_dir, ledger)

    if mode in ("draft", "redraft"):
        evidence = ""
        if mode == "redraft":
            reds = (ledger.get("criteria") or {})
            red_ids = [i for i, v in reds.items()
                       if (v or {}).get("ok") is False]
            evidence = ("\n## 前次执行失败证据（redraft 背景）\n- 红项："
                        + ("、".join(red_ids) or "（见 ledger/parking）")
                        + "\n- 上一方案与泊车记录在 exp-accept/ 与 "
                        "exp-mono/，先诊断数据面缺陷再修订。\n")
            # 重开：方案回 draft 态，旧放行作废
            acc_path = exp_dir / "acceptance.json"
            doc = _read_json(acc_path)
            if doc and doc.get("status") == "approved":
                doc["status"] = "draft"
                acc_path.write_text(json.dumps(
                    doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                ledger.pop("approval", None)
        return _run_draft(ws, exp_dir, proj, runner, manifest,
                          mono_ledger, ledger, budget, session,
                          evidence_text=evidence)

    if state == "missing":
        _log.console_line("[porter] exp-accept: 无 acceptance.json——先跑 "
                          "`--draft`（agent 设计验收方案）rc 2")
        return 2
    if state == "draft":
        _log.console_line("[porter] exp-accept: 方案待人审——评审摘要 "
                          f"{exp_dir / 'review.md'}；answers.md 写 "
                          f"`## @{GATE_ID}` + `verdict: approve` 后重跑"
                          "——rc 3")
        return 3
    # approved
    acc = _read_json(exp_dir / "acceptance.json") or {}
    acc.setdefault("driver", manifest.get("driver"))
    if mode == "diagnose":
        reds = (ledger.get("criteria") or {})
        red_ids = [i for i, v in reds.items()
                   if (v or {}).get("ok") is False]
        if not red_ids:
            _log.console_line("[porter] exp-accept: ledger 无红项——"
                              "无需诊断（重跑默认模式续跑验收）rc 2")
            return 2
        judged = [{"id": i, "layer": "?", "boot":
                   (next((c.get("boot") for c in acc.get("criteria") or []
                          if c.get("id") == i), "?")),
                   "origin": "?", "ok": False,
                   "detail": (reds[i] or {}).get("detail", "")}
                  for i in red_ids]
        results = {}
        for bid in sorted({j["boot"] for j in judged}):
            u = (ledger.get("units") or {}).get(bid) or {}
            results[bid] = {"rc": u.get("rc"), "log":
                            _read_log(u.get("snapshot")),
                            "log_state": u.get("log_state", "?"),
                            "green": u.get("green"),
                            "snapshot": u.get("snapshot", "—")}
        state_d, _out = _run_diagnose(ws, exp_dir, proj, runner,
                                      manifest, acc, judged, results,
                                      ledger, budget, session)
        _save_ledger(exp_dir, ledger)
        if state_d == "solved":
            _log.console_line("[porter] exp-accept: 修环解决——重跑默认"
                              "模式完成剩余验收")
            return 0
        _park(exp_dir, f"续修未解（{state_d}）",
              "见 exp-accept/logs/DIAG*。")
        return 1
    return _run_execute(ws, exp_dir, proj, runner, manifest, acc,
                        ledger, budget, session)


def _read_log(path) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
