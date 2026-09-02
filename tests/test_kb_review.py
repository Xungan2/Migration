"""知识审核/分类/晋升测试（review.py——随机知识后段）。

覆盖：
A. build_cp5_material：候选队列/草稿清点/健康报告（hits+veto）
B. classify_candidates：NO_AGENT 跳过 / mock agent 改判回写
C. promote_candidate：建议类晋升/改判（--to）留档/gaps 嵌套/未知域/
   不存在 id/重名递增
D. reject_candidate

运行：python3 -m unittest tests.test_kb_review
"""
import sys, os, json, tempfile, shutil, unittest.mock as mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unittest


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def rd(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def wj(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                 encoding="utf-8")


LONG1 = ("根因是构建缓存参数失效，修复须显式传 console 参数"
         "（ktest 静默类教训）")
LONG2 = "IOAPIC 仅边沿触发，PCI INTx 永不送达（平台缺口，泊车类）"


class TestKbReview(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        from porter.bootstrap import kb, candidates as C, review as R
        self.kb, self.C, self.R = kb, C, R
        self._old_temp, self._old_root = kb.TEMP_DIR, kb.KB_ROOT
        self.tmp = Path(tempfile.mkdtemp(prefix="kb_rev_"))
        kb.TEMP_DIR = self.tmp / "ttemp"
        kb.KB_ROOT = self.tmp / "knowledge"
        self.C.CONFIG_PATH = self.tmp / "config.json"
        wj(self.C.CONFIG_PATH, {"kb": {"candidates": True, "dedup": True,
                                       "min_draft_len": 8}})
        self.ws = self.tmp / "ws"
        wj(self.ws / "project.json", {
            "linux_driver": "/x/e1000", "target_os": "/y/asterinas",
            "kb_dir": "corpus"})
        self.corpus = kb.KB_ROOT / "corpus"
        (self.corpus / "pitfalls").mkdir(parents=True)
        wj(self.corpus / "pitfalls" / "INDEX.json", [
            {"file": "ktest.md", "desc": "ktest 坑", "hits": 5},
            {"file": "cold.md", "desc": "冷条目", "hits": 0}])

    def tearDown(self):
        self.kb.TEMP_DIR = self._old_temp
        self.kb.KB_ROOT = self._old_root
        shutil.rmtree(self.tmp)

    def seed(self):
        self.C.record_candidate(self.ws, "gate-answer",
                                "loop.attempts.rx-ring-p4", LONG1,
                                ["P4/rx-ring/logs/"], "pitfalls",
                                scope_extra={"module": "rx-ring"})
        self.C.record_candidate(self.ws, "l4-park", "ics-selftrigger",
                                LONG2, ["P6/reports/l4_criteria.json"],
                                "pitfalls")

    def test_cp5_material(self):
        R = self.R
        print("=== A. CP5 备审材料 ===")
        self.seed()
        # 旁车合并显示：INDEX 5 + 旁车 2 → hits 7
        self.kb.save_hits_sidecar(self.corpus,
                                  {"pitfalls/ktest.md": 2})
        mat = R.build_cp5_material(self.ws)
        txt = mat.read_text(encoding="utf-8")
        ok("M1 候选队列", "cand-0001" in txt and "cand-0002" in txt
           and LONG1[:20] in txt and "kb promote" in txt)
        ok("M2 健康报告（hits 合并值）", "ktest.md（hits 7）" in txt
           and "零咨询" in txt and "cold.md" in txt)
        ok("M3 草稿清点空态", "各域无草稿" in txt)

    def test_classify(self):
        R, C = self.R, self.C
        print("=== B. classify ===")
        self.seed()
        os.environ["PORTER_NO_AGENT"] = "1"
        try:
            ok("C1 NO_AGENT 跳过", R.classify_candidates(self.ws) == 0)
        finally:
            del os.environ["PORTER_NO_AGENT"]
        canned = {"items": [
            {"id": "cand-0001", "class": "runbook", "confidence": "high"},
            {"id": "cand-0002", "class": "gaps", "confidence": "low"},
            {"id": "cand-9999", "class": "gaps", "confidence": "high"},
            {"id": "cand-0001", "class": "bogus", "confidence": "high"}]}
        with mock.patch.object(R.agent, "run_agent",
                               return_value=(0, json.dumps(canned))):
            rc = R.classify_candidates(self.ws)
        led = {c["id"]: c for c in C.load_candidates(self.ws)}
        ok("C2 改判回写", rc == 0
           and led["cand-0001"]["suggested_class"] == "runbook"
           and led["cand-0002"]["suggested_class"] == "gaps")
        ok("C3 非法类/未知 id 忽略",
           "bogus" not in json.dumps(led)
           and "cand-9999" not in led)
        ok("C4 history 留痕",
           any("归类改判" in str(h.get("event", "")) for h in
               led["cand-0001"]["history"]))

    def test_promote_reject(self):
        R, C, kb = self.R, self.C, self.kb
        print("=== C/D. promote / reject ===")
        self.seed()
        # 建议类晋升（pitfalls → 扁平 ns__slug 文件）+ 旁车折叠
        self.kb.save_hits_sidecar(self.corpus, {
            "pitfalls/e1000@asterinas__loop.attempts.rx-ring-p4.md": 3})
        rc = R.promote_candidate(self.ws, "cand-0001")
        f = (self.corpus / "pitfalls" /
             "e1000@asterinas__loop.attempts.rx-ring-p4.md")
        ok("P1 rc=0 文件落盘", rc == 0 and f.exists())
        idx = rd(self.corpus / "pitfalls" / "INDEX.json")
        ok("P2 INDEX 行 + desc 带 ns + 旁车折叠（0+3）", any(
            e["file"].endswith("rx-ring-p4.md")
            and e["desc"].startswith("[e1000@asterinas]")
            and e["hits"] == 3 for e in idx))
        ok("P2b 折叠后旁车清键",
           "pitfalls/e1000@asterinas__loop.attempts.rx-ring-p4.md"
           not in self.kb.load_hits_sidecar(self.corpus))
        ok("P3 出账", all(c["id"] != "cand-0001"
                          for c in C.load_candidates(self.ws)))
        body = f.read_text(encoding="utf-8")
        ok("P4 元数据尾", "来源：gate-answer" in body
           and "命名空间：e1000@asterinas" in body)
        # 改判晋升（--to gaps → 嵌套 ns 目录 + 改判留档）
        rc = R.promote_candidate(self.ws, "cand-0002", to="gaps")
        g = (self.corpus / "gaps" / "e1000@asterinas" /
             "ics-selftrigger.md")
        ok("P5 gaps 嵌套落盘", rc == 0 and g.exists())
        ok("P6 改判留档",
           "类别改判：建议 pitfalls → 实际 gaps" in
           g.read_text(encoding="utf-8"))
        gidx = rd(self.corpus / "gaps" / "INDEX.json")
        ok("P7 gaps INDEX 行", any(
            str(e["file"]).startswith("e1000@asterinas/ics-selftrigger")
            for e in gidx))
        # 未知域 / 不存在 id / 无 kb_dir
        self.C.record_candidate(self.ws, "x",
                                "loop.attempts.rx-ring-p4", LONG1 + "v3")
        ok("P8 未知域 → rc 1",
           R.promote_candidate(self.ws, "cand-0001", to="bogus") == 1)
        ok("P9 不存在 id → rc 1",
           R.promote_candidate(self.ws, "cand-9999") == 1)
        wj(self.ws / "project.json", {
            "linux_driver": "/x/e1000", "target_os": "/y/asterinas"})
        ok("P10 无 kb_dir → rc 1",
           R.promote_candidate(self.ws, "cand-0003") == 1)
        wj(self.ws / "project.json", {
            "linux_driver": "/x/e1000", "target_os": "/y/asterinas",
            "kb_dir": "corpus"})
        # 重名递增
        rc = R.promote_candidate(self.ws, "cand-0003")
        files = sorted(p.name for p in
                       (self.corpus / "pitfalls").glob("*.md"))
        ok("P11 重名递增", rc == 0 and any(
            "__2" in n for n in files) and "README.md" not in files)
        # reject
        self.C.record_candidate(self.ws, "x", "r2", LONG2 + "尾部")
        ok("D1 reject 出账",
           R.reject_candidate(self.ws, "cand-0004") == 0
           and not C.load_candidates(self.ws))
        ok("D2 reject 不存在 → rc 1",
           R.reject_candidate(self.ws, "cand-0004") == 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
