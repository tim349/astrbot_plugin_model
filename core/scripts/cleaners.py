"""数据清洗与自然语言组装。

这里全是纯函数：输入接口原始 JSON，输出结构化的干净数据 + 中文自然语言文本，
方便单测，也方便模型在拿到 JSON 后自己改写成别的口吻。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Iterable

from i18n_zh import zh_name

SKILL_DIR = Path(__file__).resolve().parent.parent

# Dalong 的 category 字段含义（实测：CG 是 Club G / PB 限定，ETC 是杂项）
CATEGORY_ZH = {
    "HG": "高达 HG",
    "MG": "高达 MG",
    "RG": "高达 RG",
    "SD": "SD",
    "SD2": "SD（其他系列）",
    "PG": "高达 PG",
    "CG": "PB 限定（Club G）",
    "ETC": "其他",
    "KOTOBUKIYA": "寿屋",
}

DALONG_CN = {"ja": "日文", "ko": "韩文", "en": "英文"}


# --------------------------------------------------------------------------
# 基础格式化
# --------------------------------------------------------------------------

def money(n: Any, prefix: str = "NT$") -> str | None:
    """5145 -> 'NT$5,145'；已是字符串（如 '¥3,500'）则原样返回。"""
    if n is None or n == "":
        return None
    if isinstance(n, str):
        return n.strip() or None
    try:
        value = int(round(float(n)))
    except (TypeError, ValueError):
        return None
    return f"{prefix}{value:,}"


def absolute_url(url: Any) -> str | None:
    """寿屋图片是 //cdn... 开头的协议相对地址，补成 https。"""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def parse_built(built: Any) -> _dt.date | None:
    """Dalong 的 built 形如 '261029_153224'（YYMMDD_HHMMSS）。"""
    if not built:
        return None
    match = re.match(r"(\d{2})(\d{2})(\d{2})_", str(built))
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return _dt.date(2000 + year, month, day)
    except ValueError:
        return None


def normalize_reg_date(reg_date: Any, built: Any) -> str | None:
    """'10/30' + built '261029_...' -> '2026-10-30'。

    跨年规则：列表里 12 月条目在 1 月的新品页出现时，年份要回退一年。
    """
    if not reg_date:
        return None
    match = re.match(r"\s*(\d{1,2})/(\d{1,2})\s*$", str(reg_date))
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    anchor = parse_built(built) or _dt.date.today()
    year = anchor.year
    if month - anchor.month >= 7:  # 明显是上一年留下的记录
        year -= 1
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_release(release: Any) -> str | None:
    """'2026.9' / '2026/09' / '2026年9月' -> '2026-09'。"""
    if not release:
        return None
    match = re.search(r"(\d{4})\D{1,2}(\d{1,2})", str(release))
    if not match:
        return str(release).strip() or None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return str(release).strip() or None
    return f"{year:04d}-{month:02d}"


def normalize_deadline(brief: Any) -> str | None:
    """'2026/12/3結單' -> '2026-12-03'。

    注意：寿屋的 brief 有时是「结单日 + 整段商品介绍」，只认第一行开头的日期，
    否则会把介绍文字里的日期误当结单日。
    """
    if not brief:
        return None
    first_line = str(brief).strip().splitlines()[0].strip() if str(brief).strip() else ""
    match = re.match(r"\s*(\d{4})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})", first_line)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def brief_summary(brief: Any, limit: int = 220) -> str | None:
    """brief 里结单日之后的那段商品介绍（去掉首行日期），截断到 limit 字。"""
    if not brief:
        return None
    lines = [line.strip() for line in str(brief).splitlines() if line.strip()]
    if lines and re.fullmatch(r"\d{4}\D{1,2}\d{1,2}\D{1,2}\d{1,2}.*(?:結單|结单|截止)?", lines[0]):
        lines = lines[1:]
    text = " ".join(lines).strip()
    if not text:
        return None
    return text[:limit] + ("…" if len(text) > limit else "")


def release_from_slogan(slogan: Any) -> str | None:
    """'日本2027年6月發售預購品' -> '2027-06'。"""
    return normalize_release(slogan)


# --------------------------------------------------------------------------
# 繁简与匹配（只用于搜索，不用于输出）
# --------------------------------------------------------------------------

_T2S_PATH = SKILL_DIR / "data" / "t2s.json"
_T2S: dict[str, str] | None = None


def _t2s() -> dict[str, str]:
    global _T2S
    if _T2S is None:
        try:
            with _T2S_PATH.open("r", encoding="utf-8") as fh:
                _T2S = {str(k): str(v) for k, v in json.load(fh).items()}
        except (OSError, ValueError):
            _T2S = {}
    return _T2S


