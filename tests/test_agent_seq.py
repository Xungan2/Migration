"""test_agent_seq.py — run_agent_seq / run_agent_structured 单测（全 mock）。

覆盖（零真实 agent，仿 test_errorloop 的 mock.patch.object 惯例）：
  L1  _parse_events：session id/文本提取、字段变体、噪音行、垃圾输入
  L2  _validate_schema：缺字段/类型错/bool≠int
  L3  _static_sig：路径/时间戳/ANSI 变化不翻转；真变化翻转
  L4  _seq_preamble：禁令与 describe
  L5  run_agent_seq 主路径（session 续接）：run_static → 静态段 → done
  L6  run_agent_seq 兜底路径：无 session id → 交互式模仿 transcript 注入
  L7  预算耗尽 / no-agent / 静态段异常
  L8  防打转：同签名失败连发 2 次 → stalled；成功重置
  L9  final_static 终验：通过即 done；失败回环后再 done
  L10 schema 反馈重试 / 不可解析输出反馈重试
  L11 run_agent_structured：首发通过 / 带反馈重试通过
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from porter.common import agent

TMP = tempfile.mkdtemp(prefix="porter_seq_test_")


def _ev(session="ses_T1", text="ok") -> str:
    """构造 opencode --format json 风格的 JSONL 事件流。"""
    lines = []
    if session:
        lines.append({"type": "step_start", "sessionID": session,
                      "part": {"type": "step-start"}})
    lines.append({"type": "text", "sessionID": session,
                  "part": {"type": "text", "text": text}})
    lines.append({"type": "step_finish", "sessionID": session,
                  "part": {"type": "step-finish", "reason": "stop"}})
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _phase_text(obj: dict) -> str:
    """带 ```json phase 块的助手消息文本。"""
    return "本轮说明。\n```json\n" + json.dumps(obj, ensure_ascii=False) \
        + "\n```"


class Runner:
    """_opencode_json_runner 的 mock：按序回放响应并记录调用。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, message, workdir, log_stem, timeout_sec=0,
                 session_id=None, model=None, task=None):
        self.calls.append({"message": message, "stem": str(log_stem),
                           "session_id": session_id,
                           "timeout": timeout_sec})
        self.calls[-1]["timeout"] = timeout_sec
        rc, out = self.responses.pop(0)
        return rc, out

    @property
    def messages(self):
        return [c["message"] for c in self.calls]


def _static_fn(ok=True, output="BUILD OK\nall green"):
    calls = []

    def fn():
        calls.append(1)
        return ok, output
    return fn, calls


class TestParseEvents(unittest.TestCase):
    def test_basic(self):
        ev = agent._parse_events(_ev("ses_A", "hello"))
        self.assertEqual(ev["session_id"], "ses_A")
        self.assertEqual(ev["text"], "hello")

    def test_multi_text_concat(self):
        out = (_ev("ses_B", "part1")
               + json.dumps({"type": "text", "sessionID": "ses_B",
                             "part": {"type": "text", "text": "part2"}})
               + "\n")
        ev = agent._parse_events(out)
        self.assertEqual(ev["text"], "part1\npart2")

    def test_field_variant_and_noise(self):
        out = ("stderr 噪音行\n"
               + json.dumps({"type": "text", "session_id": "ses_C",
                             "part": {"type": "text", "text": "v"}}) + "\n"
               + "not json\n")
        ev = agent._parse_events(out)
        self.assertEqual(ev["session_id"], "ses_C")
        self.assertEqual(ev["text"], "v")

    def test_no_session_but_text(self):
        ev = agent._parse_events(
            json.dumps({"type": "text",
                        "part": {"type": "text", "text": "t"}}) + "\n")
        self.assertIsNone(ev["session_id"])
        self.assertEqual(ev["text"], "t")

    def test_garbage(self):
        self.assertIsNone(agent._parse_events("TIMEOUT"))
        self.assertIsNone(agent._parse_events(""))
        self.assertIsNone(agent._parse_events("random text\n{\"broken\": "))


class TestValidateSchema(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(
            agent._validate_schema({"a": "x", "b": 1, "c": [], "d": {}},
                                   {"a": "str", "b": "int", "c": "list",
                                    "d": "dict"}), [])

    def test_missing(self):
        errs = agent._validate_schema({"a": "x"}, {"a": "str", "b": "int"})
        self.assertEqual(len(errs), 1)
        self.assertIn("缺必填字段 b", errs[0])

    def test_type_mismatch(self):
        errs = agent._validate_schema({"a": 5}, {"a": "str"})
        self.assertIn("期望 str", errs[0])

    def test_bool_not_int(self):
        errs = agent._validate_schema({"a": True}, {"a": "int"})
        self.assertEqual(len(errs), 1)

    def test_not_dict(self):
        self.assertEqual(agent._validate_schema([1, 2], {"a": "str"}),
                         ["输出不是 JSON 对象"])


class TestStaticSig(unittest.TestCase):
    A = ("error[E0308]: mismatch at /a/b/c.rs:12:5\n"
         "build FAILED 2026-09-04T09:00:00")
    # 同 basename（c.rs），仅目录/行号/时间戳不同 → 规范化后同签名
    A2 = ("error[E0308]: mismatch at /x/y/c.rs:99:7\n"
          "build FAILED 2026-09-04T10:00:00")
    B = "error[E0002]: totally different"

    def test_stable_under_cosmetics(self):
        self.assertEqual(agent._static_sig(self.A),
                         agent._static_sig(self.A2))

    def test_flips_on_real_change(self):
        self.assertNotEqual(agent._static_sig(self.A),
                            agent._static_sig(self.B))

    def test_empty(self):
        self.assertEqual(agent._static_sig(""), "")

    def test_ansi_ignored(self):
        a = "\x1b[31m" + self.A + "\x1b[0m"
        self.assertEqual(agent._static_sig(a), agent._static_sig(self.A))


class TestPreamble(unittest.TestCase):
    def test_forbids_static(self):
        p = agent._seq_preamble({"describe": "编译验证"}, None)
        self.assertIn("编译验证", p)
        self.assertIn("禁止你自己执行", p)
        self.assertIn("run_static", p)
        self.assertIn("文件路径", p)          # 指针化：预告结果以文件提供

    def test_lists_done_fields(self):
        p = agent._seq_preamble(None, {"files": "list"})
        self.assertIn('"files"', p)
        self.assertIn("done", p)


class TestParsePhase(unittest.TestCase):
    def test_new_protocol(self):
        t = _phase_text({"phase": "run_static", "message": "验证"})
        obj = agent._parse_phase(t)
        self.assertEqual(obj["phase"], "run_static")

    def test_status_style_done(self):
        t = _phase_text({"status": "done", "files": ["a.rs"], "notes": "x"})
        obj = agent._parse_phase(t)
        self.assertEqual(obj["phase"], "done")     # 等价完成
        self.assertEqual(obj["files"], ["a.rs"])

    def test_status_style_blocked(self):
        t = _phase_text({"status": "blocked", "notes": "映射不可用"})
        obj = agent._parse_phase(t)
        self.assertEqual(obj["phase"], "done")     # 携带 blocked 的完成
        self.assertEqual(obj["status"], "blocked")

    def test_no_valid_shape(self):
        self.assertIsNone(agent._parse_phase(_phase_text({"status": "wip"})))
        self.assertIsNone(agent._parse_phase("无 JSON 块"))


class TestRunAgentSeq(unittest.TestCase):
    def _ws(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        return d, str(d / "SEQ")

    def test_happy_session_path(self):
        ws, stem = self._ws()
        fn, calls = _static_fn(True, "line1\nline2\nBUILD OK")
        r = Runner([
            (0, _ev("ses_H1", _phase_text({"phase": "run_static",
                                           "message": "验证编译"}))),
            (0, _ev("ses_H1", _phase_text({"phase": "done",
                                           "files": ["a.rs"],
                                           "summary": "迁移完成"}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq(
                "TASK BODY", ws, stem,
                static={"describe": "编译验证", "fn": fn},
                gen_schema={"files": "list", "summary": "str"},
                agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["session_id"], "ses_H1")
        self.assertFalse(out["fallback"])
        self.assertEqual(out["parsed"]["files"], ["a.rs"])
        self.assertEqual(len(calls), 1)          # 静态段跑了一次
        self.assertEqual(len(r.calls), 2)
        self.assertIsNone(r.calls[0]["session_id"])
        self.assertEqual(r.calls[1]["session_id"], "ses_H1")
        self.assertIn("TASK BODY", r.messages[0])
        self.assertIn("禁止你自己执行", r.messages[0])
        # 第二段消息 = 指针块（verdict + 文件路径，零内容注入）
        msg2 = r.messages[1]
        self.assertIn("外部执行结果", msg2)
        self.assertIn("编译验证", msg2)
        self.assertIn("成功", msg2)
        static_file = Path(f"{stem}_S1_static.log")
        self.assertIn(str(static_file.resolve()), msg2)
        self.assertNotIn("TASK BODY", msg2)
        self.assertNotIn("BUILD OK", msg2)       # 输出内容不进消息
        self.assertTrue(static_file.exists())    # 完整输出在文件里
        self.assertIn("line1", static_file.read_text())
        self.assertIn("BUILD OK", static_file.read_text())
        self.assertEqual(out["rounds"][0]["static"]["log"],
                         str(static_file))
        journal = json.loads(Path(f"{stem}.seq.json").read_text())
        self.assertEqual(journal["status"], "done")

    def test_fallback_transcript(self):
        ws, stem = self._ws()
        fn, calls = _static_fn(False, "compile error boom")
        r = Runner([
            # 无 sessionID 字段 → 解析不到会话 → 兜底
            (0, _ev(None, _phase_text({"phase": "run_static",
                                       "message": "验证"}))),
            (0, _ev(None, _phase_text({"phase": "done"}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq(
                "TASK BODY", ws, stem,
                static={"describe": "编译验证", "fn": fn},
                agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertTrue(out["fallback"])
        self.assertIsNone(out["session_id"])
        msg2 = r.messages[1]
        self.assertIn("TASK BODY", msg2)             # 任务重发
        self.assertIn("此前对话", msg2)               # transcript 注入
        self.assertIn("[用户]", msg2)
        self.assertIn("[助手]", msg2)
        self.assertIn("外部执行结果", msg2)            # 静态结果指针
        self.assertIn("_S1_static.log", msg2)
        self.assertIn("失败", msg2)
        self.assertNotIn("compile error boom", msg2)  # 内容不进消息
        sf = Path(f"{stem}_S1_static.log")
        self.assertIn("compile error boom",
                      sf.read_text())                 # 内容在文件里

    def test_budget_exhausted_no_calls(self):
        ws, stem = self._ws()
        r = Runner([])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq("T", ws, stem, agent_budget_sec=0)
        self.assertEqual(out["status"], "budget-exhausted")
        self.assertEqual(r.calls, [])

    def test_no_agent_env(self):
        ws, stem = self._ws()
        r = Runner([])
        with mock.patch.dict(os.environ, {"PORTER_NO_AGENT": "1"}):
            out = agent.run_agent_seq("T", ws, stem)
        self.assertEqual(out["status"], "no-agent")
        self.assertEqual(r.calls, [])

    def test_opencode_missing_fail_fast(self):
        ws, stem = self._ws()
        r = Runner([(127, "opencode executable not found")])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq("T", ws, stem)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(len(r.calls), 1)     # 不重试

    def test_stall_on_same_sig(self):
        ws, stem = self._ws()
        A = ("error[E0308]: mismatch at /a/b/c.rs:12:5\n"
             "build FAILED 2026-09-04T09:00:00")
        A2 = ("error[E0308]: mismatch at /x/y/c.rs:99:7\n"
              "build FAILED 2026-09-04T10:00:00")    # 同签名（碎改动）
        outputs = iter([A, A2, "should not run"])
        fn = lambda: (False, next(outputs))          # noqa: E731
        r = Runner([
            (0, _ev("ses_S", _phase_text({"phase": "run_static",
                                          "message": "1"}))),
            (0, _ev("ses_S", _phase_text({"phase": "run_static",
                                          "message": "2"}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq(
                "T", ws, stem, static={"describe": "编译", "fn": fn},
                agent_budget_sec=600)
        self.assertEqual(out["status"], "stalled")
        self.assertEqual(len(r.calls), 2)     # 第二次同签名即早退
        sigs = [rd["static"]["sig"] for rd in out["rounds"]]
        self.assertEqual(sigs[0], sigs[1])
        self.assertTrue(all(sigs))

    def test_static_ok_resets_sig_then_done(self):
        ws, stem = self._ws()
        results = iter([(False, "error A"), (True, "BUILD OK")])
        fn = lambda: next(results)                   # noqa: E731
        r = Runner([
            (0, _ev("ses_R", _phase_text({"phase": "run_static",
                                          "message": "1"}))),
            (0, _ev("ses_R", _phase_text({"phase": "done"}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq(
                "T", ws, stem, static={"describe": "编译", "fn": fn},
                agent_budget_sec=600)
        self.assertEqual(out["status"], "done")

    def test_static_exception_is_failure(self):
        ws, stem = self._ws()

        def boom():
            raise RuntimeError("kaboom")
        r = Runner([
            (0, _ev("ses_E", _phase_text({"phase": "run_static",
                                          "message": "1"}))),
            (0, _ev("ses_E", _phase_text({"phase": "done"}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq(
                "T", ws, stem, static={"describe": "编译", "fn": boom},
                agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertFalse(out["rounds"][0]["static"]["ok"])
        self.assertIn("静态段异常",
                      Path(f"{stem}_S1_static.log").read_text())
        self.assertNotIn("静态段异常", r.messages[1])   # 异常文本走文件
        self.assertIn("_S1_static.log", r.messages[1])

    def test_static_write_fail_fallback_tail(self):
        ws, stem = self._ws()
        fn, _ = _static_fn(True, "FALLBACK CONTENT LINE")
        r = Runner([
            (0, _ev("ses_WF", _phase_text({"phase": "run_static",
                                           "message": "1"}))),
            (0, _ev("ses_WF", _phase_text({"phase": "done"}))),
        ])
        real_write = Path.write_text

        def _boom(self, data, **kw):
            if str(self).endswith("_static.log"):
                raise OSError("disk full")
            return real_write(self, data, **kw)
        with mock.patch.object(agent, "_opencode_json_runner", r), \
                mock.patch.object(Path, "write_text", _boom):
            out = agent.run_agent_seq(
                "T", ws, stem, static={"describe": "编译", "fn": fn},
                agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertIsNone(out["rounds"][0]["static"]["log"])
        # 写盘失败 → 降级回尾行注入（agent 不拿死指针）
        self.assertIn("FALLBACK CONTENT LINE", r.messages[1])
        self.assertIn("输出尾", r.messages[1])

    def test_final_static_pass(self):
        ws, stem = self._ws()
        fn, calls = _static_fn(True, "BUILD OK")
        r = Runner([
            (0, _ev("ses_F", _phase_text({"phase": "done",
                                          "files": ["x.rs"]}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq(
                "T", ws, stem, static={"describe": "编译", "fn": fn},
                gen_schema={"files": "list"}, final_static=True,
                agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertEqual(len(calls), 1)
        self.assertTrue(out["rounds"][0]["static"]["ok"])

    def test_final_static_fail_then_done(self):
        ws, stem = self._ws()
        results = iter([(False, "err one"), (True, "BUILD OK")])
        fn = lambda: next(results)                   # noqa: E731
        r = Runner([
            (0, _ev("ses_G", _phase_text({"phase": "done",
                                          "files": ["x.rs"]}))),
            (0, _ev("ses_G", _phase_text({"phase": "done",
                                          "files": ["x.rs", "y.rs"]}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq(
                "T", ws, stem, static={"describe": "编译", "fn": fn},
                gen_schema={"files": "list"}, final_static=True,
                agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertEqual(len(r.calls), 2)
        self.assertIn("外部执行结果", r.messages[1])   # 失败结果回环注入
        self.assertIn("失败", r.messages[1])
        self.assertEqual(out["parsed"]["files"], ["x.rs", "y.rs"])

    def test_schema_feedback_retry(self):
        ws, stem = self._ws()
        r = Runner([
            (0, _ev("ses_V", _phase_text({"phase": "done"}))),   # 缺 files
            (0, _ev("ses_V", _phase_text({"phase": "done",
                                          "files": ["a.rs"]}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq(
                "T", ws, stem, gen_schema={"files": "list"},
                agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["parsed"]["files"], ["a.rs"])
        self.assertIn("缺必填字段 files", r.messages[1])

    def test_unparseable_feedback_retry(self):
        ws, stem = self._ws()
        r = Runner([
            (0, _ev("ses_U", "我没有输出 JSON")),               # 无 phase
            (0, _ev("ses_U", _phase_text({"phase": "done"}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq("T", ws, stem, agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertIn("未见合法 phase JSON", r.messages[1])

    def test_run_static_without_static_config(self):
        ws, stem = self._ws()
        r = Runner([
            (0, _ev("ses_W", _phase_text({"phase": "run_static",
                                          "message": "1"}))),
            (0, _ev("ses_W", _phase_text({"phase": "done"}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq("T", ws, stem, agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertIn("未配置外部静态操作", r.messages[1])

    def test_timeout_rc_nonzero_counts_budget(self):
        ws, stem = self._ws()
        r = Runner([(-1, "TIMEOUT"), (-1, "TIMEOUT")])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq("T", ws, stem, agent_budget_sec=0)
        # 预算 0：一段都不跑
        self.assertEqual(out["status"], "budget-exhausted")


class TestTimeoutSalvage(unittest.TestCase):
    """L12（2026-09-05）：超时/非零 rc 先抢救部分事件（续接地基）。"""

    def _ws(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        return d, str(d / "SEQ")

    def test_runner_timeout_salvages_partial_events(self):
        ws = Path(tempfile.mkdtemp(dir=TMP))
        partial = _ev("ses_TO", "写了一半")          # 超时前已产出的事件
        exc = subprocess.TimeoutExpired(
            cmd=["opencode"], timeout=5,
            output=partial.encode("utf-8"), stderr=b"")
        with mock.patch.object(agent.subprocess, "run", side_effect=exc):
            rc, out = agent._opencode_json_runner(
                "M", ws, str(ws / "TO"), timeout_sec=5)
        self.assertEqual(rc, -1)
        self.assertTrue(out.startswith("TIMEOUT"))    # 标记保留在首位
        ev = agent._parse_events(out)
        self.assertEqual((ev or {}).get("session_id"), "ses_TO")
        self.assertIn("写了一半", (ev or {}).get("text", ""))
        self.assertIn("ses_TO", Path(str(ws / "TO") + ".log")
                      .read_text(encoding="utf-8"))   # 部分事件完整落盘

    def test_seq_nonzero_rc_salvages_session(self):
        ws, stem = self._ws()
        fn, calls = _static_fn(True, "BUILD OK")
        r = Runner([
            (-1, "TIMEOUT\n" + _ev("ses_TO2", _phase_text(
                {"phase": "run_static", "message": "编译"}))),
            (0, _ev("ses_TO2", _phase_text({"phase": "done",
                                            "files": ["a.rs"]}))),
        ])
        with mock.patch.object(agent, "_opencode_json_runner", r):
            out = agent.run_agent_seq(
                "T", ws, stem, static={"describe": "编译", "fn": fn},
                gen_schema={"files": "list"}, agent_budget_sec=600)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["session_id"], "ses_TO2")   # rc=-1 也会话存活
        self.assertEqual(r.calls[1]["session_id"], "ses_TO2")
        self.assertEqual(len(calls), 1)


class TestStdinChannel(unittest.TestCase):
    """L13（2026-09-05）：消息经 stdin 传递，argv 不含消息元素。

    历史：argv 路径两坑——① - 开头消息（反馈块惯用 --- 分隔线）被
    yargs 当选项 → rc=1/0s（rerun2 校准 r2 全灭，先以 `--` 分隔符
    修复）；② 含空格消息被包字面引号（run.ts resolveRunInput）。
    stdin 通道根治两者；live 验证 1.18.28：事件流 / --session 续接 /
    verbatim 三检全过（stdin 属未文档化行为，opencode 升级需复验）。
    """

    def _capture(self):
        captured = {}

        def _fake_run(args, **kw):
            captured["args"] = list(args)
            captured["kw"] = kw
            return mock.Mock(returncode=0, stdout="", stderr="")

        return captured, mock.patch.object(agent.subprocess, "run",
                                           side_effect=_fake_run)

    def test_json_runner_message_via_stdin(self):
        ws = Path(tempfile.mkdtemp(dir=TMP))
        captured, patcher = self._capture()
        msg = "---\n## 上一轮验证 FAIL"
        with patcher:
            agent._opencode_json_runner(
                msg, ws, str(ws / "SEP"), timeout_sec=5, session_id="ses_X")
        args = captured["args"]
        self.assertNotIn(msg, args)                    # 消息不进 argv
        self.assertNotIn("--", args)                   # 分隔符一并退役
        self.assertEqual(captured["kw"].get("input"), msg)  # 走 stdin
        self.assertIn("--session", args)               # 续接参数不受影响

    def test_run_agent_message_via_stdin(self):
        ws = Path(tempfile.mkdtemp(dir=TMP))
        captured, patcher = self._capture()
        msg = "---\n反馈"
        with patcher:
            agent.run_agent(msg, workdir=ws,
                            log_stem=str(ws / "SEP2"), timeout_sec=5)
        self.assertNotIn(msg, captured["args"])
        self.assertEqual(captured["kw"].get("input"), msg)


class TestRunAgentStructured(unittest.TestCase):
    def test_first_try_ok(self):
        ws = Path(TMP)
        resp = [_phase_text({"phase": "done", "files": ["a.rs"]})]
        with mock.patch.object(agent, "run_agent",
                               side_effect=[(0, r) for r in resp]) as mg:
            rc, out, parsed = agent.run_agent_structured(
                "PROMPT", ws, str(ws / "ST"),
                gen_schema={"files": "list"})
        self.assertEqual(rc, 0)
        self.assertEqual(parsed["files"], ["a.rs"])
        self.assertEqual(mg.call_count, 1)
        self.assertIn("PROMPT", mg.call_args_list[0].args[0])

    def test_retry_then_ok(self):
        ws = Path(TMP)
        responses = [(0, "no json here"),
                     (0, _phase_text({"phase": "done", "files": []}))]
        with mock.patch.object(agent, "run_agent",
                               side_effect=responses) as mg:
            rc, out, parsed = agent.run_agent_structured(
                "PROMPT", ws, str(ws / "ST2"),
                gen_schema={"files": "list"}, max_tries=2)
        self.assertEqual(mg.call_count, 2)
        self.assertEqual(parsed["files"], [])
        second_prompt = mg.call_args_list[1].args[0]
        self.assertIn("未见合法 phase JSON", second_prompt)

    def test_exhausted(self):
        ws = Path(TMP)
        with mock.patch.object(agent, "run_agent",
                               side_effect=[(0, "junk")]) as mg:
            rc, out, parsed = agent.run_agent_structured(
                "PROMPT", ws, str(ws / "ST3"),
                gen_schema={"files": "list"}, max_tries=1)
        self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
