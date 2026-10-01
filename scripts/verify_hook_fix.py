"""临时库上的 hook 端到端验证（不动真库）。

验证四件事：
1. 从**外部 cwd**（模拟宿主拉起 hook 的真实处境）能打开库；
2. hook 写入的 window_id 与导入侧一致（`zcode:<目录名>`），不再各存一份；
3. 转录文件被读取，seq 按位置编号；
4. 重复投递幂等——不产生副本。

用法：.venv/Scripts/python scripts/verify_hook_fix.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DM = ROOT / ".venv" / "Scripts" / "duramem.exe"
REAL_DB = ROOT / "data" / "zcode.db"
CWD = Path("E:/Zcode/Work Space")  # 故意用工作区当 cwd：宿主就是这么拉起 hook 的

TRANSCRIPT = [
    {"role": "user", "content": "验证：hook 采集到的第一条"},
    {"role": "assistant", "content": "验证：hook 采集到的第二条"},
    {"role": "user", "content": "验证：hook 采集到的第三条"},
    {"role": "assistant", "content": "验证：hook 采集到的第四条"},
]


def run_hook(data_dir: Path, payload: dict, cwd: Path) -> dict:
    proc = subprocess.run(
        [
            str(DM),
            "hook-ingest",
            "--event",
            "UserPromptSubmit",
            "--db",
            "zcode",
            "--data-dir",
            str(data_dir),
            "--verbose",
        ],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        cwd=str(cwd),
        timeout=120,
    )
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(stderr.splitlines()[-1]) if stderr else {}
    except json.JSONDecodeError:
        return {"_stderr": stderr}


def main() -> int:
    if not REAL_DB.exists():
        print(f"找不到真库：{REAL_DB}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="duramem-hook-verify-"))
    try:
        target = tmp / "zcode.db"
        shutil.copy2(REAL_DB, target)

        # provider_config.json 也要带上：它就是"数据目录"的一部分，缺了它
        # 提供方会退到离线哈希，与库里的向量来源对不上（真实配置下两边都是 BGE-M3）。
        provider_cfg = ROOT / "data" / "provider_config.json"
        if provider_cfg.exists():
            shutil.copy2(provider_cfg, tmp / "provider_config.json")
            print("已带上 provider_config.json（提供方会按配置解析）")
        else:
            print("警告：没有 provider_config.json，提供方会退到离线哈希")

        # 登记占位：绝对路径。hook 每次都是新进程，直接读这个文件。
        (tmp / "registry.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "databases": {
                        "zcode": {
                            "display_name": "zcode",
                            "file_path": str(target),
                            "db_uuid": "a6947079651c",
                            "created_at": "2026-09-24T00:00:00+00:00",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        transcript = tmp / "transcript.jsonl"
        transcript.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in TRANSCRIPT),
            encoding="utf-8",
        )

        before = sqlite3.connect(f"file:{target}?mode=ro", uri=True).execute(
            "SELECT count(*) FROM messages"
        ).fetchone()[0]

        payload = {
            "session_id": "sess_hook_verify",
            "cwd": str(CWD),
            "prompt": "载荷里的文本（转录文件存在时应以转录为准）",
            "transcript_path": str(transcript),
        }

        print(f"cwd        : {CWD}")
        print(f"data_dir   : {tmp}")
        print(f"hook 前消息: {before}")
        print()

        first = run_hook(tmp, payload, CWD)
        print(f"第 1 次 hook: {json.dumps(first, ensure_ascii=False)}")
        second = run_hook(tmp, payload, CWD)
        print(f"第 2 次 hook: {json.dumps(second, ensure_ascii=False)}")
        print()

        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        after = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        rows = conn.execute(
            "SELECT seq, role, content FROM messages "
            "WHERE session_id='sess_hook_verify' ORDER BY seq"
        ).fetchall()
        window = conn.execute(
            "SELECT DISTINCT window_id FROM messages WHERE session_id='sess_hook_verify'"
        ).fetchall()
        print(f"hook 后消息: {after}（+{after - before}）")
        print(f"window_id  : {[w[0] for w in window]}")
        print("入库内容   :")
        for seq, role, content in rows:
            print(f"  seq={seq} {role:9} {content}")

        imported_windows = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT window_id FROM messages "
                "WHERE session_id LIKE 'sess_%' AND session_id != 'sess_hook_verify' LIMIT 3"
            )
        ]
        print(f"导入侧的 window_id（应为同一个）: {imported_windows}")

        ok = (
            first.get("collected") == 4
            and not first.get("warnings")
            and after - before == 4
            and [w[0] for w in window] == ["zcode:Work Space"]
            and [r[0] for r in rows] == [0, 1, 2, 3]
        )
        print()
        print("结论:", "通过" if ok else "未通过")
        return 0 if ok else 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
