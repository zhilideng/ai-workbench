"""运行日志：每次运行一个 JSONL 文件。保留 CLI 事件内容并记录接收时间。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator, TextIO

from workbench.core.settings import get_settings


def path(run_id: int) -> Path:
    return get_settings().paths.runs / f"run-{run_id}.jsonl"


def append_wb(run_id: int, subtype: str, **fields) -> None:
    with open(path(run_id), "a", encoding="utf-8") as f:
        write_event(f, {"type": "wb", "subtype": subtype, **fields})


def write_event(f: TextIO, event: dict) -> None:
    f.write(json.dumps({**event, "wb_at": time.time()}, ensure_ascii=False) + "\n")
    f.flush()


def events(run_id: int) -> Iterator[dict]:
    log = path(run_id)
    if not log.exists():
        return
    with open(log, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


def read_from(run_id: int, offset: int) -> tuple[list[dict], int]:
    """增量读取：offset 为字节偏移，只消费完整的行。返回 (新事件, 下一个 offset)。"""
    log = path(run_id)
    out: list[dict] = []
    if not log.exists():
        return out, offset
    with open(log, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    for line in data[:end].splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out, offset + end


def delete(run_id: int) -> None:
    path(run_id).unlink(missing_ok=True)