def to_simplified(text: Any) -> str:
    """逐字繁转简（只覆盖常用字，用于关键词匹配）。"""
    table = _t2s()
    return "".join(table.get(ch, ch) for ch in str(text or ""))


# 两岸三地叫法不同：用户搜「高达」，寿屋台湾写「鋼彈」。匹配时统一成一种写法。
_REGIONAL = {"高达": "钢弹", "敢达": "钢弹", "鋼彈": "钢弹"}


def _norm(text: Any) -> str:
    out = to_simplified(text).casefold()
    for alias, canonical in _REGIONAL.items():
        out = out.replace(alias, canonical)
    return out


def _tokens(text: Any) -> list[str]:
    """把关键词切成有意义的片段：去掉比例、括号标点，保留词元。"""
    cleaned = _norm(text)
    cleaned = re.sub(r"\d+/\d+", " ", cleaned)          # 1/144、1/100 这类比例
    cleaned = re.sub(r"[()\[\]{}【】「」·・,，。./\\|+_\-—~!！?？:：;；'\"]+", " ", cleaned)
    return [tok for tok in cleaned.split() if tok]


def match_score(keyword: str, *fields: Any) -> int:
    """粗略相关度：整串命中 > 全部词命中 > 部分词命中。"""
    needle = _norm(keyword).strip()
    if not needle:
        return 0
    haystack = " ".join(_norm(f) for f in fields if f)
    if not haystack:
        return 0
    if needle in haystack:
        return 100
    # 单字词（如「투」「兔」）太容易误命中，只认整串匹配
    tokens = [tok for tok in _tokens(keyword) if len(tok) >= 2]
    if not tokens:
        return 0
    hits = sum(1 for tok in tokens if tok in haystack)
    if hits == len(tokens):
        return 60 + min(hits, 20)
    if hits:
        return int(30 * hits / len(tokens))
    return 0


# --------------------------------------------------------------------------
# 寿屋预购
# --------------------------------------------------------------------------

def koto_item(product: dict[str, Any]) -> dict[str, Any]:
    """把一条寿屋商品清洗成字段固定的结构。"""
    images = product.get("photo_urls") or []
    photo = None
    if images:
        photo = absolute_url((images[0] or {}).get("large"))
    image = absolute_url((product.get("featured_image") or {}).get("grande")) or photo
    handle = product.get("handle") or ""
    price_raw = product.get("cheapest_variant_price")
    if price_raw is None:
        price_raw = product.get("first_variant_price")
    return {
        "source": "kotobukiya",
        "id": product.get("id"),
        "handle": handle,
        "title": (product.get("title") or "").strip(),
        "slogan": (product.get("slogan") or "").strip(),
        "brief": (product.get("brief") or "").strip(),
        "summary": brief_summary(product.get("brief")),
        "sku": product.get("cheapest_variant_sku") or product.get("first_variant_sku"),
        "price": money(price_raw),
        "price_raw": price_raw,
        "in_stock": product.get("in_stock"),
        "purchasable": bool(product.get("sales_mode_purchasable")),
        "release": release_from_slogan(product.get("slogan")),
        "deadline": normalize_deadline(product.get("brief")),
        "image": image,
        "photos": [absolute_url((img or {}).get("large")) for img in images][:8],
        "detail_url": f"https://www.kotobukiya.com.tw/products/{handle}" if handle else None,
    }


