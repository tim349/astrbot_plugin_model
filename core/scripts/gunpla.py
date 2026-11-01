#!/usr/bin/env python3
"""拼装模型资讯 / 说明书查询脚本（纯标准库）。

用法示例：
    python scripts/gunpla.py news --source all --limit 8
    python scripts/gunpla.py news --source koto --keyword 赛马娘
    python scripts/gunpla.py manual "刹帝利" --limit 3
    python scripts/gunpla.py manual "雪初音" --lang auto --json
    python scripts/gunpla.py zh "フレームアーティスト 雪ミク" --mt
    python scripts/gunpla.py doctor
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any, Iterable

import cleaners as cl
import fetchers as ft
import i18n_zh as zh

LANGS = ("ja", "en", "ko")
LANG_LABEL = {"ja": "日文", "en": "英文", "ko": "韩文"}

# 硬性时限：查不到就立刻收手，统一回复这句，避免继续试错烧 token
NOT_FOUND = "没有找到对应的内容，可能网站还没有搜录，或者更换描述后重试"
DEFAULT_BUDGET = 30.0


def _setup_stdout() -> None:
    """Windows 控制台/重定向默认 GBK，统一成 UTF-8，避免中文报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _err(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)


# --------------------------------------------------------------------------
# news
# --------------------------------------------------------------------------

def collect_koto(keyword: str | None, *, collection: str, pages: int, per: int) -> list[dict[str, Any]]:
    max_pages = pages if pages > 0 else 100
    raw: list[dict[str, Any]] = []
    for product in ft.koto_iter_products(collection=collection, per=per, max_pages=max_pages):
        raw.append(cl.koto_item(product))
    return cl.filter_koto(raw, keyword)


def collect_dalong(keyword: str | None, *, allow_mt: bool = False) -> list[dict[str, Any]]:
    payload = ft.dalong_latest()
    built = payload.get("built")
    items = [cl.dalong_item(row, built, allow_mt=allow_mt) for row in payload.get("rows") or []]
    return cl.filter_dalong(items, keyword)


def cmd_news(args: argparse.Namespace) -> int:
    sections: list[tuple[str, list[dict[str, Any]], Any]] = []

    if args.source in ("all", "koto"):
        try:
            items = collect_koto(args.keyword, collection=args.collection, pages=args.pages, per=args.per)
            sections.append(("寿屋台湾预购", items, cl.render_koto))
        except ft.HttpError as exc:
            _err(f"寿屋接口不可用：{exc}")
    if args.source in ("all", "dalong"):
        try:
            items = collect_dalong(args.keyword, allow_mt=args.mt)
            sections.append(("Dalong 最新收录", items, cl.render_dalong))
        except ft.HttpError as exc:
            _err(f"Dalong 接口不可用：{exc}")

    if not sections:
        return 2

    payload: dict[str, Any] = {"query": {"keyword": args.keyword, "source": args.source}}
    for name, items, _render in sections:
        items = items[: args.limit]
        payload["kotobukiya" if name.startswith("寿屋") else "dalong"] = items

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    total = 0
    for name, items, render in sections:
        if not items:
            continue
        print(f"## {name}（共 {len(items)} 条，展示前 {min(len(items), args.limit)} 条）")
        for index, item in enumerate(items[: args.limit], start=1):
            print(render(item, index=index))
            print()
        total += len(items)
    if total == 0:
        print(NOT_FOUND)
    return 0


# --------------------------------------------------------------------------
# manual
# --------------------------------------------------------------------------

def _search_variants(keyword: str, lang: str, *, remaining: float = 30.0, cap: int = 3) -> list[str]:
    # 时间紧就不做机翻，只走术语表 + 原词
    variants = zh.query_variants(
        keyword,
        lang,
        allow_mt=remaining > 5,
        mt_timeout=max(3.0, min(remaining, 8.0)),
    )
    return variants[:cap]


