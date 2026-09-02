"""S4 路由配置 + agent 先行测试。

无 agent / 无网络（PORTER_NO_AGENT 或 mock）。覆盖：
A. route_for：硬路由锁 / allow 开关 / 特异性覆盖 / 内置 fallback / 两级合并
B. validate_routing 护栏
C. maybe_auto_answer：policy 命中 / agent 高置信 / 低置信回落 / NO_AGENT
D. 债计数收窄（skip/measure/low 计，general 不计）+ 限额
E. p3 register-fill 硬路由（分类转 human）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import gates as G
from porter.loop import routing as R


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_ws() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="porter_s4_t_"))
    ws = tmp / "ws"
    ws.mkdir()
    return ws


class RouteForTest(unittest.TestCase):
    def test_a_routes(self):
        routing = {"gates": {}, "default": ["rules", "agent", "human"]}
        ok("A1 必人硬锁",
           R.route_for("p6.l4.finalize", routing=routing) == ["human"])
        ok("A2 硬锁优先于 default",
           R.route_for("cp1.strategy", routing=routing) == ["human"])
        ok("A3 allow 放开补 agent 兜底",
           R.route_for("p6.l4.finalize",
                       routing={"allow_agent_on_human_gates": True,
                                "gates": {}}) == ["agent", "human"])
        ok("A4 特异性覆盖（长前缀优先）",
           R.route_for("p3.gap.m1.api_x",
                       routing={"gates": {
                           "p3.gap": ["agent", "human"],
                           "p3.gap.m1": ["human"]}}) == ["human"])
        ok("A5 内置 fallback（agent 层已消费）",
           R.route_for("p0.t3.extract", routing=routing) == ["human"])
        ok("A6 无覆盖走 default",
           R.route_for("p5.deferred.m1.c1", routing=routing)
           == ["rules", "agent", "human"])
        # 两级合并：工作区 routing.json 覆盖仓级
        ws = _mk_ws()
        (ws / "routing.json").write_text(json.dumps(
            {"routing": {"gates": {"p5.deferred": ["human"]}}}),
            encoding="utf-8")
        merged = R.load_routing(ws)
        ok("A7 工作区覆写合并",
           merged["gates"].get("p5.deferred") == ["human"])
        ok("A8 覆写生效",
           R.route_for("p5.deferred.m1.c1", ws=ws) == ["human"])

    def test_b_validate(self):
        warns = R.validate_routing({"default": ["rules", "bogus"],
                                    "gates": {"x": [],
                                              "y": ["rules"],
                                              "z": ["nope"]}})
        text = "; ".join(warns)
        ok("B1 未知层", "bogus" in text and "nope" in text)
        ok("B2 空链", "x" in text)
        ok("B3 链缺兜底", "y" in text)

    def test_d_debt(self):
        led = G.GateLedger(_mk_ws()).load()
        for gid, cls, by in [
                ("p3.gap.m1.a", "skip", "agent"),
                ("p3.gap.m1.b", "general", "agent"),
                ("criteria.m1.c", "measure", "agent"),
                ("p3.gap.m1.d", "low", "policy")]:
            g = led.add(id=gid, kind="decision")
            g.update({"answer": {"x": 1}, "answered_by": by,
                      "status": "applied", "debt_class": cls})
        led.save()
        ok("D1 收窄计数（skip+measure+low=3，general 不计）",
           R.debt_count(led) == 3)
        with mock.patch.object(R, "load_routing",
                               return_value={"checkpoints":
                                             {"decision_debt_limit": 3}}), \
             mock.patch.object(R, "load_repo_raw", return_value={}):
            ok("D2 限额读取", R.debt_limit(led.ws) == 3)


class AutoAnswerTest(unittest.TestCase):
    def setUp(self):
        self.ws = _mk_ws()
        self._old = os.environ.get("PORTER_NO_AGENT")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PORTER_NO_AGENT", None)
        else:
            os.environ["PORTER_NO_AGENT"] = self._old

    def _mk_gate(self):
        led = G.GateLedger(self.ws).load()
        g = led.add(id="p3.gap.m1.api_x", kind="decision", phase="P3",
                    module="m1", target="gap", subject="api_x",
                    answer_form=[
                        {"field": "strategy", "type": "enum",
                         "options": ["bypass", "fill"], "required": True},
                        {"field": "instruction", "type": "text",
                         "required": True},
                        {"field": "rationale", "type": "text",
                         "required": True}])
        # gap 正本
        p3r = self.ws / "P3" / "m1" / "reports"
        p3r.mkdir(parents=True, exist_ok=True)
        (p3r / "gap_decisions.json").write_text(json.dumps(
            {"decisions": [{"linux_api": "api_x", "strategy": "human"}]}),
            encoding="utf-8")
        return led, g

    def test_c_policy_hit(self):
        led, g = self._mk_gate()
        hit = {"hit": True, "rule_id": "ethtool-stats-bypass",
               "answer": {"strategy": "bypass", "instruction": "丢统计",
                          "rationale": "规则命中"}, "confidence": "high"}
        with mock.patch.object(R, "consult_policy", return_value=hit):
            got = R.maybe_auto_answer(self.ws, led, g)
        ok("C1 policy 命中应用", got is True and g["status"] == "applied")
        ok("C2 answered_by=policy", g["answered_by"] == "policy")
        ok("C3 debt_class=skip", g["debt_class"] == "skip")
        R._record_hit(self.ws, "ethtool-stats-bypass", g["id"])
        ok("C4 遥测落盘", "ethtool-stats-bypass" in json.loads(
            (self.ws / "policy_hits.json").read_text(
                encoding="utf-8"))["hits"])
        dec = json.loads((self.ws / "P3" / "m1" / "reports" /
                          "gap_decisions.json").read_text(encoding="utf-8"))
        ok("C5 正本已改", dec["decisions"][0]["strategy"] == "bypass")

    def test_c2_agent_high(self):
        led, g = self._mk_gate()
        got_ans = {"answer": {"strategy": "fill", "instruction": "补实现",
                              "rationale": "树内有等价物"},
                   "confidence": "high"}
        with mock.patch.object(R, "consult_policy", return_value=None), \
             mock.patch.object(R, "agent_answer", return_value=got_ans):
            got = R.maybe_auto_answer(self.ws, led, g)
        ok("C6 agent 高置信应用", got is True
           and g["answered_by"] == "agent")

    def test_c3_agent_low_falls_back(self):
        led, g = self._mk_gate()
        with mock.patch.object(R, "consult_policy", return_value=None), \
             mock.patch.object(R, "agent_answer",
                               return_value={"answer": {"strategy": "bypass",
                                                        "instruction": "x",
                                                        "rationale": "y"},
                                             "confidence": "low"}):
            got = R.maybe_auto_answer(self.ws, led, g)
        ok("C7 低置信回落人工", got is False and g["status"] == "open")

    def test_c4_no_agent(self):
        os.environ["PORTER_NO_AGENT"] = "1"
        led, g = self._mk_gate()
        got = R.maybe_auto_answer(self.ws, led, g)   # 真实路径：两层自查环境
        ok("C8 NO_AGENT 两层全跳过", got is False and g["status"] == "open")

    def test_c5_retry_kind_never_auto(self):
        led = G.GateLedger(self.ws).load()
        g = led.add(id="loop.attempts.m1-p4", kind="retry", module="m1")
        with mock.patch.object(R, "consult_policy") as mp:
            got = R.maybe_auto_answer(self.ws, led, g)
        ok("C9 retry 类不自动应答", got is False and not mp.called)


if __name__ == "__main__":
    unittest.main()
