"""AstrBot 插件：拼装模型说明书 / 新品查询。

为什么要有这个插件（而不是只放一个 Skill）：
- Skill 只是提示词，模型可以不读；插件注册的是 LLM Tool，和「网页搜索」同级，
  工具描述里写清「优先于通用联网搜索」，模型才会在 function calling 里选中它。
- 查询逻辑复用 skill 里的纯标准库脚本（core/scripts），通过子进程调用，
  不污染 AstrBot 的 Python 环境，也天然和其他插件隔离。

数据源：Dalong（日/英/韩说明书）+ 寿屋台湾（预购资讯）。全部标准库，无需第三方依赖。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star

try:  # AstrBot 会注入 AstrBotConfig；桩环境/独立测试时退回 dict
    from astrbot.api import AstrBotConfig
except ImportError:  # pragma: no cover
    AstrBotConfig = dict  # type: ignore[assignment,misc]

PLUGIN_DIR = Path(__file__).resolve().parent
CORE_DIR = PLUGIN_DIR / "core" / "scripts"
CLI = CORE_DIR / "gunpla.py"

# 默认值必须与 _conf_schema.json 保持一致
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RESULTS = 5
DEFAULT_NEWS_LIMIT = 8
DEFAULT_NEWS_SOURCE = "all"
DEFAULT_NOT_FOUND = "没有找到对应的内容，可能网站还没有搜录，或者更换描述后重试"

_CORE_MODULE = None


def _load_core():
    """导入 core/scripts/gunpla.py（只导入一次，供无管道环境兜底使用）。"""
    global _CORE_MODULE
    if _CORE_MODULE is not None:
        return _CORE_MODULE
    if str(CORE_DIR) not in sys.path:
        sys.path.insert(0, str(CORE_DIR))
    spec = importlib.util.spec_from_file_location("astrbot_gunpla_core_cli", CLI)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失
        raise ImportError(f"找不到查询脚本: {CLI}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _CORE_MODULE = module
    return module


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class GunplaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        cfg = config or {}

        # 30 秒是硬性约定，配置只允许在 10～120 秒之间微调
        self.timeout = float(
            _clamp_int(cfg.get("timeout_seconds", DEFAULT_TIMEOUT), int(DEFAULT_TIMEOUT), 10, 120)
        )
        self.max_results = _clamp_int(cfg.get("max_results", DEFAULT_MAX_RESULTS), DEFAULT_MAX_RESULTS, 1, 10)
        self.news_limit = _clamp_int(cfg.get("news_limit", DEFAULT_NEWS_LIMIT), DEFAULT_NEWS_LIMIT, 1, 20)

        source = str(cfg.get("default_news_source") or DEFAULT_NEWS_SOURCE).lower()
        self.default_news_source = source if source in ("all", "koto", "dalong") else DEFAULT_NEWS_SOURCE

        self.enable_commands = bool(cfg.get("enable_commands", True))
        message = str(cfg.get("not_found_message") or "").strip()
        self.not_found = message or DEFAULT_NOT_FOUND

    # ------------------------------------------------------------------
    # 执行层
    # ------------------------------------------------------------------

    async def _run_cli(self, *args: str, budget: float | None = None) -> str:
        """执行一次查询，超时或异常统一返回兜底话术。

        优先子进程（与 AstrBot 运行环境隔离）；宿主禁止创建管道时自动退回进程内调用。
        """
        limit = budget if budget is not None else self.timeout
        try:
            text = await asyncio.wait_for(
                self._run_subprocess(args, limit), timeout=limit + 2.0
            )
        except (OSError, PermissionError, NotImplementedError) as exc:
            logger.warning(f"[gunpla] 子进程不可用（{exc}），改用进程内调用")
            text = await self._run_inprocess(args, limit)
        except asyncio.TimeoutError:
            logger.warning(f"[gunpla] 查询整体超时: {args}")
            return self.not_found
        return text or self.not_found

    async def _run_subprocess(self, args: tuple[str, ...], budget: float) -> str:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(CLI),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(CORE_DIR),
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=budget)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.warning(f"[gunpla] 查询超过 {budget:.0f} 秒，已中止: {args}")
            return self.not_found

        text = (out or b"").decode("utf-8", "replace").strip()
        err_text = (err or b"").decode("utf-8", "replace").strip()
        if err_text:
            logger.debug(f"[gunpla] stderr: {err_text[:500]}")
        if not text:
            logger.warning(f"[gunpla] 空输出，返回码 {proc.returncode}: {args}")
        return text

    async def _run_inprocess(self, args: tuple[str, ...], budget: float) -> str:
        def call() -> str:
            module = _load_core()
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
                module.main(list(args))
            return buffer.getvalue().strip()

        try:
            return await asyncio.wait_for(asyncio.to_thread(call), timeout=budget)
        except asyncio.TimeoutError:
            logger.warning(f"[gunpla] 进程内查询超过 {budget:.0f} 秒: {args}")
            return self.not_found
        except Exception as exc:  # noqa: BLE001 - 任何解析/网络异常都按查不到处理
            logger.error(f"[gunpla] 进程内查询失败: {exc}")
            return self.not_found

    # ------------------------------------------------------------------
    # LLM Tools
    # ------------------------------------------------------------------

    @filter.llm_tool(name="gunpla_manual_search")
    async def gunpla_manual_search(
        self, event: AstrMessageEvent, keyword: str
    ) -> str:
        """查询拼装模型（高达/寿屋/机娘）的说明书链接，返回日/英/韩三语页面与评测页。

        当用户问「XX 的说明书/组装图/图纸在哪」时调用本工具。请优先于通用联网搜索使用。
        支持中文俗称、日文名、英文名、商品编号，以及 Dalong 编号（如 mdv26）。
        若返回「没有找到对应的内容…」，说明确实查不到，不要再反复换关键词重试。

        Args:
            keyword(string): 模型名称或编号，例如「雪兔」「刹帝利」「mdv271」「KP481」
        """
        logger.info(f"[gunpla] tool=gunpla_manual_search keyword={keyword!r}")
        text = await self._run_cli("manual", keyword, "--limit", str(self.max_results))
        return text  # 注意：llm_tool 必须 return，不能 yield（否则 agent runner 拿不到结果）

    @filter.llm_tool(name="gunpla_news")
    async def gunpla_news(
        self, event: AstrMessageEvent, keyword: str = "", source: str = ""
    ) -> str:
        """查询拼装模型新品/预购资讯（价格、发售时间、结单日、图片、详情页）。

        当用户问「最近有什么新模型/高达」「XX 预购多少钱、什么时候发售」时调用本工具。
        寿屋台湾只卖寿屋自家/IP 比例模型；万代高达相关新品请用 source=dalong。

        Args:
            keyword(string): 可选的中文关键词过滤，如「赛马娘」「独角兽」；留空表示最新全部
            source(string): 可选数据源 all / koto（寿屋预购）/ dalong（万代&寿屋全品类新品），留空用默认
        """
        chosen = (source or self.default_news_source).lower()
        if chosen not in ("all", "koto", "dalong"):
            chosen = self.default_news_source
        args = ["news", "--limit", str(self.news_limit)]
        if chosen != "all":
            args += ["--source", chosen]
        if keyword:
            args += ["--keyword", keyword]
        logger.info(f"[gunpla] tool=gunpla_news keyword={keyword!r} source={chosen}")
        return await self._run_cli(*args)

    @filter.llm_tool(name="gunpla_manual_by_dalong_id")
    async def gunpla_manual_by_dalong_id(
        self, event: AstrMessageEvent, dalong_id: str
    ) -> str:
        """已知 Dalong 编号（或 Dalong 链接）时，直接给出该条目的三语说明书与评测页链接。

        用户发来 dalong.net 链接、或明确说「Dalong 编号是 mdv271」时用这个工具，跳过搜索。

        Args:
            dalong_id(string): Dalong 编号或链接，例如 mdv271 或 https://www.dalong.net/reviews/kotobukiya/mdv271/mdv271_i_e.htm
        """
        logger.info(f"[gunpla] tool=gunpla_manual_by_dalong_id id={dalong_id!r}")
        return await self._run_cli("manual", "--folder", dalong_id)

    # ------------------------------------------------------------------
    # 指令（不经过 LLM，方便直接冒烟测试）
    # ------------------------------------------------------------------

    @filter.command("gunpla")
    async def gunpla_command(self, event: AstrMessageEvent) -> MessageEventResult:
        """直接查说明书：/gunpla 雪兔 或 /gunpla mdv271"""
        if not self.enable_commands:
            yield event.plain_result("该指令已在插件配置中关闭。")
            return
        keyword = self._strip_prefix(event.message_str, "/gunpla")
        if not keyword:
            yield event.plain_result(
                "用法：/gunpla <模型名或 Dalong 编号>\n例：/gunpla 雪兔　/gunpla mdv271"
            )
            return
        logger.info(f"[gunpla] command=/gunpla keyword={keyword!r}")
        text = await self._run_cli("manual", keyword, "--limit", str(self.max_results))
        yield event.plain_result(text)

    @filter.command("gunpla_news")
    async def gunpla_news_command(self, event: AstrMessageEvent) -> MessageEventResult:
        """直接看新品：/gunpla_news [关键词]"""
        if not self.enable_commands:
            yield event.plain_result("该指令已在插件配置中关闭。")
            return
        keyword = self._strip_prefix(event.message_str, "/gunpla_news")
        args = ["news", "--limit", str(self.news_limit)]
        if self.default_news_source != "all":
            args += ["--source", self.default_news_source]
        if keyword:
            args += ["--keyword", keyword]
        text = await self._run_cli(*args)
        yield event.plain_result(text)

    @staticmethod
    def _strip_prefix(message: str, command: str) -> str:
        text = (message or "").strip()
        for prefix in (command, command.lstrip("/")):
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return text.strip()

    async def terminate(self) -> None:
        """插件卸载/停用时的清理（当前无需释放资源）。"""
        logger.info("[gunpla] plugin terminated")
