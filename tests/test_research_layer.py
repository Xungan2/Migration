"""test_research_layer.py — 研究交付物层（校验/收割/双模型加载）单测。

覆盖（全合成数据，零真实 OS/驱动事实——红线纪律）：
  1. _split_deliverable：尾部 JSON 块分离 / 无块 / 坏块 / 非 module 形状
  2. _validate_research：合法通过；module 不符 / verdict 出表 /
     mappings 空 / 缺数组 / 条目缺字段 → problems；证据路径解析不到 →
     仅 warning；绝对路径与目标树相对路径可解析
  3. _harvest_research：词典/契约登记表/泊车三路追加 + 机器排版 +
     幂等（节标题已存在不重复追加）
  4. _load_models：合法双模型 / 两值一致合法 / 缺块 / 缺键 / 裸模型名
"""
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.exp import mono as mono_mod


def _deliverable(module="fx-a", **over):
    data = {
        "module": module,
        "mappings": [
            {"symbol": "api_x", "verdict": "equivalent",
             "usage": "目标等价物 api_x_t", "evidence": "lib/core.rs:12",
             "notes": "语义一致，无注意事项"},
            {"symbol": "api_y", "verdict": "adapt",
             "usage": "组合原语 helper_y", "evidence": "lib/util.rs:30-42"}],
        "contracts": [
            {"name": "HookX", "signature": "type HookX = fn(&mut Dev) -> i32",
             "anchor": "spec_a.c:10", "consumers": "fx-a、fx-b",
             "conflicts_with": ""}],
        "prunes": [{"item": "观测面 dump", "reason": "纯观测",
                    "anchor": "spec_a.c:20"}],
        "parking": [
            {"item": "平台原语 P", "missing": "目标树无对应层",
             "handling": "注入谓词绕行", "refill_when": "平台层落地"}],
        "read_list": [{"path": "lib/core.rs", "purpose": "词汇先例"}],
        "negatives": [
            {"claim": "原语 Q 不存在", "evidence": "rg 全树无命中"}],
        "open_questions": [
            {"question": "锁习语取舍", "verify": "对照同类驱动先例"}],
    }
    data.update(over)
    return _rebuild(data), data


def _rebuild(data: dict, sections: bool = True) -> str:
    head = ("## 研究叙事\n\n### 适配架构与整合方案\n\n（合成）直译。\n\n"
            "### 可测面评估\n\n（合成）全部可执行验证。\n\n"
            if sections else
            "## 研究叙事\n\n本模块为虚构词汇层，整体策略是直译。\n\n")
    return head + "```json\n" + json.dumps(data, ensure_ascii=False) \
        + "\n```\n"


class TestSplitDeliverable(unittest.TestCase):

    def test_split_ok(self):
        body, _ = _deliverable()
        prose, data = mono_mod._split_deliverable(body)
        self.assertEqual(data["module"], "fx-a")
        self.assertNotIn("```json", prose)
        self.assertIn("研究叙事", prose)

    def test_no_block(self):
        prose, data = mono_mod._split_deliverable("只有正文\n")
        self.assertIsNone(data)
        self.assertEqual(prose, "只有正文\n")

    def test_bad_json_or_wrong_shape(self):
        for text in ("```json\n{bad}\n```\n",
                     '```json\n{"modules": []}\n```\n'):
            _prose, data = mono_mod._split_deliverable(text)
            self.assertIsNone(data)

    def test_last_block_wins(self):
        text = ('```json\n{"module": "old"}\n```\n正文\n'
                '```json\n{"module": "new"}\n```\n')
        _prose, data = mono_mod._split_deliverable(text)
        self.assertEqual(data["module"], "new")


