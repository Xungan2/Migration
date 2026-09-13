"""test_check_template.py — v2 检查模板（skills/assets/check-template.py）

真子进程冒烟：证据三层落盘（cmd-i.log 全量 / snapshots 快照 / 有标记
瘦回显）、占位符安全替换（${VAR} 不被打碎）、not-applicable、判定语义
回归、EVIDENCE_DIR 缺席回退。模板是样例身份（工具代码零消费点），
本测试守住样例本身的行为契约。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = TOOL_ROOT / "porter" / "skills" / "assets" / "check-template.py"


def _run(tmp: Path, doc: dict, env_extra: dict | None = None,
         unset_evidence: bool = False) -> tuple[int, str, Path]:
    """写 JSON、跑模板，返回 (rc, stdout, evidence 目录)。"""
    jp = tmp / "9-test.json"
    jp.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                  encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin",
           "PORTER_TARGET_OS_ROOT": str(tmp / "tree"),
           "PORTER_DRIVER_HOME": "home/drv"}
    if not unset_evidence:
        env["PORTER_EVIDENCE_DIR"] = str(tmp / "ev")
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, str(TEMPLATE), str(jp)],
                       env=env, capture_output=True, text=True, timeout=120)
    ev = Path(env["PORTER_EVIDENCE_DIR"]) if "PORTER_EVIDENCE_DIR" in env \
        else jp.parent / ".evidence"
    return p.returncode, p.stdout + p.stderr, ev


class TestCheckTemplate(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "tree").mkdir()
        (self.tmp / "tree" / "home").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_output_archived_and_marker(self):
        """长输出：cmd-1.log 全量落盘 + 回显带截断标记含路径。"""
        big = "x" * 2000
        rc, out, ev = _run(self.tmp, {
            "commands": [{"cmd": f"printf '%s' '{big}'"}],
            "expect": {"rc": 0}})
        self.assertEqual(rc, 0)
        ce = ev / "cmd-1.log"
        self.assertEqual(ce.read_text(encoding="utf-8"), big)
        self.assertIn("truncated", out)
        self.assertIn("cmd-1.log", out)
        self.assertIn("evidence: cmd-1.log", out)

    def test_snapshots_copied_and_missing_noted(self):
        """snapshots：在场文件拷贝；缺席打 note 不失败。"""
        (self.tmp / "tree" / "qemu.log").write_text("guest-log",
                                                    encoding="utf-8")
        rc, out, ev = _run(self.tmp, {
            "commands": [{"cmd": "true",
                          "snapshots": ["qemu.log", "nope.log"]}],
            "expect": {"rc": 0}})
        self.assertEqual(rc, 0)
        self.assertEqual((ev / "cmd-1.qemu.log").read_text(
            encoding="utf-8"), "guest-log")
        self.assertIn("snapshot 缺席", out)
        self.assertIn("cmd-1.qemu.log", out)

    def test_placeholder_safe(self):
        """${VAR} shell 展开不被打碎；独立 {VAR} 被脚本替换。"""
        drv = self.tmp / "tree" / "home" / "drv"
        drv.mkdir(parents=True)
        (drv / "f.txt").write_text("hi", encoding="utf-8")
        (self.tmp / "tree" / "probe.txt").write_text("probe-ok",
                                                     encoding="utf-8")
        rc, out, ev = _run(self.tmp, {
            "commands": [{"cmd": "cat ${PORTER_TARGET_OS_ROOT}/probe.txt"
                                 " && cat {PORTER_DRIVER_HOME}/f.txt"}],
            "expect": {"rc": 0, "log_contains": ["probe-ok", "hi"]}})
        self.assertEqual(rc, 0)
        self.assertNotIn("${", ev.joinpath("cmd-1.log").read_text(
            encoding="utf-8").replace("${PORTER_TARGET_OS_ROOT}", ""))
        self.assertNotIn("{PORTER_DRIVER_HOME}",
                         (ev / "cmd-1.log").read_text(encoding="utf-8"))

    def test_not_applicable(self):
        """空 commands：rc 0、说明行、零证据文件。"""
        rc, out, ev = _run(self.tmp, {"commands": [], "notes": "N/A"})
        self.assertEqual(rc, 0)
        self.assertIn("not-applicable", out)
        self.assertFalse(list(ev.glob("cmd-*.log")))

    def test_judgment_semantics(self):
        """rc/contains/not_contains/min_matches 合取语义回归。"""
        rc, out, _ = _run(self.tmp, {
            "commands": [{"cmd": "printf 'alpha\\nbeta\\nalpha\\n'"}],
            "expect": {"rc": 0,
                       "log_contains": ["alpha"],
                       "log_not_contains": ["gamma"],
                       "min_matches": [{"expr": "alpha", "count": 2}]}})
        self.assertEqual(rc, 0)
        rc2, out2, _ = _run(self.tmp, {
            "commands": [{"cmd": "printf 'alpha\\n'"}],
            "expect": {"rc": 0,
                       "min_matches": [{"expr": "alpha", "count": 5}]}})
        self.assertEqual(rc2, 1)
        self.assertIn("FAIL", out2)
        rc3, _, _ = _run(self.tmp, {
            "commands": [{"cmd": "printf 'boom\\n'; exit 3"}],
            "expect": {"rc": 0}})
        self.assertEqual(rc3, 1)

    def test_evidence_dir_fallback(self):
        """PORTER_EVIDENCE_DIR 缺席：回退 <json 同目录>/.evidence/。"""
        rc, _, ev = _run(self.tmp, {
            "commands": [{"cmd": "echo hi"}], "expect": {"rc": 0}},
            unset_evidence=True)
        self.assertEqual(rc, 0)
        self.assertTrue((ev / "cmd-1.log").exists())
        self.assertEqual(ev.name, ".evidence")


if __name__ == "__main__":
    unittest.main()
