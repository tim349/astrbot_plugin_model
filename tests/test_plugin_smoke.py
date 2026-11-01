"""插件冒烟测试：不需要安装 AstrBot。

用假的 astrbot 模块注入 sys.modules，验证：
1. main.py 能正常导入、类能实例化；
2. 三个 llm_tool 与两个指令都注册在插件类上；
3. 子进程调用 core/scripts/gunpla.py 的链路通（用离线的 zh 命令，不访问网络）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _install_astrbot_stub() -> None:
    """注入最小可用的 astrbot.* 桩模块。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: D401 - stub
        message_str = ""

        def plain_result(self, text: str):
            return text

    class MessageEventResult:  # noqa: D401 - stub
        pass

    class _Filter:
        @staticmethod
        def llm_tool(name: str | None = None):
            def deco(func):
                func._is_llm_tool = True
                func._tool_name = name or func.__name__
                return func

            return deco

        @staticmethod
        def command(name: str | None = None):
            def deco(func):
                func._is_command = True
                func._command_name = name or func.__name__
                return func

            return deco

    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageEventResult = MessageEventResult

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None) -> None:
            self.context = context

    class Context:  # noqa: D401 - stub
        pass

    star_mod.Star = Star
    star_mod.Context = Context

    api.event = event_mod
    api.star = star_mod
    astrbot.api = api

    for name, module in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.star", star_mod),
    ):
        sys.modules[name] = module


def _load_plugin():
    _install_astrbot_stub()
    spec = importlib.util.spec_from_file_location(
        "gunpla_plugin_main", PLUGIN_DIR / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PluginSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_plugin()
        cls.plugin = cls.mod.GunplaPlugin(context=types.SimpleNamespace())

    def test_tools_registered(self) -> None:
        names = {
            getattr(obj, "_tool_name", None)
            for obj in vars(type(self.plugin)).values()
            if getattr(obj, "_is_llm_tool", False)
        }
        self.assertEqual(
            names,
            {"gunpla_manual_search", "gunpla_news", "gunpla_manual_by_dalong_id"},
        )

    def test_commands_registered(self) -> None:
        names = {
            getattr(obj, "_command_name", None)
            for obj in vars(type(self.plugin)).values()
            if getattr(obj, "_is_command", False)
        }
        self.assertEqual(names, {"gunpla", "gunpla_news"})

    def test_not_found_constant_matches_skill(self) -> None:
        self.assertEqual(
            self.mod.DEFAULT_NOT_FOUND,
            "没有找到对应的内容，可能网站还没有搜录，或者更换描述后重试",
        )

    def test_cli_path(self) -> None:
        """跑一遍查询链路（glossary 翻译，不联网）。"""
        text = asyncio.run(self.plugin._run_cli("zh", "クシャトリヤ"))
        self.assertIn("刹帝利", text)

    def test_empty_result_falls_back_to_not_found(self) -> None:
        plugin = self.mod.GunplaPlugin(context=types.SimpleNamespace())

        async def boom(*args, **kwargs):
            raise PermissionError("no pipes")

        async def empty(*args, **kwargs):
            return ""

        plugin._run_subprocess = boom  # type: ignore[method-assign]
        plugin._run_inprocess = empty  # type: ignore[method-assign]
        text = asyncio.run(plugin._run_cli("manual", "x"))
        self.assertEqual(text, self.mod.DEFAULT_NOT_FOUND)

    def test_config_is_applied(self) -> None:
        cfg = {
            "timeout_seconds": 45,
            "max_results": 2,
            "news_limit": 3,
            "default_news_source": "dalong",
            "enable_commands": False,
            "not_found_message": "自定义兜底",
        }
        plugin = self.mod.GunplaPlugin(context=types.SimpleNamespace(), config=cfg)
        self.assertEqual(plugin.timeout, 45.0)
        self.assertEqual(plugin.max_results, 2)
        self.assertEqual(plugin.news_limit, 3)
        self.assertEqual(plugin.default_news_source, "dalong")
        self.assertFalse(plugin.enable_commands)
        self.assertEqual(plugin.not_found, "自定义兜底")

    def test_config_clamps_bad_values(self) -> None:
        plugin = self.mod.GunplaPlugin(
            context=types.SimpleNamespace(),
            config={"timeout_seconds": 1, "max_results": 99, "default_news_source": "bogus"},
        )
        self.assertEqual(plugin.timeout, 10.0)
        self.assertEqual(plugin.max_results, 10)
        self.assertEqual(plugin.default_news_source, "all")

    def test_tool_uses_configured_limit(self) -> None:
        plugin = self.mod.GunplaPlugin(
            context=types.SimpleNamespace(), config={"max_results": 2}
        )
        captured = {}

        async def fake(*args, **kwargs):
            captured["args"] = args
            return "ok"

        plugin._run_cli = fake  # type: ignore[method-assign]

        class Ev:
            def plain_result(self, text):
                return text

        async def run():
            assert not hasattr(plugin.gunpla_manual_search, "__aiter__")
            return await plugin.gunpla_manual_search(Ev(), "雪兔")

        self.assertEqual(asyncio.run(run()), "ok")
        self.assertEqual(captured["args"][-1], "2")

    def test_timeout_returns_not_found(self) -> None:
        plugin = self.mod.GunplaPlugin(context=types.SimpleNamespace())

        async def slow(*args, **kwargs):
            await asyncio.sleep(5)
            return "late"

        plugin._run_subprocess = slow  # type: ignore[method-assign]
        text = asyncio.run(plugin._run_cli("manual", "x", budget=0.1))
        self.assertEqual(text, plugin.not_found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
