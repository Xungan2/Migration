"""T1 --intent-file（迁移意图文件）的功能性测试（无 agent）。

覆盖（范围声明层批次 1：P0 入口）：
  1. 创建带意图：goals.md 拷贝入工作区 + project.json 记
     intent_file/intent_source，内容与源一致
  2. 创建不带意图：project.json 无 intent_file 字段、无 goals.md（向后兼容）
  3. validate 负向：intent 文件不存在 / 空文件 → InputError
  4. resume 补拷：无记录工作区 + backfill_intent → goals.md + 记录；
     重复调用幂等（一致跳过，产物不变）
  5. resume 冲突：已有 goals.md + 传入不同内容 → 不覆盖（内容保持原样）
  6. resume 恢复：goals.md 被删 + backfill 同文件 → 恢复
  7. backfill 负向：文件不存在/为空 → InputError

运行：python3 tests/test_env_inputs.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest

from porter.env import inputs as T1
from porter.env.inputs import InputError


def ok(name, cond, extra=""):
    """断言助手：失败即抛（unittest discover / 直跑两用法通用）。"""
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_tree(tmp: Path) -> tuple[Path, Path]:
    """合成最小输入：驱动目录（1 个 .c）+ 可写目标树。"""
    drv = tmp / "drv"
    drv.mkdir(parents=True)
    (drv / "a.c").write_text("int a_fn(void)\n{\n\treturn 0;\n}\n",
                             encoding="utf-8")
    tos = tmp / "tos"
    tos.mkdir()
    return drv, tos


def _mk_intent(tmp: Path, text: str = "仅迁移 dm-zero 及其依赖闭包\n") -> Path:
    p = tmp / "goals.md"
    p.write_text(text, encoding="utf-8")
    return p


class TestIntentFile(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t1_intent_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.drv, self.tos = _mk_tree(self.tmp)

    def _ws(self) -> Path:
        return self.tmp / "ws"

    def test_a_create_with_intent(self):
        intent = _mk_intent(self.tmp)
        ws = T1.init_workspace(self._ws(), self.drv, self.tos, [],
                               intent_file=intent)
        ok("A1 goals.md 拷贝入工作区", (ws / "goals.md").exists())
        ok("A2 内容与源一致",
           (ws / "goals.md").read_text(encoding="utf-8")
           == intent.read_text(encoding="utf-8"))
        proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
        ok("A3 project.json 记 intent_file",
           proj.get("intent_file") == "goals.md")
        ok("A4 project.json 记 intent_source（绝对路径）",
           proj.get("intent_source") == str(intent.resolve()))
        ok("A5 原文件未被移动", intent.exists())

    def test_b_create_without_intent(self):
        ws = T1.init_workspace(self._ws(), self.drv, self.tos, [])
        proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
        ok("B1 无 intent_file 字段（向后兼容）", "intent_file" not in proj)
        ok("B2 无 intent_source 字段", "intent_source" not in proj)
        ok("B3 无 goals.md", not (ws / "goals.md").exists())

    def test_c_validate_negative(self):
        intent = _mk_intent(self.tmp)
        intent.unlink()
        with self.assertRaises(InputError):
            T1.validate(self.drv, self.tos, [], intent_file=intent)
        ok("C1 不存在的 intent 文件 → InputError", True)
        empty = self.tmp / "empty.md"
        empty.write_text("", encoding="utf-8")
        with self.assertRaises(InputError):
            T1.validate(self.drv, self.tos, [], intent_file=empty)
        ok("C2 空 intent 文件 → InputError", True)

    def test_d_backfill_new_and_idempotent(self):
        ws = T1.init_workspace(self._ws(), self.drv, self.tos, [])
        intent = _mk_intent(self.tmp)
        T1.backfill_intent(ws, ws / "project.json", intent)
        ok("D1 补拷后 goals.md 存在", (ws / "goals.md").exists())
        proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
        ok("D2 补记 intent_file", proj.get("intent_file") == "goals.md")
        before = ((ws / "goals.md").read_bytes(),
                  (ws / "project.json").read_bytes())
        T1.backfill_intent(ws, ws / "project.json", intent)
        ok("D3 重复调用幂等（一致跳过）",
           ((ws / "goals.md").read_bytes(),
            (ws / "project.json").read_bytes()) == before)

    def test_e_backfill_conflict_no_overwrite(self):
        intent_a = _mk_intent(self.tmp, "意图 A\n")
        ws = T1.init_workspace(self._ws(), self.drv, self.tos, [],
                               intent_file=intent_a)
        intent_b = self.tmp / "goals_b.md"
        intent_b.write_text("意图 B（不同内容）\n", encoding="utf-8")
        T1.backfill_intent(ws, ws / "project.json", intent_b)
        ok("E1 冲突不覆盖——内容仍为 A",
           (ws / "goals.md").read_text(encoding="utf-8") == "意图 A\n")

    def test_f_backfill_restore_missing(self):
        intent = _mk_intent(self.tmp)
        ws = T1.init_workspace(self._ws(), self.drv, self.tos, [],
                               intent_file=intent)
        (ws / "goals.md").unlink()
        T1.backfill_intent(ws, ws / "project.json", intent)
        ok("F1 goals.md 缺失 → 从传入文件恢复",
           (ws / "goals.md").read_text(encoding="utf-8")
           == intent.read_text(encoding="utf-8"))

    def test_g_backfill_negative(self):
        ws = T1.init_workspace(self._ws(), self.drv, self.tos, [])
        ghost = self.tmp / "ghost.md"
        with self.assertRaises(InputError):
            T1.backfill_intent(ws, ws / "project.json", ghost)
        ok("G1 backfill 不存在文件 → InputError", True)
        empty = self.tmp / "empty2.md"
        empty.write_text("", encoding="utf-8")
        with self.assertRaises(InputError):
            T1.backfill_intent(ws, ws / "project.json", empty)
        ok("G2 backfill 空文件 → InputError", True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
