"""porter/loop/events.py 单元测试（§15 子系统 B：events.jsonl + 失败快照）。

无 agent / 无网络 / 无 docker。覆盖：
A. append_event：绑定后追加 / 未绑定 no-op / 字段截断 / 坏调用不抛
B. read/tail_events：过滤（subject 前缀 / mount / kind 前缀）与 limit
C. 快照：qemu.log+串口+extra_files 复制 / manifest 字段 / 序号递增 /
   mapping 大文件 hash-only / 内核 glob 命中与 not-found / QEMU cmdline
D. 埋桩联动：bind 后 note_agent_start/end 与 note_cmd_* 落 events.jsonl
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import events as EV


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_ev_t_"))
        EV.unbind()

    def tearDown(self):
        EV.unbind()

    def test_a_append_and_nobind(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        # 未绑定：no-op
        ok("A1 未绑定 no-op", EV.append_event("x") is False)
        ok("A2 未绑定不产文件", not (ws / "events.jsonl").exists())
        # 绑定后追加
        EV.bind(ws, "p5")
        ok("A3 绑定后写入", EV.append_event("agent_start",
                                            intent="P5_ut_R1") is True)
        EV.append_event("agent_end", intent="P5_ut_R1", rc=0,
                        summary="ok" * 300, subject="modA.c1")
        evs = EV.read_events(ws)
        ok("A4 两条事件", len(evs) == 2)
        ok("A5 mount 继承", evs[0]["mount"] == "p5")
        ok("A6 time 存在", bool(evs[0]["time"]))
        ok("A7 截断生效", len(evs[1]["summary"]) <= 402)
        ok("A8 subject 记录", evs[1]["subject"] == "modA.c1")

    def test_a_no_raise(self):
        EV.bind(Path("/nonexistent/nowhere"), "p5")
        ok("A9 坏路径不抛", EV.append_event("x") is False)

    def test_b_tail_filter(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        EV.bind(ws, "p6")
        for i in range(5):
            EV.append_event("cmd_start", subject=f"modA.c{i}", cmd=f"c{i}")
            EV.append_event("cmd_end", subject=f"modA.c{i}", rc=0)
        EV.append_event("snapshot", subject="modB.d1", summary="s")
        t = EV.tail_events(ws, subject="modA.c3")
        ok("B1 subject 精确+路径前缀", {e["kind"] for e in t} ==
           {"cmd_start", "cmd_end"} and all(
               e["subject"] == "modA.c3" for e in t))
        t = EV.tail_events(ws, kind_prefix="cmd_end")
        ok("B2 kind 前缀", len(t) == 5)
        t = EV.tail_events(ws, limit=3)
        ok("B3 limit 尾部", len(t) == 3 and t[-1]["subject"] == "modB.d1")

    def _mk_ws(self):
        ws = self.tmp / f"ws_{len(list(self.tmp.iterdir()))}"
        tos = self.tmp / "tos"
        tos.mkdir(exist_ok=True)
        ws.mkdir(exist_ok=True)
        (ws / "P3" / "modA" / "reports").mkdir(parents=True, exist_ok=True)
        (ws / "P2").mkdir(exist_ok=True)
        (tos / "qemu.log").write_text("boot log … panic", encoding="utf-8")
        (tos / "qemu-serial.log").write_text("serial…", encoding="utf-8")
        (ws / "P3" / "modA" / "reports" / "criteria.json").write_text(
            json.dumps({"criteria": [{"id": "modA.c1"}]}), encoding="utf-8")
        runner = {"boot": {"cmd": "make run_kernel",
                           "log_file": "qemu.log"},
                  "inject_device": {"mechanism": "env",
                                    "env": {"EXTRA_QEMU_ARGS":
                                            "-netdev user,id=e1"}}}
        return ws, tos, runner

    def test_c_snapshot(self):
        ws, tos, runner = self._mk_ws()
        snap = EV.take_failure_snapshot(
            ws, "p5", "modA.c1", "L3 hits=0",
            runner=runner, target_os=tos,
            extra_env={"EXTRA_QEMU_ARGS": "-device e1000"},
            extra_files=[(ws / "P3" / "modA" / "reports" /
                          "criteria.json", "criteria.json")])
        ok("C1 返回快照目录", snap is not None and snap.name ==
           "failure-snapshot-1")
        man = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
        ok("C2 manifest 基本字段",
           man["source"] == "p5" and man["subject"] == "modA.c1"
           and man["n"] == 1)
        ok("C3 qemu.log 复制", (snap / "qemu.log").read_text(
            encoding="utf-8").startswith("boot log"))
        ok("C4 串口复制", (snap / "qemu-serial.log").exists())
        ok("C5 extra 判定输入", (snap / "criteria.json").exists())
        ok("C6 cmdline 含设备参数",
           "-device e1000" in man["qemu_cmdline"]
           and "run_kernel" in man["qemu_cmdline"])
        ok("C7 内核 not-found 容忍", man["kernel"]["found"] is False)
        # 小 mapping = 全量复制
        (ws / "P2" / "mapping.json").write_text("{}", encoding="utf-8")
        # 内核 glob 命中
        (tos / "osdk" / "build" / "k").mkdir(parents=True, exist_ok=True)
        kp = tos / "osdk" / "build" / "k" / "kernel-x86_64"
        kp.write_bytes(b"ELF...")
        snap2 = EV.take_failure_snapshot(ws, "p6", "P6.boot", "boot FAIL",
                                         runner=runner, target_os=tos)
        man2 = json.loads((snap2 / "manifest.json").read_text(
            encoding="utf-8"))
        ok("C8 序号递增", snap2.name == "failure-snapshot-2")
        ok("C9 内核哈希命中", man2["kernel"]["found"] is True
           and len(man2["kernel"]["sha256"]) == 64)
        ok("C10 mapping 全量复制", (snap2 / "mapping.json").exists())
        # 快照事件落账
        evs = EV.read_events(ws)
        ok("C11 snapshot 事件 ×2",
           sum(1 for e in evs if e["kind"] == "snapshot") == 2)

    def test_c_mapping_hash_only(self):
        ws, tos, runner = self._mk_ws()
        big = ws / "P2" / "mapping.json"
        big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        snap = EV.take_failure_snapshot(ws, "p5", "modA.c1", "r",
                                        runner=runner, target_os=tos)
        man = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
        ok("C12 大 mapping hash-only",
           man["files"]["mapping"].get("mode") == "hash-only"
           and not (snap / "mapping.json").exists())

    def test_d_notes(self):
        ws = self.tmp / "ws"
        ws.mkdir()
        EV.bind(ws, "d1")
        EV.note_agent_start("D1_triage_R1", "prompt…")
        EV.note_agent_end("D1_triage_R1", 0, "out\n尾行")
        EV.note_cmd_start("make kernel", ws / "x.log")
        EV.note_cmd_end("make kernel", 2, "err", 12.3, ws / "x.log")
        evs = EV.read_events(ws)
        ok("D1 四条埋桩", [e["kind"] for e in evs] ==
           ["agent_start", "agent_end", "cmd_start", "cmd_end"])
        ok("D2 agent_end rc", evs[1]["rc"] == 0 and "尾行" in
           evs[1]["summary"])
        ok("D3 cmd_end 耗时", "12s" in evs[3]["summary"])


if __name__ == "__main__":
    unittest.main()
