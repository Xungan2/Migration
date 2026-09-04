"""test_agent_seq_live.py — opt-in 真实 agent 冒烟（默认 skip）。

mock 单测（test_agent_seq.py）只能证明"我们正确传了 --session、正确解析
事件流"，证不了 opencode 真的接上了。本文件用真实小调用验证两件事：

  1. 非交互 run --session 续接可用，且续接后信息确实在（暗号回溯：
     段 1 埋暗号 → 段 2 按会话续接问暗号 → 只有记忆真携带才答得出）
  2. run_agent_seq 端到端小循环：agent 请求 run_static → 外部执行 →
     结果发回 → done JSON 引用外部输出原文

运行：PORTER_LIVE_AGENT_TEST=1 python3 -m unittest tests.test_agent_seq_live
（产生真实模型调用，成本可忽略；需 opencode 已登录）
"""

from __future__ import annotations

import os
import secrets
import tempfile
import unittest
from pathlib import Path

from porter.common import agent


@unittest.skipUnless(os.environ.get("PORTER_LIVE_AGENT_TEST") == "1",
                     "需真实 agent：设 PORTER_LIVE_AGENT_TEST=1 启用")
class TestLiveSessionResume(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.pop("PORTER_NO_AGENT", None)
        self.td = tempfile.mkdtemp(prefix="porter_seq_live_")

    def tearDown(self):
        if self._prev is not None:
            os.environ["PORTER_NO_AGENT"] = self._prev

    def test_session_resume_recall(self):
        """暗号回溯：续接会话后能答出段 1 埋的暗号 = 无信息损失。"""
        token = "XK" + secrets.token_hex(3)
        rc1, out1 = agent._opencode_json_runner(
            f"记住暗号{token}，除此之外只回复 ok", Path(self.td),
            str(Path(self.td) / "L1"), timeout_sec=240)
        ev1 = agent._parse_events(out1)
        self.assertEqual(rc1, 0, out1[-300:])
        self.assertTrue(ev1 and ev1["session_id"],
                        f"session id 未解析：{out1[-300:]!r}")
        rc2, out2 = agent._opencode_json_runner(
            "暗号是什么？只回复暗号本身，不要其他文字", Path(self.td),
            str(Path(self.td) / "L2"), timeout_sec=240,
            session_id=ev1["session_id"])
        ev2 = agent._parse_events(out2)
        self.assertEqual(rc2, 0, out2[-300:])
        self.assertIn(token, (ev2 or {}).get("text", ""),
                      f"续接后未回忆起暗号：{(ev2 or {}).get('text')!r}")

    def test_seq_end_to_end_with_static(self):
        """端到端强验证（指针化后）：run_static 请求 → 回声写进结果文件
        （token 不出现在任何消息里）→ agent 必须自行读文件才能写进
        done.notes —— 验证「指针 → 自读」完整行为链。
        """
        token = "EK" + secrets.token_hex(3)

        def echo():
            return True, f"ECHO OUTPUT: {token}\necho done"

        out = agent.run_agent_seq(
            "你的任务：请求外部执行一次「外部回声验证」；拿到结果后即完成"
            "任务，输出 done JSON，notes 字段原样写出 ECHO OUTPUT: 之后的"
            "完整字符串（EK 开头）。不要自己执行任何等价命令。",
            Path(self.td), str(Path(self.td) / "SEQ"),
            static={"describe": "外部回声验证", "fn": echo},
            gen_schema={"notes": "str"},
            agent_budget_sec=600)
        self.assertEqual(out["status"], "done", json_dumps(out))
        self.assertIn(token, out["parsed"]["notes"],
                      "agent 未能从结果文件读出 token（指针→自读断链）")
        self.assertTrue(out["session_id"], "主路径应解析到 session id")
        self.assertFalse(out["fallback"])
        # 纯净度实证：token 只存在于结果文件，任何段消息里都没有
        sf = Path(f"{Path(self.td) / 'SEQ'}_S1_static.log")
        self.assertTrue(sf.exists() and token in sf.read_text())
        for md in sorted(Path(self.td).glob("SEQ_S*.prompt.md")):
            body = md.read_text()
            self.assertNotIn(token, body,
                             f"token 泄漏进消息：{md.name}")
            if md.name != "SEQ_S1.prompt.md":
                self.assertIn("_static.log", body,
                              f"续接消息应含结果文件指针：{md.name}")


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)[:2000]


if __name__ == "__main__":
    unittest.main()
