"""随机知识探查测试（候选记录/去重闸/建议类/各钩子助手）。

覆盖：
A. record_candidate：落账字段/签名去重/过短跳过/开关关停
B. suggest_class 关口前缀映射
C. record_from_gate（类 1）：rationale/note 提取
D. record_lessons（类 4）
E. load/remove 出账
F. 接线冒烟：close_defect / park_defect / finalize park / 切片翻转
   （直接调挂载函数，验证候选侧产生）

运行：python3 -m unittest tests.test_candidates
"""
import sys, json, tempfile, shutil
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


LONG = "根因是构建缓存参数失效，修复须显式传 console 参数（足够长）"


class TestCandidates(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        from porter.bootstrap import candidates as C
        from porter.bootstrap import kb
        self.C, self.kb = C, kb
        self._old_temp, self._old_cfg = kb.TEMP_DIR, C.CONFIG_PATH
        self.tmp = Path(tempfile.mkdtemp(prefix="kb_cand_"))
        kb.TEMP_DIR = self.tmp / "ttemp"
        self.cfgp = self.tmp / "config.json"
        C.CONFIG_PATH = self.cfgp
        wj(self.cfgp, {"kb": {"candidates": True, "dedup": True,
                              "min_draft_len": 24}})
        self.ws = self.tmp / "ws"
        wj(self.ws / "project.json", {
            "linux_driver": "/x/e1000", "target_os": "/y/asterinas"})

    def tearDown(self):
        self.kb.TEMP_DIR = self._old_temp
        self.C.CONFIG_PATH = self._old_cfg
        shutil.rmtree(self.tmp)

    def ledger(self):
        return rd(self.kb.TEMP_DIR / "candidates" /
                  "e1000@asterinas.json")

    def test_record(self):
        C = self.C
        print("=== A. record_candidate ===")
        cid = C.record_candidate(self.ws, "gate-answer",
                                 "loop.attempts.rx-ring-p4", LONG,
                                 ["P4/rx-ring/logs/"], "pitfalls")
        ok("R1 落账", cid == "cand-0001")
        led = self.ledger()
        ok("R2 字段", led[0]["source"]["hook"] == "gate-answer"
           and led[0]["scope"]["driver"] == "e1000"
           and led[0]["suggested_class"] == "pitfalls"
           and led[0]["status"] == "pending"
           and len(led[0]["signature"]) == 16)
        # 去重（同文不同空白）
        cid2 = C.record_candidate(self.ws, "gate-answer", "other.ref",
                                  "  根因是构建缓存参数失效，"
                                  "修复须显式传 console 参数（足够长）\n")
        ok("R3 签名去重", cid2 is None and len(self.ledger()) == 1)
        # 不同文 → 新条目
        C.record_candidate(self.ws, "l4-park", "c1",
                           "IOAPIC 电平触发缺失，INTx 永不送达（泊车）")
        ok("R4 新条目递增 id", len(self.ledger()) == 2
           and self.ledger()[1]["id"] == "cand-0002")
        # 过短跳过
        ok("R5 过短跳过",
           C.record_candidate(self.ws, "x", "r", "太短") is None)
        # 开关关停
        wj(self.cfgp, {"kb": {"candidates": False}})
        ok("R6 开关关停",
           C.record_candidate(self.ws, "x", "r", LONG) is None)
        wj(self.cfgp, {"kb": {"candidates": True, "dedup": False,
                              "min_draft_len": 24}})
        C.record_candidate(self.ws, "x", "r", LONG)
        ok("R7 dedup=false 放行", len(self.ledger()) == 3)

    def test_suggest_class(self):
        C = self.C
        print("=== B. 建议类映射 ===")
        ok("S1 gap→gaps", C.suggest_class("p3.gap.m1.api_x") == "gaps")
        ok("S2 blocked→gaps",
           C.suggest_class("p4.blocked.m1.f-12") == "gaps")
        ok("S3 t3→runbook",
           C.suggest_class("p0.t3.extract") == "runbook")
        ok("S4 attempts→pitfalls",
           C.suggest_class("loop.attempts.rx-p4") == "pitfalls")
        ok("S5 默认→pitfalls", C.suggest_class("x.y.z") == "pitfalls")

    def test_gate_and_lessons(self):
        C = self.C
        print("=== C/D. gate 收口与 lessons ===")
        gate = {"id": "p3.gap.m1.netdev_stats", "module": "m1"}
        cid = C.record_from_gate(self.ws, gate, {
            "strategy": "bypass", "instruction": "丢弃统计",
            "rationale": "MVP 无消费方且验收判据不引用该统计接口，跨驱动可复用的裁定理由"})
        ok("G1 rationale 优先", cid is not None
           and self.ledger()[0]["suggested_class"] == "gaps"
           and self.ledger()[0]["scope"]["module"] == "m1")
        cid = C.record_from_gate(self.ws, {"id": "loop.attempts.m-p4"},
                                 {"note": "cargo 路径未导出导致编译失败，"
                                          "修复了 shell 环境变量导出"})
        ok("G2 note 兜底", cid is not None)
        ok("G3 空答案跳过",
           C.record_from_gate(self.ws, {"id": "x"}, {}) is None)
        cids = C.record_lessons(self.ws, {
            "entries": [], "lessons": [
                "此目标 OS 的 IRQ 注册在 Bootstrap 阶段之后才可用"
                "（组件阶段禁 spawn）", 42, "短"]}, "P3A/linux_pci_h")
        ok("D1 lessons 只收字符串且过闸", len(cids) == 1)
        ok("D2 无 lessons 不崩",
           C.record_lessons(self.ws, {"entries": []}, "r") == [])

    def test_mounts(self):
        C = self.C
        print("=== F. 挂载点接线 ===")
        # close/park defect
        from porter.loop import p6
        wj(self.ws / "defects.json", {"defects": [
            {"id": "RX-PATH", "title": "收包路径缺陷",
             "status": "in_progress", "history": [],
             "found": "P5 判据失败"}]})
        p6.close_defect(self.ws, "RX-PATH",
                        "configure_rx 未接线导致 RCTL.EN 未置位",
                        "接线 configure_rx 并加回归判据",
                        "L3 rx 计数 >0")
        led = self.ledger()
        ok("F1 defect-close 候选", any(
            c["source"]["hook"] == "defect-close"
            and "configure_rx" in c["draft"] for c in led))
        p6.park_defect(self.ws, "RX-PATH", "平台缺口：IOAPIC 电平触发"
                                        "缺失，泊车待上游")
        ok("F2 defect-park 候选", any(
            c["source"]["hook"] == "defect-park"
            and "泊车" in c["draft"] for c in self.ledger()))
        # finalize park（最小草案 + 直接放行路径：mode agent）
        crit = [{"id": "os-rx.ics", "title": "ICS 软触发",
                 "form": "内核自测", "expr": "ICR=0x14",
                 "rationale": "设备侧已证；交付层泊车（平台缺口）",
                 "disposition": "park"}]
        wj(self.ws / "P6" / "reports" / "l4_criteria.json",
           {"criteria": crit, "status": "draft"})
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = p6.finalize_l4(self.ws, cfg={"review_gates": {
                "l4_criteria_finalization": "agent"}})
        ok("F3 finalize rc=0", rc == 0)
        ok("F4 l4-park 候选", any(
            c["source"]["hook"] == "l4-park"
            and "泊车" in c["draft"] for c in self.ledger()))

    def test_load_remove(self):
        C = self.C
        print("=== E. 出入账 ===")
        C.record_candidate(self.ws, "x", "r", LONG * 2)
        C.record_candidate(self.ws, "x2", "r2",
                           "另一条完全不同的知识记录内容，这里写得足够长以通过闸门")
        led = C.load_candidates(self.ws)
        ok("E1 load", len(led) == 2)
        ok("E2 remove", C.remove_candidate(self.ws, "cand-0001")
           and len(C.load_candidates(self.ws)) == 1)
        ok("E3 remove 不存在 → False",
           not C.remove_candidate(self.ws, "cand-9999"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