def search_manuals(
    keyword: str,
    *,
    langs: Iterable[str] = LANGS,
    limit: int = 10,
    per_lang: int = 20,
    verify: bool = False,
    allow_mt: bool = False,
    budget: float = 30.0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """多语言搜同一关键词，合并去重后按相关度排序。

    budget 是整次查询的时间预算（秒）：到点立刻收手，避免把时间耗在反复试探上。
    """
    deadline = time.monotonic() + max(budget, 1.0)
    rows: list[dict[str, Any]] = []
    used_keywords: list[str] = [keyword]
    errors: list[str] = []

    def remaining() -> float:
        return deadline - time.monotonic()

    def search(variant: str, lang: str) -> list[dict[str, Any]]:
        if remaining() < 3:
            return []
        timeout = max(3.0, min(remaining(), 15.0))
        payload = ft.dalong_search(variant, lang=lang, limit=per_lang, timeout=timeout)
        return payload.get("results") or []

    for lang in langs:
        for variant in _search_variants(keyword, lang, remaining=remaining()):
            if remaining() < 3:
                break
            used_keywords.append(variant)
            try:
                results = search(variant, lang)
            except ft.HttpError as exc:
                errors.append(f"{LANG_LABEL.get(lang, lang)}搜索失败：{exc}")
                continue
            rows.extend(results)
            if results:
                break  # 该语言已命中，不必再试兜底变体

    # 仍然为空：把关键词拆词再搜一轮（例如「1/144 刹帝利」里的比例前缀）
    # 只丢「足够长」的词；拆词结果不加入 used_keywords，否则单 token 会把
    # 「PUNI」和「punisher」都算成满分，返回一堆无关机体
    if not rows:
        tokens = [
            tok
            for tok in re.split(r"[\s/、,，]+", keyword)
            if (len(tok) >= 4) or (len(tok) >= 2 and re.search(r"[\u4e00-\u9fff]", tok))
        ]
        for token in tokens[:2]:
            if remaining() < 3:
                break
            for lang in langs:
                try:
                    got = search(token, lang)
                except ft.HttpError:
                    continue
                if got:
                    rows.extend(got)
                    break

    items = cl.group_manual(rows, used_keywords, limit=limit, allow_mt=allow_mt)
    if verify:
        for item in items:
            for key in ("manual_ja", "manual_ko", "manual_en", "review_ja", "review_ko", "review_en"):
                if item.get(key) and remaining() >= 3:
                    item[key + "_ok"] = ft.verify_url(item[key], timeout=min(remaining(), 10))
    return items, errors


def cmd_manual(args: argparse.Namespace) -> int:
    keyword = " ".join(args.keyword).strip() if args.keyword else ""
    deadline = time.monotonic() + max(args.budget, 1.0)

    # 用户直接给了 Dalong 链接或编号（mdv26）时，不需要搜索，按命名规则给三语链接
    ref = ft.parse_dalong_ref(args.folder or keyword)
    if ref:
        directory, folder = ref
        links = ft.resolve_dalong_folder(folder, directory, timeout=max(3.0, min(deadline - time.monotonic(), 8.0)))
        if not links:
            print(NOT_FOUND)
            return 0
        if args.json:
            print(json.dumps({"query": keyword or folder, "folder": folder, "links": links}, ensure_ascii=False, indent=2))
            return 0
        print(cl.render_manual_ref(folder, links))
        return 0

    if not keyword:
        _err("请给一个关键词，或用 --folder 指定 Dalong 编号（如 mdv26）。")
        return 2

    langs = LANGS if args.lang == "auto" else (args.lang,)
    try:
        items, errors = search_manuals(
            keyword,
            langs=langs,
            limit=args.limit,
            verify=args.verify,
            allow_mt=args.mt,
            budget=max(deadline - time.monotonic(), 1.0),
        )
    except ft.HttpError as exc:
        _err(f"Dalong 接口不可用：{exc}")
        print(NOT_FOUND)
        return 0

    for message in errors:
        _err(message)

    if args.json:
        print(json.dumps({"query": keyword, "items": items}, ensure_ascii=False, indent=2))
        return 0

    if not items:
        print(NOT_FOUND)
        return 0

    print(f"## 说明书搜索结果：{keyword}（{len(items)} 条）")
    for index, item in enumerate(items, start=1):
        print(cl.render_manual(item, index=index))
        if args.verify:
            marks = []
            for key, label in (("manual_ja_ok", "日文"), ("manual_ko_ok", "韩文"), ("manual_en_ok", "英文")):
                if key in item:
                    marks.append(f"{label}{'可访问' if item[key] else '打不开'}")
            if marks:
                print("链接校验：" + "｜".join(marks))
        print()
    return 0


# --------------------------------------------------------------------------
# zh
# --------------------------------------------------------------------------

def cmd_zh(args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    result = zh.zh_name(text, args.lang, allow_mt=args.mt)
    if args.json:
        print(json.dumps({"input": text, "lang": args.lang, "result": result}, ensure_ascii=False, indent=2))
        return 0
    if not result:
        print(f"「{text}」没有可用的中文参考名（术语表未覆盖，机翻不可用）。")
        return 1
    tag = {
        "glossary": "术语表",
        "glossary-partial": "部分术语替换，仅供参考",
        "mt": "机翻，仅供参考",
    }.get(result["source"], result["source"])
    print(f"{text} → {result['text']}（{tag}）")
    return 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, str]] = [
        ("寿屋预购接口", ft.koto_collection_url(page=1, per=1)),
        ("Dalong 最新资讯", ft.dalong_latest_url(timestamp=1)),
        ("Dalong 说明书搜索", ft.dalong_search_url("ガンダム", lang="ja", limit=1)),
    ]
    ok = True
    for label, url in checks:
        try:
            ft.fetch_json(url, retries=1, ttl=0)
            print(f"[OK]   {label}")
        except ft.HttpError as exc:
            ok = False
            print(f"[FAIL] {label}：{exc}")
    try:
        sample = zh.free_translate("ガンダム", "ja")
        print(f"[OK]   免费机翻接口（示例：{sample or '无返回'}）")
    except Exception as exc:  # noqa: BLE001 - 诊断命令不因单点失败中断
        print(f"[WARN] 免费机翻接口异常：{exc}")
    print(f"缓存目录：{ft.cache_dir()}")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gunpla.py",
        description="拼装模型资讯与说明书查询（寿屋台湾 + Dalong）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    news = sub.add_parser("news", help="新品 / 预购资讯")
    news.add_argument("--source", choices=("all", "koto", "dalong"), default="all")
    news.add_argument("--collection", choices=("pre-order", "all"), default="pre-order",
                      help="寿屋集合：pre-order 预购（默认）或 all 全部商品")
    news.add_argument("--keyword", help="本地关键词过滤，支持简体中文（如 赛马娘、高达）")
    news.add_argument("--limit", type=int, default=10, help="每个来源最多输出多少条")
    news.add_argument("--pages", type=int, default=0, help="寿屋翻页上限，0 表示翻完（默认）")
    news.add_argument("--per", type=int, default=24, help="寿屋每页数量，默认 24")
    news.add_argument("--json", action="store_true", help="输出结构化 JSON")
    news.add_argument("--mt", action="store_true", help="中文名补充时允许调用免费机翻（质量有限）")
    news.set_defaults(func=cmd_news)

    manual = sub.add_parser("manual", help="按关键词搜说明书")
    manual.add_argument("keyword", nargs="*", help="关键词：中文/日文/英文/Dalong 链接或编号都可以")
    manual.add_argument("--folder", help="直接指定 Dalong 编号或链接（如 mdv26），跳过搜索")
    manual.add_argument("--lang", choices=("auto", "ja", "en", "ko"), default="auto",
                        help="搜索语言，auto 表示三种语言都试")
    manual.add_argument("--limit", type=int, default=10)
    manual.add_argument("--verify", action="store_true", help="校验说明书链接是否可访问（较慢）")
    manual.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                        help="整次查询的时间预算（秒），超时即收手并回复「没有找到…」，默认 30")
    manual.add_argument("--json", action="store_true")
    manual.add_argument("--mt", action="store_true", help="中文名补充时允许调用免费机翻（质量有限）")
    manual.set_defaults(func=cmd_manual)

    zh_cmd = sub.add_parser("zh", help="补全中文参考名")
    zh_cmd.add_argument("text", nargs="+")
    zh_cmd.add_argument("--lang", choices=LANGS, default="ja")
    zh_cmd.add_argument("--mt", action="store_true", help="术语表不可靠时调用免费机翻")
    zh_cmd.add_argument("--json", action="store_true")
    zh_cmd.set_defaults(func=cmd_zh)

    doctor = sub.add_parser("doctor", help="检查接口连通性")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