def render_koto(item: dict[str, Any], *, index: int | None = None) -> str:
    """按 test.txt 里的样例拼成中文自然语言。"""
    head = f"【寿屋预购 {index}】" if index is not None else "【寿屋预购】"
    slogan = item.get("slogan") or ""
    title = item.get("title") or ""
    price = item.get("price") or "价格未知"

    line1 = f"{head}{slogan + '：' if slogan else ''}{title}，售价 {price}"
    if item.get("deadline"):
        line1 += f"（{item['deadline'].replace('-', '/')} 结单）"
    if item.get("sku"):
        line1 += f"，SKU {item['sku']}"

    lines = [line1]
    if item.get("image"):
        lines.append(f"图片：{item['image']}")
    if item.get("detail_url"):
        lines.append(f"详情：{item['detail_url']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Dalong 资讯 / 说明书
# --------------------------------------------------------------------------

def dalong_item(row: dict[str, Any], built: Any = None, *, allow_mt: bool = False) -> dict[str, Any]:
    """清洗一条 Dalong 结果（最新资讯与搜索结果的字段大部分相同）。"""
    from fetchers import dalong_manual_links  # 局部导入避免循环

    links = dalong_manual_links(row)
    jp = (row.get("japanese_name") or "").strip()
    en = (row.get("english_name") or "").strip()
    ko = (row.get("korean_name") or "").strip()

    zh = zh_name(jp, "ja", allow_mt=allow_mt) or zh_name(en, "en", allow_mt=allow_mt)
    if zh:
        zh["text"] = to_simplified(zh["text"])  # 参考名统一用简体
    category = (row.get("category") or "").upper()
    category_zh = CATEGORY_ZH.get(category, category)
    grade = (row.get("grade") or row.get("grade_disp") or "").strip()
    code_disp = (row.get("code_disp") or row.get("code") or "").strip()

    # 寿屋的 grade 恒为 Kotobukiya，显示出来只是噪音；同理去掉与分类重复的系列名
    grade_display = "" if grade.casefold() in {"kotobukiya", category.casefold()} else grade
    # code_disp 里像「プレバン」「Gundam Base」这种渠道名没有编号，不必挤在标题里
    code_display = code_disp if re.search(r"\d", code_disp) else ""

    return {
        "source": "dalong",
        "folder_name": row.get("folder_name"),
        "category": category,
        "category_zh": category_zh,
        "grade": grade,
        "grade_display": grade_display,
        "code": row.get("code") or "",
        "code_disp": code_disp,
        "code_display": code_display,
        "japanese_name": jp,
        "english_name": en,
        "korean_name": ko,
        "zh_name": zh["text"] if zh else None,
        "zh_source": zh["source"] if zh else None,
        "reg_date": normalize_reg_date(row.get("reg_date"), built),
        "release": normalize_release(row.get("release_date")),
        "price": (row.get("price") or "").strip() or None,
        "is_combined": row.get("is_combined"),
        **links,
    }


def _zh_line(item: dict[str, Any]) -> str | None:
    if not item.get("zh_name"):
        return None
    tag = {
        "glossary": "【术语表】",
        "glossary-partial": "【部分术语，仅供参考】",
        "mt": "【机翻，仅供参考】",
    }.get(item.get("zh_source") or "", "【参考】")
    return f"中文名：{item['zh_name']}{tag}"


def _manual_line(item: dict[str, Any]) -> str | None:
    parts = [
        f"{DALONG_CN[lang]} {item[key]}"
        for lang, key in (("ja", "manual_ja"), ("ko", "manual_ko"), ("en", "manual_en"))
        if item.get(key)
    ]
    return f"说明书：{'｜'.join(parts)}" if parts else None


def _review_line(item: dict[str, Any]) -> str | None:
    parts = [
        f"{DALONG_CN[lang]} {item[key]}"
        for lang, key in (("ja", "review_ja"), ("ko", "review_ko"), ("en", "review_en"))
        if item.get(key)
    ]
    return f"评测页：{'｜'.join(parts)}" if parts else None


def render_dalong(item: dict[str, Any], *, index: int | None = None, with_manual: bool = True) -> str:
    head = f"【Dalong 新品 {index}】" if index is not None else "【Dalong 新品】"
    if item.get("reg_date"):
        head += f"{item['reg_date']}｜"
    head += item.get("category_zh") or item.get("category") or ""
    if item.get("grade_display"):
        head += f"／{item['grade_display']}"
    if item.get("code_display"):
        head += f"（{item['code_display']}）"

    lines = [head.rstrip("｜")]
    if item.get("japanese_name"):
        lines.append(f"日文名：{item['japanese_name']}")
    if item.get("english_name"):
        lines.append(f"英文名：{item['english_name']}")
    if item.get("korean_name"):
        lines.append(f"韩文名：{item['korean_name']}")
    zh_line = _zh_line(item)
    if zh_line:
        lines.append(zh_line)
    if item.get("image"):
        lines.append(f"图片：{item['image']}")
    if with_manual:
        manual = _manual_line(item)
        if manual:
            lines.append(manual)
        review = _review_line(item)
        if review:
            lines.append(review)
    return "\n".join(lines)


def render_manual(item: dict[str, Any], *, index: int | None = None) -> str:
    """说明书搜索结果的自然语言块（带价格与发售时间）。"""
    head = f"【说明书 {index}】" if index is not None else "【说明书】"

    primary = item.get("japanese_name") or item.get("english_name") or ""
    head += primary

    aliases = []
    if item.get("english_name") and item.get("english_name") != primary:
        aliases.append(item["english_name"])
    if item.get("korean_name"):
        aliases.append(item["korean_name"])
    if aliases:
        head += "（" + " / ".join(aliases) + "）"

    meta = []
    if item.get("category_zh"):
        meta.append(f"分类：{item['category_zh']}")
    if item.get("grade_display"):
        meta.append(f"系列：{item['grade_display']}")
    if item.get("release"):
        meta.append(f"发售：{item['release']}")
    if item.get("price"):
        meta.append(f"定价：{item['price']}")

    lines = [head]
    if meta:
        lines.append("｜".join(meta))
    zh_line = _zh_line(item)
    if zh_line:
        lines.append(zh_line)
    if item.get("image"):
        lines.append(f"图片：{item['image']}")
    manual = _manual_line(item)
    if manual:
        lines.append(manual)
    review = _review_line(item)
    if review:
        lines.append(review)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 排序 / 去重 / 关键词过滤
# --------------------------------------------------------------------------

def dedupe(items: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        value = item.get(key)
        if value in seen:
            continue
        seen.add(value)
        out.append(item)
    return out


def render_manual_ref(folder: str, links: dict[str, str], *, index: int | None = None) -> str:
    """已知 folder（用户直接给的 Dalong 链接/编号）时，只输出可信链接，不猜名称。"""
    head = f"【说明书 {index}】Dalong 条目 {folder}" if index is not None else f"【说明书】Dalong 条目 {folder}"
    lines = [head]
    if links.get("image"):
        lines.append(f"图片：{links['image']}")
    manual = _manual_line(links)
    if manual:
        lines.append(manual)
    review = _review_line(links)
    if review:
        lines.append(review)
    return "\n".join(lines)


def filter_dalong(items: Iterable[dict[str, Any]], keyword: str | None) -> list[dict[str, Any]]:
    """在 Dalong 条目上做关键词过滤，中文关键词会先用术语表扩成日/英/韩写法。"""
    items = list(items)
    if not keyword:
        return items
    variants = [keyword]
    for lang in ("ja", "en", "ko"):
        variants.extend(glossary_terms(keyword, lang))
    token_sets = [tokens for tokens in (_tokens(v) for v in variants) if tokens]
    if not token_sets:
        return items
    out = []
    for item in items:
        haystack = _norm(
            " ".join(
                str(item.get(key) or "")
                for key in ("japanese_name", "english_name", "korean_name", "grade", "code_disp", "category_zh")
            )
        )
        if any(all(tok in haystack for tok in tokens) for tokens in token_sets):
            out.append(item)
    return out


def glossary_terms(keyword: str, lang: str) -> list[str]:
    """术语表里的中文→目标语言写法（延迟导入，避免循环依赖）。"""
    from i18n_zh import glossary

    return glossary().zh_to_terms(keyword, lang)


def filter_koto(items: Iterable[dict[str, Any]], keyword: str | None) -> list[dict[str, Any]]:
    """在已清洗的寿屋条目上做本地关键词过滤（支持繁体/简体混搜）。"""
    items = list(items)
    if not keyword:
        return items
    tokens = _tokens(keyword)
    if not tokens:
        return items
    out = []
    for item in items:
        # 只用标题/发售语/SKU/编号匹配：brief 可能是整段介绍，会误命中
        haystack = _norm(" ".join(str(item.get(k) or "") for k in ("title", "slogan", "sku", "handle")))
        if all(tok in haystack for tok in tokens):
            out.append(item)
    return out


def rank_manual(rows: Iterable[dict[str, Any]], keywords: Iterable[str]) -> list[tuple[int, dict[str, Any]]]:
    """按与关键词的匹配度排序；同分时更短的名称（通常是本体而非衍生款）优先。"""
    keywords = [k for k in keywords if k]
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        fields = (
            row.get("japanese_name"),
            row.get("english_name"),
            row.get("korean_name"),
            row.get("folder_name"),
            row.get("code_disp"),
            row.get("code"),
            row.get("grade"),
        )
        score = max((match_score(k, *fields) for k in keywords), default=0)
        names = [row.get("japanese_name"), row.get("english_name"), row.get("korean_name")]
        shortest = min((len(_norm(n).replace(" ", "")) for n in names if n), default=99)
        scored.append((score, -shortest, row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(score, row) for score, _short, row in scored]


def group_manual(
    rows: Iterable[dict[str, Any]],
    keywords: Iterable[str],
    limit: int = 10,
    *,
    allow_mt: bool = False,
    min_score: int = 60,
) -> list[dict[str, Any]]:
    """合并去重 + 排序 + 截断，返回干净条目。

    只保留相关度 ≥ min_score 的结果：Dalong 的搜索很宽松，模糊拼词能返回一堆
    无关机体（例如「bar」命中 Barbatos），宁可不给结果也不要给错的。
    """
    keywords = list(keywords)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        folder = row.get("folder_name") or ""
        if not folder or folder in seen:
            continue
        seen.add(folder)
        unique.append(row)

    ranked = rank_manual(unique, keywords)
    out: list[dict[str, Any]] = []
    for score, row in ranked:
        if score < min_score:
            continue
        item = dalong_item(row, allow_mt=allow_mt)
        item["score"] = score
        out.append(item)
        if len(out) >= limit:
            break
    return out
