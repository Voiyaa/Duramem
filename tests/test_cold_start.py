"""MCP 冷启动注入（每库的库主留言，配置存各库 db_meta）。

只该在"边沿"出现：进程刚启动即见开、开关关→开、或留言内容改了——满足其一
的下一次以该库为目标的调用注入一次，之后静默。状态按库隔离：work 库注入过
不影响 life 库。装饰器注册 + SDK schema 内省这条真实链路由 test_mcp_stdio.py
的端到端测试保证。
"""

from __future__ import annotations

from duramem.mcp_server import _ColdStartNote


class _StubService:
    """注入器只依赖 service.cold_start_view(db)，用桩模拟每库配置。"""

    def __init__(self, views: dict[str, dict]) -> None:
        self.views = views

    def cold_start_view(self, db: str) -> dict:
        if db not in self.views:
            raise KeyError(db)
        return {"db": db, **self.views[db]}


def test_empty_note_is_transparent():
    injector = _ColdStartNote(_StubService({"work": {"note": "", "enabled": True}}))
    payload = {"results": [], "retrieval_mode": "none"}
    assert injector.apply("work", payload) is payload


def test_fires_once_per_db_then_silent():
    injector = _ColdStartNote(_StubService({"work": {"note": "项目代号 K3", "enabled": True}}))
    first = injector.apply("work", {"ok": True})
    assert "库主备注" in first["cold_start_note"]
    assert "「work」" in first["cold_start_note"]  # 框架语说明留言属于哪个库
    assert "K3" in first["cold_start_note"]

    assert "cold_start_note" not in injector.apply("work", {"ok": True})


def test_note_is_the_first_key_of_payload():
    """留言刻意排在返回体第一个键：模型按顺序读，先框架后内容；
    原有键的相对顺序不动（results 仍在 sessions 之前）。"""
    injector = _ColdStartNote(_StubService({"work": {"note": "x", "enabled": True}}))
    out = injector.apply("work", {"results": [1, 2], "count": 2, "sessions": []})
    assert next(iter(out)) == "cold_start_note"
    assert list(out)[1:] == ["results", "count", "sessions"]


def test_databases_are_isolated():
    injector = _ColdStartNote(
        _StubService(
            {
                "work": {"note": "work 的说明", "enabled": True},
                "life": {"note": "life 的说明", "enabled": True},
            }
        )
    )
    first = injector.apply("work", {"ok": True})
    assert "work 的说明" in first["cold_start_note"]

    # life 自己的第一次调用照常注入，与 work 的状态无关
    other = injector.apply("life", {"ok": True})
    assert "life 的说明" in other["cold_start_note"]
    assert "cold_start_note" not in injector.apply("work", {"ok": True})
    assert "cold_start_note" not in injector.apply("life", {"ok": True})


def test_switch_toggled_off_on_rearms():
    views = {"work": {"note": "备忘", "enabled": True}}
    injector = _ColdStartNote(_StubService(views))

    assert "cold_start_note" in injector.apply("work", {})  # 首见即开：注入
    assert "cold_start_note" not in injector.apply("work", {})  # 之后静默

    views["work"]["enabled"] = False
    assert "cold_start_note" not in injector.apply("work", {})  # 关：不注入

    views["work"]["enabled"] = True
    assert "cold_start_note" in injector.apply("work", {})  # 重拨开：边沿，再注入
    assert "cold_start_note" not in injector.apply("work", {})


def test_note_edit_rearms():
    """改留言内容也是一次新边沿——最顺手的重注入方式。"""
    views = {"work": {"note": "第一版", "enabled": True}}
    injector = _ColdStartNote(_StubService(views))
    assert "第一版" in injector.apply("work", {})["cold_start_note"]
    assert "cold_start_note" not in injector.apply("work", {})

    views["work"]["note"] = "第二版"
    assert "第二版" in injector.apply("work", {})["cold_start_note"]
    assert "cold_start_note" not in injector.apply("work", {})


def test_unknown_db_stays_silent():
    injector = _ColdStartNote(_StubService({}))
    payload = {"ok": False, "error": "库打不开"}
    assert injector.apply("gone", payload) is payload


def test_source_payload_not_mutated():
    injector = _ColdStartNote(_StubService({"work": {"note": "x", "enabled": True}}))
    payload = {"ok": True}
    injector.apply("work", payload)
    assert payload == {"ok": True}
