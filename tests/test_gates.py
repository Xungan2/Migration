"""porter/loop/gates.py 单元测试（人工介入框架 S1：关口账本）。

无 agent / 无网络。覆盖：
A. 账本 CRUD：登记/查找/幂等 re-ask/history 追加/原子写
B. 答案解析：## @id 节 + 字段行 + 多行值；legacy 节（## retry X）不碰
C. 表单校验：enum 非法/必填缺失/可选空放行
D. process_answered_gates：happy（answered→applied+resolution）/
   invalid（错误进 history、@ 节保留）/ 未知 id 不消费
E. applier：retry 清 attempts / gap 回写 gap_decisions+mapping /
   deferred 双写同步 / approval 指纹不符拒绝
F. 渲染：open 关口出表单、invalid 标红
G. panic：登记+返回 3+幂等 re-ask、summary_line
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import gates as G


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


class GatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_gates_t_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()

    def _ledger(self) -> G.GateLedger:
        return G.GateLedger(self.ws).load()

    # ---------- A ----------

    def test_a_crud(self):
        led = self._ledger()
        g = led.add(id="p3.gap.m1.api_x", kind="decision", phase="P3",
                    module="m1", target="gap", subject="api_x",
                    question="api_x 无对应物，如何处置？",
                    answer_form=[
                        {"field": "strategy", "type": "enum",
                         "options": ["bypass", "fill"], "required": True},
                        {"field": "rationale", "type": "text",
                         "required": True}])
        ok("A1 登记", led.find("p3.gap.m1.api_x") is g)
        ok("A2 初始 open", g["status"] == "open")
        ok("A3 落盘", (self.ws / "gates.json").exists())
        g2 = led.add(id="p3.gap.m1.api_x", question="再问一次")
        ok("A4 幂等 re-ask 同条目", g2 is g)
        ok("A5 history 追加", g["history"][-1]["event"] == "re-asked")
        led2 = self._ledger()
        ok("A6 重载保持", len(led2.gates) == 1
           and led2.find("p3.gap.m1.api_x")["status"] == "open")

    # ---------- B ----------

    def test_b_parse(self):
        (self.ws / "answers.md").write_text(
            "# 人工答案\n\n"
            "## @g1\n"
            "strategy: bypass\n"
            "rationale: 统计类不需要，\n"
            "  MVP 范围外\n"
            "\n"
            "## retry m1-p5\n"
            "已修复，重跑\n",
            encoding="utf-8")
        got = G.parse_gate_answers(self.ws)
        ok("B1 @节解析", set(got) == {"g1"})
        ok("B2 字段行", got["g1"]["strategy"] == "bypass")
        ok("B3 多行值拼接", "MVP 范围外" in got["g1"]["rationale"])
        ok("B4 legacy 节不在视野", "retry m1-p5" not in got)

    # ---------- C ----------

    def test_c_validate(self):
        form = [
            {"field": "strategy", "type": "enum",
             "options": ["bypass", "fill"], "required": True},
            {"field": "note", "type": "text", "required": False}]
        ok("C1 enum 合法", G.validate_answer(form, {"strategy": "bypass"}) == [])
        ok("C2 enum 非法", G.validate_answer(form, {"strategy": "nope"}) != [])
        ok("C3 必填缺失", G.validate_answer(form, {}) != [])
        ok("C4 可选空放行",
           G.validate_answer(form, {"strategy": "fill"}) == [])

    # ---------- D ----------

    def _register_gap_gate(self):
        led = self._ledger()
        led.add(id="p3.gap.m1.api_x", kind="decision", phase="P3",
                module="m1", target="gap", subject="api_x",
                question="?", answer_form=[
                    {"field": "strategy", "type": "enum",
                     "options": ["bypass", "fill"], "required": True},
                    {"field": "instruction", "type": "text",
                     "required": True},
                    {"field": "rationale", "type": "text", "required": True}])
        return led

    def test_d_process_happy(self):
        led = self._register_gap_gate()
        (self.ws / "answers.md").write_text(
            "## @p3.gap.m1.api_x\n"
            "strategy: bypass\n"
            "instruction: 丢弃统计\n"
            "rationale: MVP 不需要\n",
            encoding="utf-8")
        # gap 正本（applier 目标）
        p3r = self.ws / "P3" / "m1" / "reports"
        p3r.mkdir(parents=True)
        (p3r / "gap_decisions.json").write_text(json.dumps(
            {"decisions": [{"linux_api": "api_x", "strategy": "human"}]}),
            encoding="utf-8")
        applied, invalid = G.process_answered_gates(self.ws, led)
        ok("D1 应用成功", applied == 1 and invalid == 0)
        g = led.find("p3.gap.m1.api_x")
        ok("D2 状态 applied", g["status"] == "applied")
        ok("D3 answered_by", g["answered_by"] == "human")
        ok("D4 resolution 回填", "gap 处置回写" in (g["resolution"] or ""))
        dec = json.loads((p3r / "gap_decisions.json").read_text(
            encoding="utf-8"))
        ok("D5 正本已改", dec["decisions"][0]["strategy"] == "bypass")
        left = (self.ws / "answers.md").read_text(encoding="utf-8")
        ok("D6 @节已移除", "@p3.gap" not in left)

    def test_d_process_invalid(self):
        self._register_gap_gate()
        (self.ws / "answers.md").write_text(
            "## @p3.gap.m1.api_x\nstrategy: 不存在的选项\n",
            encoding="utf-8")
        led = self._ledger()
        applied, invalid = G.process_answered_gates(self.ws, led)
        ok("D7 invalid 计数", invalid == 1 and applied == 0)
        g = led.find("p3.gap.m1.api_x")
        ok("D8 状态 invalid", g["status"] == "invalid")
        ok("D9 错误进 history",
           any(h["event"] == "invalid-answer" for h in g["history"]))
        left = (self.ws / "answers.md").read_text(encoding="utf-8")
        ok("D10 @节保留待改", "@p3.gap" in left)

    def test_d_process_unknown(self):
        (self.ws / "answers.md").write_text(
            "## @nobody-knows\nfoo: bar\n", encoding="utf-8")
        led = self._ledger()
        applied, invalid = G.process_answered_gates(self.ws, led)
        ok("D11 未知 id 不消费不炸", applied == 0 and invalid == 0)
        ok("D12 节保留",
           "@nobody-knows" in (self.ws / "answers.md").read_text(
               encoding="utf-8"))

    # ---------- E ----------

    def test_e_retry_applier(self):
        led = self._ledger()
        led.add(id="loop.attempts.m1-p4", kind="retry", module="m1",
                step="p4", question="attempts 烧穿")
        (self.ws / "loop_state.json").write_text(json.dumps(
            {"order": ["m1"],
             "modules": {"m1": {"phase": "p4",
                                "attempts": {"p3": 0, "p4": 3, "p5": 0}}}}),
            encoding="utf-8")
        led.find("loop.attempts.m1-p4").update(
            {"answer": {"note": "工具链修好了"},
             "status": "answered", "answered_by": "human"})
        res = G._apply(self.ws, led.find("loop.attempts.m1-p4"),
                       {"note": "工具链修好了"})
        st = json.loads((self.ws / "loop_state.json").read_text(
            encoding="utf-8"))
        ok("E1 指定 step 清零", st["modules"]["m1"]["attempts"]["p4"] == 0)
        ok("E2 其他 step 不动", st["modules"]["m1"]["attempts"]["p3"] == 0)
        ok("E3 笔记进 resolution", "工具链修好了" in res)

    def test_e_deferred_applier(self):
        led = self._ledger()
        led.add(id="p5.deferred.m1.crit1", kind="decision", module="m1",
                target="deferred", subject="crit1", question="判据修什么？",
                answer_form=[
                    {"field": "verdict", "type": "enum",
                     "options": ["fix-criterion", "fix-code"],
                     "required": True},
                    {"field": "new_expr", "type": "text", "required": False}])
        (self.ws / "deferred.json").write_text(json.dumps(
            {"entries": [{"id": "crit1", "module": "m1",
                          "criterion": {"id": "crit1", "expr": "旧正则",
                                        "kind": "log_pattern"},
                          "status": "open", "history": []}]}),
            encoding="utf-8")
        p3r = self.ws / "P3" / "m1" / "reports"
        p3r.mkdir(parents=True)
        (p3r / "criteria.json").write_text(json.dumps(
            {"criteria": [{"id": "crit1", "layer": "L3",
                           "kind": "log_pattern", "expr": "旧正则",
                           "deferred_by": ["m2"]}]}),
            encoding="utf-8")
        ans = {"verdict": "fix-criterion", "new_expr": "新正则 [0-9]+"}
        res = G._apply(self.ws, led.find("p5.deferred.m1.crit1"), ans)
        d = json.loads((self.ws / "deferred.json").read_text(encoding="utf-8"))
        ok("E4 副本已改", d["entries"][0]["criterion"]["expr"] == "新正则 [0-9]+")
        c = json.loads((p3r / "criteria.json").read_text(encoding="utf-8"))
        ok("E5 正本同步", c["criteria"][0]["expr"] == "新正则 [0-9]+")
        ok("E6 resolution 说明双写", "criteria.json" in res)

    def test_e_approval_sha(self):
        art = self.ws / "draft.json"
        art.write_text("{\"a\": 1}", encoding="utf-8")
        import hashlib
        sha = hashlib.sha256(art.read_bytes()).hexdigest()[:16]
        led = self._ledger()
        led.add(id="p6.l4.finalize", kind="approval",
                artifact_path="draft.json", artifact_sha=sha, question="?")
        res_ok = G._apply(self.ws, led.find("p6.l4.finalize"), {})
        ok("E7 指纹相符通过", "批准已记录" in res_ok)
        art.write_text("{\"a\": 2}", encoding="utf-8")   # 工件变更
        res_bad = G._apply(self.ws, led.find("p6.l4.finalize"), {})
        ok("E8 指纹不符拒绝", "工件指纹不符" in res_bad)

    # ---------- F ----------

    def test_f_render(self):
        led = self._register_gap_gate()
        led.add(id="p0.memo.x", kind="memo", blocking=False,
                question="备忘一条")
        p = G.render_human_questions(self.ws, led)
        text = p.read_text(encoding="utf-8")
        ok("F1 渲染含关口", "@p3.gap.m1.api_x" in text)
        ok("F2 表单呈现", "strategy" in text and "bypass" in text)
        ok("F3 备忘分区", "非阻塞备忘" in text)

    # ---------- G ----------

    def test_g_panic(self):
        rc = G.panic(self.ws, {"id": "loop.attempts.m2-p5", "kind": "retry",
                               "module": "m2", "step": "p5",
                               "question": "烧穿了"})
        ok("G1 返回 3", rc == 3)
        led = self._ledger()
        ok("G2 已登记", led.find("loop.attempts.m2-p5") is not None)
        rc2 = G.panic(self.ws, {"id": "loop.attempts.m2-p5",
                                "question": "又烧穿了"})
        led2 = self._ledger()
        ok("G3 幂等不重复", len(led2.gates) == 1)
        ok("G4 history 增长", len(led2.gates[0]["history"])
           > len(led.gates[0]["history"]))
        ok("G5 渲染已产出",
           (self.ws / "human_questions.md").exists())
        ok("G6 summary", "open 阻塞关口 1" in G.summary_line(self.ws))


if __name__ == "__main__":
    unittest.main()
