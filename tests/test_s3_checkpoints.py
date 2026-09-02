"""S3 检查点车道测试：checkpoint_run/digest/veto 回滚/CP1/FM。

无 agent / 无网络。覆盖：
A. checkpoint_digest：决策债分组渲染 + veto 指引
B. 批审 veto：applied 债 + answers verdict veto → vetoed + attempts 清零
   + 相位回拨（gap→p4 / deferred→p5）
C. CP1 strategy_checkpoint：human 停车 → approve 结清 → 改文件指纹失效重审
D. CP2 默认关 / 配置开
E. FM 首模块检查点：loop 首模块 done 后停车（默认开）→ approve 后放行
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import gates as G


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_ws() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="porter_s3_t_"))
    ws = tmp / "ws"
    ws.mkdir()
    return ws


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.ws = _mk_ws()

    def test_a_digest(self):
        led = G.GateLedger(self.ws).load()
        g = led.add(id="p3.gap.m1.api_x", kind="decision", phase="P3",
                    module="m1")
        g.update({"answer": {"strategy": "bypass"}, "answered_by": "agent",
                  "status": "applied", "resolution": "gap 处置回写"})
        led.save()
        p = G.checkpoint_digest(self.ws, "CP2", led)
        text = p.read_text(encoding="utf-8")
        ok("A1 债列表", "p3.gap.m1.api_x" in text and "decision" in text)
        ok("A2 veto 指引", "verdict: veto" in text)

    def test_b_veto_rollback(self):
        led = G.GateLedger(self.ws).load()
        g = led.add(id="p3.gap.m1.api_x", kind="decision", phase="P3",
                    module="m1", target="gap", applies_to={"modules": ["m1"]})
        g.update({"answer": {"strategy": "bypass"}, "answered_by": "agent",
                  "status": "applied"})
        led.save()
        (self.ws / "loop_state.json").write_text(json.dumps(
            {"order": ["m1"],
             "modules": {"m1": {"phase": "done",
                                "attempts": {"p3": 0, "p4": 2, "p5": 0}}}}),
            encoding="utf-8")
        (self.ws / "answers.md").write_text(
            "## @p3.gap.m1.api_x\nverdict: veto\nrationale: 该功能不能丢\n",
            encoding="utf-8")
        applied, invalid = G.process_answered_gates(self.ws)
        ok("B1 veto 消费", applied == 0 and invalid == 0)
        led2 = G.GateLedger(self.ws).load()
        g2 = led2.find("p3.gap.m1.api_x")
        ok("B2 状态 vetoed", g2["status"] == "vetoed")
        st = json.loads((self.ws / "loop_state.json").read_text(
            encoding="utf-8"))
        ok("B3 attempts 清零", st["modules"]["m1"]["attempts"]["p4"] == 0)
        ok("B4 相位回拨 gap→p4", st["modules"]["m1"]["phase"] == "p4")

    def test_b2_approve_debt(self):
        led = G.GateLedger(self.ws).load()
        g = led.add(id="p3.gap.m1.api_y", kind="decision", phase="P3",
                    module="m1")
        g.update({"answer": {"strategy": "fill"}, "answered_by": "agent",
                  "status": "applied"})
        led.save()
        (self.ws / "answers.md").write_text(
            "## @p3.gap.m1.api_y\nverdict: approve\n", encoding="utf-8")
        G.process_answered_gates(self.ws)
        g2 = G.GateLedger(self.ws).load().find("p3.gap.m1.api_y")
        ok("B5 approve 结清", g2["status"] == "resolved")

    def test_c_cp1_strategy(self):
        st = self.ws / "P1" / "strategy.md"
        st.parent.mkdir(parents=True)
        st.write_text("# 拆分策略\n- 13 模块\n", encoding="utf-8")
        with mock.patch.object(G, "load_config", return_value={}):
            ok("C1 human 停车 rc 3", G.strategy_checkpoint(self.ws) == 3)
            led = G.GateLedger(self.ws).load()
            ok("C2 关口登记", led.find("cp1.strategy") is not None
               and led.find("cp1.strategy")["status"] == "open")
            (self.ws / "answers.md").write_text(
                "## @cp1.strategy\nverdict: approve\n", encoding="utf-8")
            G.process_answered_gates(self.ws)
            ok("C3 批准后放行 rc 0",
               G.strategy_checkpoint(self.ws) == 0)
            ok("C4 结清", G.GateLedger(self.ws).load()
               .find("cp1.strategy")["status"] == "resolved")
            st.write_text("# 拆分策略（改）\n- 14 模块\n", encoding="utf-8")
            ok("C5 指纹失效 → 重审 rc 3",
               G.strategy_checkpoint(self.ws) == 3)

    def test_d_cp2_switch(self):
        ok("D1 CP2 默认关", G.checkpoint_enabled("CP2") is False)
        with mock.patch.object(G, "load_config",
                               return_value={"checkpoints":
                                             {"CP2_enabled": True}}):
            ok("D2 配置开", G.checkpoint_enabled("CP2") is True)
            rc = G.checkpoint_run(self.ws, "CP2", register=[{
                "id": "cp2.mapping_review", "kind": "approval",
                "question": "映射抽审",
                "answer_form": [{"field": "verdict", "type": "enum",
                                 "options": ["approve", "reject"],
                                 "required": True}]}])
            ok("D3 开启后停车 rc 3", rc == 3)
            ok("D4 digest 落盘",
               (self.ws / "checkpoints" / "CP2_digest.md").exists())

    def test_e_fm_loop(self):
        from porter.loop import run as RUN
        from porter.loop.state import LoopState
        tmp = self.ws.parent
        drv = tmp / "drv"
        drv.mkdir()
        # 最小 loop 工作区（沿用 test_loop_state 的夹具思路）
        ws = tmp / "wsfm"
        ws.mkdir()
        (ws / "project.json").write_text(
            json.dumps({"linux_driver": str(drv), "target_os": str(drv)}),
            encoding="utf-8")
        p1m = ws / "P1" / "modules"
        p1m.mkdir(parents=True)
        (p1m / "deps.json").write_text(
            json.dumps({"order": ["modA"], "edges": {}}), encoding="utf-8")
        with mock.patch.object(RUN.p3, "run_p3", return_value=0), \
             mock.patch.object(RUN.p4, "run_p4", return_value=0), \
             mock.patch.object(RUN.p5, "run_p5", return_value=0):
            rc = RUN.run_loop(ws)
        ok("E1 首模块后 FM 停车 rc 3", rc == 3)
        led = G.GateLedger(ws).load()
        gate = led.find("cp.fm.modA")
        ok("E2 FM 关口登记", gate is not None
           and gate["status"] == "open" and gate["checkpoint"] == "FM")
        (ws / "answers.md").write_text(
            "## @cp.fm.modA\nverdict: approve\n", encoding="utf-8")
        with mock.patch.object(RUN.p3, "run_p3", return_value=0), \
             mock.patch.object(RUN.p4, "run_p4", return_value=0), \
             mock.patch.object(RUN.p5, "run_p5", return_value=0):
            rc = RUN.run_loop(ws)
        ok("E3 approve 后直通 rc 0", rc == 0)
        st = LoopState(ws)
        st.load_or_init()
        ok("E4 全部 done", st.done_set() == {"modA"})
        # FM 一次性：resolved 后（重置状态再跑）不再停
        (ws / "loop_state.json").unlink()
        with mock.patch.object(RUN.p3, "run_p3", return_value=0), \
             mock.patch.object(RUN.p4, "run_p4", return_value=0), \
             mock.patch.object(RUN.p5, "run_p5", return_value=0):
            rc = RUN.run_loop(ws)
        ok("E5 FM 一次性（resolved 不再停）", rc == 0)


if __name__ == "__main__":
    unittest.main()
