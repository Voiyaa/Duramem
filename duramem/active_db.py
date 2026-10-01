"""「当前记忆库」指针：前端选定，MCP 跟随。

一个库 = 一个文件是**硬隔离**边界，所以"我现在在用哪个库"是一件跨进程的事：
界面在 `serve` 进程里，工具调用在 MCP 子进程里，两者没有共享内存。指针文件就是
它们之间唯一的传话方式——写一次，读很多次。

两个刻意的选择：

1. **单独一个文件，不写进 `registry.json`。** registry 有多个进程会**整份回写**
   （create / rename / delete / discover），谁内存里揣着旧副本谁就能把用户的选择
   覆盖掉——`zcode-2` 那次"同一份记忆两个身份"就是同一类事故。
2. **写入是原子的**（临时文件 + `os.replace`），读取按 mtime 缓存。因为读取频率很高
   （MCP 每次工具调用都会问一次"当前是哪个库"），半截文件会让检索落到错误的库上。

指针**不保证指向存在的库**：库可能被删掉。这种时候不静默兜底到别的库，而是
由调用方响亮报错（静默兜底正是"看起来正常、其实查错库"的来源）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_FILE = "active_db.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def path_for(data_dir: str | Path) -> Path:
    return Path(data_dir) / ACTIVE_FILE


def read(data_dir: str | Path) -> dict[str, Any]:
    """读指针。

    文件不存在 = 还没选定过（不是错误）。文件坏掉 = 当作没选定，但把原因放进
    `warning`，由调用方决定要不要说出来——不能假装无事发生。
    """
    path = path_for(data_dir)
    info: dict[str, Any] = {
        "db": None,
        "updated_at": None,
        "updated_by": None,
        "warning": None,
    }
    if not path.exists():
        return info
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        info["warning"] = f"当前记忆库指针不可读（{path.name}）：{exc}"
        return info
    if not isinstance(raw, dict):
        info["warning"] = f"当前记忆库指针格式不对（{path.name}）"
        return info
    name = raw.get("db")
    info["db"] = name.strip() if isinstance(name, str) and name.strip() else None
    info["updated_at"] = raw.get("updated_at")
    info["updated_by"] = raw.get("updated_by")
    if info["db"] is None:
        info["warning"] = f"当前记忆库指针里没有库名（{path.name}）"
    return info


def write(data_dir: str | Path, name: str, by: str = "ui") -> dict[str, Any]:
    """原子写入指针。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("库名不能为空")
    path = path_for(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"db": name, "updated_at": now_iso(), "updated_by": by}
    tmp = path.with_name(path.name + ".tmp")
    # 先写临时文件再 os.replace：读取方要么看到旧内容、要么看到新内容，
    # 绝不会读到写了一半的 JSON。
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return payload


def clear(data_dir: str | Path) -> None:
    """删掉指针（没有库可用时用）。"""
    try:
        path_for(data_dir).unlink()
    except FileNotFoundError:
        pass


__all__ = ["ACTIVE_FILE", "clear", "now_iso", "path_for", "read", "write"]
