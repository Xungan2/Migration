"""state.py — loop_state.json 状态机（垂直循环断点重入，plan §10 定案 3）。

schema：
    {
      "order":   ["hw-defs", ...],            # P1 deps.json 拓扑序（真值源）
      "modules": {"hw-defs": {"phase": "pending|p3|p4|p5|done",
                              "attempts": {"p3": 0, "p4": 0, "p5": 0}}, ...},
      "updated": "<iso8601>"
    }

phase 语义（方案 A 重构，2026-08-31）：
    pending  未开始
    p3       P3(M) 进行中/已留检查点（产物幂等续跑）
    p4       P3(M) 完成，P4(M)（fill + 切片迁移 + 轮末快速冒烟）待做/进行中
    p5       P4(M) 完成，P5(M) 模块级验收待做/进行中
    done     P5(M) 验收 PASS（L1/L2/L0/L3 判据 + 累积回归 + deferred 清偿）

存量兼容：读入时 attempts 缺 p5 键自动补 0（仅内存归一，合法 state 不回写）。

断点指针 = order 中首个 phase != done 的模块。写入原子（tmp + rename）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

PHASES = ("pending", "p3", "p4", "p5", "done")
ATTEMPT_STEPS = ("p3", "p4", "p5")
MAX_ATTEMPTS = 3        # 每模块每阶段的人工升级界（§10.5 "FAIL 超界"）


def _zero_attempts() -> dict[str, int]:
    return {step: 0 for step in ATTEMPT_STEPS}


class LoopState:
    def __init__(self, ws: Path):
        self.ws = ws
        self.path = ws / "loop_state.json"
        self.order: list[str] = []
        self.modules: dict[str, dict] = {}

    # ---------- 载入/初始化 ----------

    def load_or_init(self) -> bool:
        """载入既有状态；不存在则自 deps.json 初始化。返回是否可用。"""
        deps_path = self.ws / "P1" / "modules" / "deps.json"
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.order = data.get("order") or []
            self.modules = data.get("modules") or {}
            # 存量兼容：旧状态机 attempts 无 p5 桶——读入时补零
            for mod in self.modules.values():
                att = mod.get("attempts")
                if isinstance(att, dict):
                    for step in ATTEMPT_STEPS:
                        att.setdefault(step, 0)
            if self.order and set(self.order) == set(self.modules):
                return True
            print("[porter] loop: loop_state.json 结构异常——尝试自 deps.json 重建")
        if not deps_path.exists():
            print(f"[porter] loop: 缺少 {deps_path}（先跑 p0/p1）")
            return False
        deps = json.loads(deps_path.read_text(encoding="utf-8"))
        self.order = list(deps.get("order") or [])
        self.modules = {m: {"phase": "pending", "attempts": _zero_attempts()}
                        for m in self.order}
        self.save()
        print(f"[porter] loop: 初始化 loop_state（{len(self.order)} 模块，"
              f"拓扑序）")
        return True

    def save(self) -> None:
        self.modules.setdefault  # no-op 保持字段访问习惯
        data = {"order": self.order,
                "modules": self.modules,
                "updated": datetime.now().isoformat(timespec="seconds")}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- 查询 ----------

    def phase_of(self, module: str) -> str | None:
        return (self.modules.get(module) or {}).get("phase")

    def pointer(self) -> str | None:
        """首个非 done 模块（断点）。"""
        for m in self.order:
            if self.modules.get(m, {}).get("phase") != "done":
                return m
        return None

    def done_set(self) -> set[str]:
        return {m for m in self.order if self.phase_of(m) == "done"}

    def attempts(self, module: str, step: str) -> int:
        return ((self.modules.get(module) or {}).get("attempts") or {}).get(
            step, 0)

    # ---------- 迁移 ----------

    def set_phase(self, module: str, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"非法 phase: {phase}")
        self.modules.setdefault(module, {"phase": "pending",
                                         "attempts": _zero_attempts()})
        self.modules[module]["phase"] = phase
        self.save()

    def bump(self, module: str, step: str) -> int:
        """attempts+1 并返回新值。超界由调用方判（exit 3）。"""
        mod = self.modules.setdefault(module, {"phase": "pending",
                                               "attempts": _zero_attempts()})
        mod.setdefault("attempts", _zero_attempts())
        mod["attempts"][step] = mod["attempts"].get(step, 0) + 1
        self.save()
        return mod["attempts"][step]

    def reset_attempts(self, module: str, step: str) -> None:
        """人工重试指令（answers.md `## retry <module>`）清零。"""
        mod = self.modules.get(module)
        if mod and mod.get("attempts"):
            mod["attempts"][step] = 0
            self.save()


def parse_answers(ws: Path) -> dict[str, str]:
    """解析 ws/answers.md（T3 既有惯例）：`## <键>` 节 = 一条人工答案。

    键形态：<linux_api>（gap 答案）或 `retry <module>`（重试某阶段）。
    """
    path = ws / "answers.md"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    key = None
    buf: list[str] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## "):
            if key is not None:
                out[key] = "\n".join(buf).strip()
            key = ln[3:].strip()
            buf = []
        elif key is not None:
            buf.append(ln)
    if key is not None:
        out[key] = "\n".join(buf).strip()
    return {k: v for k, v in out.items() if v}


def consume_answers(ws: Path, keys: list[str]) -> dict[str, str]:
    """取走指定键的答案并从 answers.md 删除对应节（已消费即移除）。"""
    path = ws / "answers.md"
    if not path.exists():
        return {}
    answers = parse_answers(ws)
    taken = {k: answers[k] for k in keys if k in answers}
    if not taken:
        return {}
    remaining = {k: v for k, v in answers.items() if k not in taken}
    lines = ["# 人工答案（T3/loop 共用；被消费的节会自动移除）", ""]
    for k, v in remaining.items():
        lines += [f"## {k}", "", v, ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return taken