class TestValidateResearch(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="research_layer_"))
        self.tree = self.tmp / "tree"
        (self.tree / "lib").mkdir(parents=True)
        (self.tree / "lib" / "core.rs").write_text("unit core;\n",
                                                   encoding="utf-8")
        (self.tree / "lib" / "util.rs").write_text("unit util;\n",
                                                   encoding="utf-8")
        (self.tree / "home" / "drv").mkdir(parents=True)
        (self.tree / "home" / "drv" / "scaffold.cx").write_text(
            "unit scaffold;\n", encoding="utf-8")
        self.ws = self.tmp / "ws"
        self.ws.mkdir()

    def test_valid_passes_with_resolved_evidence(self):
        body, _ = _deliverable()
        r = mono_mod._validate_research(body, "fx-a", self.tree,
                                        "home/drv", self.ws)
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(r["problems"], [])
        self.assertEqual(r["warnings"], [])

    def test_absolute_path_resolves(self):
        _b, data = _deliverable()
        data["mappings"][0]["evidence"] = f"{self.tree / 'lib/core.rs'}:5"
        body = _rebuild(data)
        r = mono_mod._validate_research(body, "fx-a", self.tree,
                                        "home/drv", self.ws)
        self.assertTrue(r["ok"])
        self.assertEqual(r["warnings"], [])

    def test_unresolvable_evidence_warns_not_blocks(self):
        _b, data = _deliverable()
        data["mappings"][0]["evidence"] = "no/such/file.rs:1"
        body = _rebuild(data)
        r = mono_mod._validate_research(body, "fx-a", self.tree,
                                        "home/drv", self.ws)
        self.assertTrue(r["ok"], r["problems"])
        self.assertTrue(any("未能解析" in w for w in r["warnings"]))

    def test_missing_required_sections(self):
        # 硬性要求 3：叙事缺「适配架构与整合方案」「可测面评估」节
        # 标题 → 判败（纯数据模块允许写"无"，但不许略节）
        _b, data = _deliverable()
        body = _rebuild(data, sections=False)
        r = mono_mod._validate_research(body, "fx-a", self.tree,
                                        "home/drv", self.ws)
        self.assertFalse(r["ok"])
        self.assertEqual(
            len([p for p in r["problems"] if "必写节" in p]), 2)

    def test_no_json_block(self):
        r = mono_mod._validate_research("无结构化块\n", "fx-a", self.tree,
                                        "home/drv", self.ws)
        self.assertFalse(r["ok"])
        self.assertTrue(any("```json" in p for p in r["problems"]))

    def test_module_mismatch(self):
        body, _ = _deliverable(module="fx-b")
        r = mono_mod._validate_research(body, "fx-a", self.tree,
                                        "home/drv", self.ws)
        self.assertFalse(r["ok"])
        self.assertTrue(any("module" in p for p in r["problems"]))

    def test_bad_verdict(self):
        _b, data = _deliverable()
        data["mappings"][0]["verdict"] = "same"
        body = _rebuild(data)
        r = mono_mod._validate_research(body, "fx-a", self.tree,
                                        "home/drv", self.ws)
        self.assertFalse(r["ok"])
        self.assertTrue(any("词表" in p for p in r["problems"]))

    def test_empty_mappings_rejected(self):
        _b, data = _deliverable()
        data["mappings"] = []
        body = _rebuild(data)
        r = mono_mod._validate_research(body, "fx-a", self.tree,
                                        "home/drv", self.ws)
        self.assertFalse(r["ok"])
        self.assertTrue(any("mappings 为空" in p for p in r["problems"]))

    def test_missing_array_and_fields(self):
        _b, data = _deliverable()
        del data["negatives"]                      # 缺数组
        data["contracts"][0]["signature"] = "  "   # 必填为空
        body = _rebuild(data)
        r = mono_mod._validate_research(body, "fx-a", self.tree,
                                        "home/drv", self.ws)
        self.assertFalse(r["ok"])
        self.assertTrue(any("缺 negatives 数组" in p for p in r["problems"]))
        self.assertTrue(any("signature" in p for p in r["problems"]))


