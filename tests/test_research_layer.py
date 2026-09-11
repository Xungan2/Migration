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
                                  "parking": 1, "prunes": 1,
                                  "negatives": 1})
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
                                  "parking": 0, "prunes": 0,
                                  "negatives": 0})
        notes = (self.exp / "mapping-notes.md").read_text(encoding="utf-8")
        self.assertEqual(notes.count("- api_x |"), 1)

    def test_harvest_empty_sections_noop(self):
        _body, data = _deliverable()
        data.update(mappings=[], contracts=[], parking=[], prunes=[],
                    negatives=[])
        counts = mono_mod._harvest_research(self.exp, "fx-a", data)
        self.assertEqual(counts, {"dictionary": 0, "contracts": 0,
                                  "parking": 0, "prunes": 0,
                                  "negatives": 0})
        self.assertFalse((self.exp / "contracts.md").exists())


class TestHarvestPrunes(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="research_pr_"))
        self.exp = self.tmp / "exp-mono"
        self.exp.mkdir()

    def test_content_and_header(self):
        _body, data = _deliverable()
        counts = mono_mod._harvest_research(self.exp, "fx-a", data)
        self.assertEqual(counts["prunes"], 1)
        text = (self.exp / "prunes.md").read_text(encoding="utf-8")
        self.assertIn("# 裁剪台账", text)
        self.assertIn("## fx-a", text)
        self.assertIn("- **观测面 dump**：纯观测（锚 spec_a.c:20）", text)

    def test_anchor_optional(self):
        _body, data = _deliverable()
        data["prunes"] = [{"item": "死类型 T", "reason": "裁剪刀"}]
        mono_mod._harvest_research(self.exp, "fx-a", data)
        text = (self.exp / "prunes.md").read_text(encoding="utf-8")
        self.assertIn("- **死类型 T**：裁剪刀\n", text)
        self.assertNotIn("（锚", text)

    def test_idempotent(self):
        _body, data = _deliverable()
        mono_mod._harvest_research(self.exp, "fx-a", data)
        counts = mono_mod._harvest_research(self.exp, "fx-a", data)
        self.assertEqual(counts["prunes"], 0)
        text = (self.exp / "prunes.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("- **观测面 dump**"), 1)


class TestHarvestNegatives(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="research_ng_"))
        self.exp = self.tmp / "exp-mono"
        self.exp.mkdir()

    @staticmethod
    def _entries(claim, evidence="rg 全树无命中"):
        return [{"claim": claim, "evidence": evidence}]

    def test_new_entry_line(self):
        n = mono_mod._harvest_negatives(self.exp, "fx-a",
                                        self._entries("原语 Q 不存在"))
        self.assertEqual(n, 1)
        sig = mono_mod._claim_sig("原语 Q 不存在")
        text = (self.exp / "negatives.md").read_text(encoding="utf-8")
        self.assertIn(f"- [{sig}] 原语 Q 不存在"
                      f"（证据 rg 全树无命中；首报 fx-a）", text)

    def test_cross_module_reconfirm_written(self):
        # 纯复证（新条目 0）也必须写盘——曾因只在 new_count>0 时落盘而丢
        mono_mod._harvest_negatives(self.exp, "fx-a",
                                    self._entries("原语 Q 不存在"))
        n = mono_mod._harvest_negatives(
            self.exp, "fx-b", self._entries("原语 Q   不存在"))  # 空白变体
        self.assertEqual(n, 0)                        # 不算新条目
        text = (self.exp / "negatives.md").read_text(encoding="utf-8")
        self.assertIn("；复证：fx-b）", text)           # 但复证要落盘
        self.assertEqual(text.count("首报 fx-a"), 1)

    def test_second_reconfirm_appends(self):
        for mod in ("fx-a", "fx-b", "fx-c"):
            mono_mod._harvest_negatives(self.exp, mod,
                                        self._entries("原语 Q 不存在"))
        text = (self.exp / "negatives.md").read_text(encoding="utf-8")
        self.assertIn("复证：fx-b、fx-c）", text)

    def test_same_module_idempotent(self):
        mono_mod._harvest_negatives(self.exp, "fx-a",
                                    self._entries("原语 Q 不存在"))
        n = mono_mod._harvest_negatives(self.exp, "fx-a",
                                        self._entries("原语 Q 不存在"))
        self.assertEqual(n, 0)
        text = (self.exp / "negatives.md").read_text(encoding="utf-8")
        self.assertNotIn("复证：fx-a", text)
        self.assertEqual(len(text.strip().splitlines()), 1)


