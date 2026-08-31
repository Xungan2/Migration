"""criteria.py — 判据草案 schema 校验 + L0-L4 复核执行器（plan §10 定案 4）。

schema（每条）：
    {id, layer(L0-L4), kind(unit_test|log_pattern|counter|compile|boot|e2e),
     expr, deferred_by(消费者模块列表|null)}

layer-kind 一致性（机器强制）：
    unit_test→L0 / compile→L1 / boot→L2 / log_pattern|counter→L3 / e2e→L4

expr 语义：
    unit_test    测试函数名（逗号分隔多个）；判 `test .*<名>.* ... ok`
    log_pattern  qemu.log 正则；计数 ≥1
    counter      同 log_pattern（正则自带数值断言，如 rx=[1-9]）
    compile/boot/e2e  未用（空串）

复核执行器（P5(M) 消费）：
    compile     → runner build 双信号
    boot        → runner boot 双信号（含无 panic）
    unit_test   → runner unit_test 节（通用机制节；mechanism=none → 自动
                  转 deferred，非硬失败）
    log_pattern/counter → qemu.log 正则
    e2e         → 循环内不机器复核，登记 deferred 归 P6 系统验收
"""

from __future__ import annotations

import re
from pathlib import Path

KIND_LAYER = {"unit_test": "L0", "compile": "L1", "boot": "L2",
              "log_pattern": "L3", "counter": "L3", "e2e": "L4"}
LAYERS = {"L0", "L1", "L2", "L3", "L4"}
KINDS = set(KIND_LAYER)
_MODULES_UNKNOWN_OK = True      # deferred_by 允许指向尚不存在消费者的模块名


def validate_criteria(raw: list, module: str) -> tuple[list[dict], list[str]]:
    """返回 (合格条目, 错误清单)。"""
    ok: list[dict] = []
    errs: list[str] = []
    if not isinstance(raw, list):
        return [], ["criteria 不是数组"]
    seen: set[str] = set()
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            errs.append(f"[{i}] 非对象")
            continue
        miss = [k for k in ("id", "layer", "kind", "expr", "deferred_by")
                if k not in c]
        if miss:
            errs.append(f"[{i}] 缺字段 {miss}")
            continue
        problems = []
        cid = str(c["id"])
        if not cid:
            problems.append("id 为空")
        if cid in seen:
            problems.append(f"id 重复: {cid}")
        if c["layer"] not in LAYERS:
            problems.append(f"layer 非法: {c['layer']}")
        if c["kind"] not in KINDS:
            problems.append(f"kind 非法: {c['kind']}")
        elif c["layer"] != KIND_LAYER[c["kind"]]:
            problems.append(f"layer {c['layer']} 与 kind {c['kind']} 不一致"
                            f"（须 {KIND_LAYER[c['kind']]}）")
        expr = str(c["expr"] or "")
        if c["kind"] in ("log_pattern", "counter"):
            if not expr:
                problems.append(f"{c['kind']} 必须有 expr")
            else:
                try:
                    re.compile(expr)
                except re.error as e:
                    problems.append(f"expr 非法正则: {e}")
        if c["kind"] == "unit_test" and not expr:
            problems.append("unit_test 必须给测试函数名")
        db = c["deferred_by"]
        if db is not None and not isinstance(db, list):
            problems.append("deferred_by 须为数组或 null")
        if problems:
            errs.append(f"[{c.get('id', i)}] {'; '.join(problems)}")
        else:
            seen.add(cid)
            ok.append({"id": cid, "layer": c["layer"], "kind": c["kind"],
                       "expr": expr, "deferred_by": db})
    _ = (module, _MODULES_UNKNOWN_OK)   # 预留：消费者存在性弱校验
    return ok, errs


def baseline_criteria(module: str) -> list[dict]:
    """恒加基线：L1 编译 + L2 启动（strategy 未覆盖的底线）。"""
    return [
        {"id": f"{module}.compile", "layer": "L1", "kind": "compile",
         "expr": "", "deferred_by": None},
        {"id": f"{module}.boot", "layer": "L2", "kind": "boot",
         "expr": "", "deferred_by": None},
    ]


# ---------- 复核执行器 ----------

def check_unit_test(output_text: str, names: list[str],
                    success_pattern: str = "test result: ok"
                    ) -> tuple[bool, str]:
    """判单测输出：整体 ok（discovered success_pattern）+ 逐测名命中。

    调用方须先去 ANSI 颜色码（p4._run_unit_test 已做）。
    """
    if success_pattern not in output_text:
        return False, f"输出无 '{success_pattern}'"
    for ln in output_text.splitlines():
        if "test result:" in ln and "failed" in ln and "0 failed" not in ln:
            return False, f"存在失败: {ln.strip()}"
    # 逐名只要求"该测试行存在"：整体 result 已确认 0 failed（跑到的都过），
    # 而驱动 debug! 日志可能内联插进 "name ... ok" 之间，使单行
    # `name ... ok` 正则失配（2026-08-30 hw-eeprom 实测）。
    missing = [n for n in names
               if not re.search(rf"test .*\b{re.escape(n)}\b", output_text)]
    if missing:
        return False, f"测试未命中: {', '.join(missing)}"
    return True, f"{len(names)} 测试全过"


def check_log_pattern(log_text: str, pattern: str) -> tuple[bool, int]:
    """判 qemu.log 正则命中数 ≥1。返回 (ok, count)。"""
    n = len(re.findall(pattern, log_text))
    return n > 0, n
