"""scope（迁移范围闭包）的功能性测试（无 agent）。

覆盖（批次 2a：范围声明层生成侧）：
  1. split_strategy_output：正文+json 块分离 / 无块原样返回 / 坏 JSON
     跳过取最后合规块 / 非 modules 对象不算 scope
  2. scope_files：模块文件并集
  3. validate_and_normalize：合法规范化回写（排序去重）/ 文件不存在 /
     越出驱动目录（公共头拒收）/ modules 空 / files 空 / 全无 .c
  4. load_scope：无文件 → None；有文件 → 集合
  5. cross_check：清单外排除警告 / 零定义文件警告（合成驱动）
  6. 兼容：_task_data 无 goals.md 时不含意图节；run_strategy 协议分支

运行：python3 tests/test_scope.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest

from porter.common import scope as SC


def ok(name, cond, extra=""):
    """断言助手：失败即抛（unittest discover / 直跑两用法通用）。"""
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_drv(tmp: Path) -> Path:
    drv = tmp / "drv"
    (drv / "sub").mkdir(parents=True)
    (drv / "core.c").write_text(
        "int core_fn(void)\n{\n\treturn 0;\n}\n", encoding="utf-8")
    (drv / "leaf.c").write_text(
        "int core_fn(void);\n\nint leaf_fn(void)\n{\n\treturn core_fn();\n}\n",
        encoding="utf-8")
    (drv / "defs.h").write_text("/* data-only header, no definitions */\n",
                                encoding="utf-8")
    return drv


class TestSplit(unittest.TestCase):

    def test_a_split_with_json_block(self):
        text = ("# 策略\n\n正文 A 段。\n\n```json\n"
                '{"modules": [{"name": "m1", "files": ["a.c"]}]}\n'
                "```\n")
        md, scope = SC.split_strategy_output(text)
        ok("A1 json 块被抽出", scope is not None
           and scope["modules"][0]["name"] == "m1")
        ok("A2 正文不含 json 块", "```json" not in md and "正文 A 段" in md)

    def test_b_no_block_passthrough(self):
        text = "# 策略\n\n纯正文，无围栏块。\n"
        md, scope = SC.split_strategy_output(text)
        ok("B1 无块 → scope None", scope is None)
        ok("B2 原文返回", md == text)

    def test_c_bad_json_skipped(self):
        text = ("# 策略\n\n```json\n{bad json}\n```\n\n收尾。\n\n```json\n"
                '{"modules": []}\n```\n')
        md, scope = SC.split_strategy_output(text)
        ok("C1 坏 JSON 跳过、取最后合规块",
           scope is not None and scope == {"modules": []})
        ok("C2 两块都从正文移除", "```json" not in md)

    def test_d_non_scope_json_ignored(self):
        text = '# 策略\n\n```json\n{"answer": 42}\n```\n'
        md, scope = SC.split_strategy_output(text)
        ok("D1 非 modules 对象不算 scope", scope is None)


class TestValidateLoad(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scope_val_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.drv = _mk_drv(self.tmp)
        self.ws = self.tmp / "ws"
        (self.ws / "P1").mkdir(parents=True)

    def _scope(self, files_by_mod):
        return {"driver_name": "test-drv",
                "modules": [{"name": n, "function": n, "files": fs}
                            for n, fs in files_by_mod.items()]}

    def test_e_valid_normalized(self):
        sc = self._scope({"m-b": ["defs.h"], "m-a": ["core.c", "core.c",
                                                     "leaf.c"]})
        defects = SC.validate_and_normalize(sc, self.drv, self.ws)
        ok("E1 合法无缺陷", defects == [], str(defects))
        out = json.loads((self.ws / "P1" / "scope.json")
                         .read_text(encoding="utf-8"))
        ok("E2 模块按名排序", [m["name"] for m in out["modules"]]
           == ["m-a", "m-b"])
        ok("E3 文件去重排序", out["modules"][0]["files"]
           == ["core.c", "leaf.c"])
        ok("E4 load_scope 返回并集",
           SC.load_scope(self.ws) == {"core.c", "leaf.c", "defs.h"})

    def test_f_negative(self):
        d = SC.validate_and_normalize(
            self._scope({"m": ["ghost.c"]}), self.drv, self.ws)
        ok("F1 文件不存在", any("不存在" in x for x in d), str(d))
        (self.tmp / "outside.h").write_text("#define Y 2\n", encoding="utf-8")
        d = SC.validate_and_normalize(
            self._scope({"m": ["../outside.h"]}), self.drv, self.ws)
        ok("F2 越出驱动目录", any("越出" in x for x in d), str(d))
        d = SC.validate_and_normalize({"modules": []}, self.drv, self.ws)
        ok("F3 modules 空", d != [])
        d = SC.validate_and_normalize(
            {"modules": [{"name": "m", "files": []}]}, self.drv, self.ws)
        ok("F4 files 空", any("files" in x for x in d), str(d))
        d = SC.validate_and_normalize(
            self._scope({"m": ["defs.h"]}), self.drv, self.ws)
        ok("F5 并集无 .c", any(".c" in x for x in d), str(d))
        ok("F6 非法时不落盘", not (self.ws / "P1" / "scope.json").exists())

    def test_g_load_missing(self):
        ok("G1 无 scope.json → None", SC.load_scope(self.ws) is None)

    def test_h_cross_check(self):
        warns = SC.cross_check({"core.c"}, self.drv)
        ok("H1 排除文件有警告", any("排除" in w for w in warns), str(warns))
        warns = SC.cross_check({"core.c", "defs.h"}, self.drv)
        ok("H2 defs.h 无定义条目警告", any("无定义" in w for w in warns),
           str(warns))


class TestTaskData(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scope_task_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.drv = _mk_drv(self.tmp)
        self.ws = self.tmp / "ws"
        (self.ws / "P1").mkdir(parents=True)

    def test_i_goals_injection(self):
        from porter.divide import strategy as ST
        ws = self.tmp / "ws2"
        (ws / "P1").mkdir(parents=True)
        (ws / "goals.md").write_text("仅保留设备号 8086:100E\n",
                                     encoding="utf-8")
        proj = {"category": ["net"], "materials": []}
        data = ST._task_data(ws, proj, self.drv)
        ok("I1 意图节在场", "迁移意图" in data and "8086:100E" in data)
        ok("I2 最高优先级标注", "最高优先级" in data)

    def test_j_no_goals_compat(self):
        from porter.divide import strategy as ST
        proj = {"category": [], "materials": []}
        data = ST._task_data(self.ws, proj, self.drv)
        ok("J1 无 goals.md 时无意图节", "迁移意图" not in data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
