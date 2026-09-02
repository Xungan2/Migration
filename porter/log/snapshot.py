"""snapshot.py — 失败即快照（不可变证据束；log 子系统的证据保全面）。

自 loop/events.py 平移（行为不变）。判定 FAIL 的第一时间、任何重跑
之前抢救现场 → ws/failure-snapshot-<n>/（qemu.log/串口/判定输入/
内核哈希 best-effort/QEMU 命令行/criteria+mapping 状态 +
manifest.json）。快照不可变——升级报告的 evidence_files 只指向它。

体积纪律（docs/log.md）：内核镜像只存哈希不复制；mapping.json 超
_MAPPING_COPY_LIMIT 只记 sha256——大文件不整体进快照。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from . import store

_MAPPING_COPY_LIMIT = 2 * 1024 * 1024   # mapping.json 超此尺寸只记 sha256
# 快照单文件钳制（体积纪律，docs/log.md）：超阈值改"头+尾"裁剪复制，
# manifest 如实记 clipped——有界且不静默。
_CLIP_THRESHOLD = 5 * 1024 * 1024
_CLIP_HEAD = 1 * 1024 * 1024
_CLIP_TAIL = 2 * 1024 * 1024
# 内核镜像常见落点（best-effort；找不到记 not-found，不阻塞快照）
_KERNEL_GLOBS = ("osdk/build/*/kernel*", "osdk/build/kernel*",
                 "target/*/debug/kernel*", "target/*/release/kernel*")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _next_snapshot_idx(ws: Path) -> int:
    n = 0
    for d in ws.glob("failure-snapshot-*"):
        m = re.match(r"failure-snapshot-(\d+)$", d.name)
        if m and d.is_dir():
            n = max(n, int(m.group(1)))
    return n + 1


def _find_kernel(target_os: Path | None) -> dict:
    if target_os is None:
        return {"found": False, "reason": "no-target-os"}
    for pat in _KERNEL_GLOBS:
        for p in sorted(target_os.glob(pat)):
            if p.is_file():
                try:
                    return {"found": True, "path": str(p),
                            "size": p.stat().st_size, "sha256": _sha256(p)}
                except OSError:
                    continue
    return {"found": False, "reason": "no-kernel-glob-hit"}


def _qemu_cmdline(runner: dict | None, extra_env: dict | None) -> str:
    if not runner:
        return ""
    boot = (runner.get("boot") or {}).get("cmd") or ""
    inj = runner.get("inject_device") or {}
    parts = [boot]
    if inj.get("mechanism") == "env" and extra_env:
        for k, v in extra_env.items():
            parts.append(f"({k}={v})")
    elif inj.get("cmd_suffix"):
        parts.append(f"(suffix={inj['cmd_suffix']})")
    return " ".join(x for x in parts if x)


def _copy_in(src: Path, dst: Path, files: dict, key: str) -> None:
    """把 src 复制进快照；不存在记 missing；超阈值裁剪复制（记 clipped）。"""
    if src and src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        size = src.stat().st_size
        if size > _CLIP_THRESHOLD:
            with src.open("rb") as f:
                head = f.read(_CLIP_HEAD)
                f.seek(-_CLIP_TAIL, 2)
                tail = f.read(_CLIP_TAIL)
            dst.write_bytes(
                head + f"\n…[porter log: clipped, 原 {size} 字节，"
                f"中段省略 {size - _CLIP_HEAD - _CLIP_TAIL} 字节]\n"
                .encode("utf-8") + tail)
            files[key] = {"copied": str(dst.name), "size": size,
                          "clipped": True}
        else:
            dst.write_bytes(src.read_bytes())
            files[key] = {"copied": str(dst.name), "size": size}
    else:
        files[key] = {"missing": str(src) if src else "unset"}


def take_failure_snapshot(ws: Path, source: str, subject: str,
                          reason: str, runner: dict | None = None,
                          target_os: Path | None = None,
                          extra_files: list[tuple[Path, str]] | None = None,
                          extra_env: dict | None = None) -> Path | None:
    """失败现场抢救（重跑前调用）。返回快照目录；失败返回 None（不抛）。

    source ∈ {"p5","p6","d1",...}（挂载点）；subject = 判据 id / 红项 /
    defect id；extra_files = [(源路径, 快照内文件名), ...]（判定输入等）。
    """
    try:
        ws = Path(ws)
        n = _next_snapshot_idx(ws)
        snap = ws / f"failure-snapshot-{n}"
        snap.mkdir(parents=True, exist_ok=False)
        files: dict[str, dict] = {}

        # qemu.log / 串口日志（boot 判定的真值源）
        log_file = ((runner or {}).get("boot") or {}).get("log_file")
        if log_file:
            p = Path(log_file) if Path(log_file).is_absolute() \
                else (target_os / log_file if target_os else Path(log_file))
            _copy_in(p, snap / "qemu.log", files, "qemu_log")
        else:
            files["qemu_log"] = {"missing": "runner.boot.log_file unset"}
        serial = (target_os / "qemu-serial.log") if target_os else None
        _copy_in(serial, snap / "qemu-serial.log", files, "serial_log")

        # 调用方指定判定输入（acceptance / 红项 / criteria 等）
        for i, (src, name) in enumerate(extra_files or []):
            _copy_in(Path(src), snap / name, files, f"extra_{i}_{name}")

        # criteria + mapping 状态（缺省捎带 P2/mapping.json 的指纹/副本）
        mapping = ws / "P2" / "mapping.json"
        if mapping.is_file():
            if mapping.stat().st_size <= _MAPPING_COPY_LIMIT:
                _copy_in(mapping, snap / "mapping.json", files, "mapping")
            else:
                files["mapping"] = {"sha256": _sha256(mapping),
                                    "size": mapping.stat().st_size,
                                    "mode": "hash-only"}

        manifest = {"n": n, "time": datetime.now().isoformat(
                        timespec="seconds"),
                    "source": source, "subject": subject, "reason": reason,
                    "files": files,
                    "kernel": _find_kernel(target_os),
                    "qemu_cmdline": _qemu_cmdline(runner, extra_env)}
        (snap / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8")
        store.append_event("snapshot", subject=subject, intent=source,
                           summary=f"failure-snapshot-{n}: {reason}", ws=ws,
                           mount=source)
        return snap
    except OSError:
        return None