class TestFixEpisodes(unittest.TestCase):

    @staticmethod
    def _round(seg, ok=None, sig=None, log=None):
        return {"seg": seg, "stem": f"S{seg}",
                "static": (None if ok is None else
                           {"ok": ok, "sig": sig, "log": log})}

    def test_pairing_skips_repeat_fails(self):
        rounds = [self._round(1, True, None),
                  self._round(2, False, "aaa"),
                  self._round(3, False, "aaa"),      # 同签名连败只记首个
                  self._round(4, True, None)]
        eps = mono_mod._fix_episodes({"rounds": rounds})
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["fail"]["seg"], 2)
        self.assertEqual(eps[0]["pass"]["seg"], 4)

    def test_two_episodes(self):
        rounds = [self._round(2, False, "aaa"), self._round(3, True, None),
                  self._round(4, False, "bbb"), self._round(5, True, None)]
        self.assertEqual(len(mono_mod._fix_episodes({"rounds": rounds})), 2)

    def test_trailing_failure_not_episode(self):
        rounds = [self._round(1, True, None), self._round(2, False, "aaa")]
        self.assertEqual(mono_mod._fix_episodes({"rounds": rounds}), [])

    def test_fail_without_sig_ignored(self):
        rounds = [self._round(2, False, ""), self._round(3, True, None)]
        self.assertEqual(mono_mod._fix_episodes({"rounds": rounds}), [])


class TestRecordFixes(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="research_fx_"))
        self.exp = self.tmp / "exp-mono"
        self.exp.mkdir()
        self.log = self.tmp / "S2_static.log"
        self.log.write_text(
            "filler\n" * 30
            + "\x1b[31merror[E0432]\x1b[0m: unresolved import `ostd::foo`\n"
            "  --> home/drv/regs.rs:88:12\n"
            "note: 建议改用 ostd::bar\n", encoding="utf-8")

    def _episodes(self):
        return [{"fail": {"seg": 2, "static": {"ok": False, "sig": "ab12",
                                               "log": str(self.log)}},
                 "pass": {"seg": 4, "static": {"ok": True}}}]

    def test_writes_inline_tail(self):
        n = mono_mod._record_fixes(self.exp, "fx-a", self._episodes())
        self.assertEqual(n, 1)
        text = (self.exp / "fixes.md").read_text(encoding="utf-8")
        self.assertIn("# 修复记录（翻车与救活）", text)
        self.assertIn("## fx-a", text)
        self.assertIn("- 轮 2→4｜签名 `ab12`｜修复 2 段", text)
        self.assertIn("> 失败尾文（摘）：", text)
        self.assertIn("error[E0432]", text)          # ANSI 已剥
        self.assertNotIn("\x1b[", text)

    def test_idempotent(self):
        mono_mod._record_fixes(self.exp, "fx-a", self._episodes())
        self.assertEqual(
            mono_mod._record_fixes(self.exp, "fx-a", self._episodes()), 0)

    def test_missing_log_graceful(self):
        eps = [{"fail": {"seg": 1, "static": {"ok": False, "sig": "x",
                                              "log": None}},
                "pass": {"seg": 2, "static": {"ok": True}}}]
        self.assertEqual(mono_mod._record_fixes(self.exp, "fx-a", eps), 1)
        text = (self.exp / "fixes.md").read_text(encoding="utf-8")
        self.assertIn("（失败日志指针缺失）", text)


class TestWriteReportKnowledge(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="research_rp_"))
        self.ws = self.tmp / "ws"
        self.exp = self.ws / "exp-mono"
        self.exp.mkdir(parents=True)
        (self.exp / "prunes.md").write_text(
            "# 裁剪台账\n\n## fx-a\n- **A**：r\n- **B**：r\n",
            encoding="utf-8")
        (self.exp / "negatives.md").write_text(
            "# 已排除死路\n- [s1] c1（证据 e；首报 fx-a）\n"
            "- [s2] c2（证据 e；首报 fx-a）\n", encoding="utf-8")
        (self.exp / "fixes.md").write_text(
            "# 修复记录\n\n## fx-a\n- 轮 2→4｜签名 `ab`\n",
            encoding="utf-8")

    def test_knowledge_section(self):
        ledger = {"modules": {"fx-a": {
            "status": "pass",
            "research": {"status": "pass",
                         "open_questions": [{"question": "q1",
                                             "verify": "v"}]},
            "decl_fixed": ["p1", "p2"]}}}
        proj = {"target_os": "/tmp/t", "linux_driver": "/tmp/l"}
        manifest = {"driver_home": "home/drv", "driver": "fx"}
        mono_mod._write_report(self.ws, self.exp, ledger, ["fx-a"], proj,
                               manifest)
        text = (self.exp / "report.md").read_text(encoding="utf-8")
        self.assertIn("## 知识与修复汇总", text)
        self.assertIn("裁剪台账：2 条", text)
        self.assertIn("负结论：2 条", text)
        self.assertIn("修复记录：1 条", text)
        self.assertIn("研究存疑点（open_questions，内容见 ledger）：fx-a×1",
                      text)
        self.assertIn("记账修正史（decl_fixed，重试修掉的问题清单）："
                      "fx-a×2", text)


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