class TestHarvestResearch(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="research_hv_"))
        self.exp = self.tmp / "exp-mono"
        self.exp.mkdir()
        (self.exp / "mapping-notes.md").write_text(
            "# 迁移词典（JIT 接力）\n\n> 每条一行。\n", encoding="utf-8")
        (self.exp / "parking.md").write_text("# 泊车记录\n\n> 条目。\n",
                                             encoding="utf-8")

    def test_harvest_three_targets(self):
        _body, data = _deliverable()
        counts = mono_mod._harvest_research(self.exp, "fx-a", data)
        self.assertEqual(counts, {"dictionary": 2, "contracts": 1,
                                  "parking": 1})
        notes = (self.exp / "mapping-notes.md").read_text(encoding="utf-8")
        self.assertIn("## fx-a", notes)
        self.assertIn("- api_x | equivalent | 目标等价物 api_x_t"
                      " | lib/core.rs:12（注意：语义一致，无注意事项）",
                      notes)
        reg = (self.exp / "contracts.md").read_text(encoding="utf-8")
        self.assertIn("契约登记表", reg)
        self.assertIn("- **HookX**：`type HookX = fn(&mut Dev) -> i32`",
                      reg)
        self.assertIn("冲突 无", reg)
        park = (self.exp / "parking.md").read_text(encoding="utf-8")
        self.assertIn("## fx-a 研究轮", park)
        self.assertIn("回填条件：平台层落地", park)

    def test_harvest_idempotent(self):
        _body, data = _deliverable()
        mono_mod._harvest_research(self.exp, "fx-a", data)
        counts = mono_mod._harvest_research(self.exp, "fx-a", data)
        self.assertEqual(counts, {"dictionary": 0, "contracts": 0,
                                  "parking": 0})
        notes = (self.exp / "mapping-notes.md").read_text(encoding="utf-8")
        self.assertEqual(notes.count("- api_x |"), 1)

    def test_harvest_empty_sections_noop(self):
        _body, data = _deliverable()
        data.update(mappings=[], contracts=[], parking=[])
        counts = mono_mod._harvest_research(self.exp, "fx-a", data)
        self.assertEqual(counts, {"dictionary": 0, "contracts": 0,
                                  "parking": 0})
        self.assertFalse((self.exp / "contracts.md").exists())


class TestLoadModels(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="research_cfg_"))
        self.cfg = self.tmp / "config.json"

    def _write(self, obj):
        self.cfg.write_text(json.dumps(obj), encoding="utf-8")

    def test_valid_pair(self):
        self._write({"models": {"reasoning": "prov/strong",
                                "coding": "prov/cheap"}})
        r, c = mono_mod._load_models(self.cfg)
        self.assertEqual((r, c), ("prov/strong", "prov/cheap"))

    def test_same_values_legal(self):
        self._write({"models": {"reasoning": "prov/x",
                                "coding": "prov/x"}})
        r, c = mono_mod._load_models(self.cfg)
        self.assertEqual(r, c)

    def test_missing_block(self):
        self._write({"model": "prov/x"})
        r, err = mono_mod._load_models(self.cfg)
        self.assertIsNone(r)
        self.assertIn("models", err)

    def test_missing_key(self):
        self._write({"models": {"reasoning": "prov/x"}})
        r, err = mono_mod._load_models(self.cfg)
        self.assertIsNone(r)
        self.assertIn("coding", err)

    def test_bare_model_name(self):
        self._write({"models": {"reasoning": "Bare-Name",
                                "coding": "prov/x"}})
        r, err = mono_mod._load_models(self.cfg)
        self.assertIsNone(r)
        self.assertIn("provider 前缀", err)

    def test_unreadable_config(self):
        r, err = mono_mod._load_models(self.tmp / "nope.json")
        self.assertIsNone(r)
        self.assertIn("不可读", err)


if __name__ == "__main__":
    unittest.main()
