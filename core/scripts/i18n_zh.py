"""中文名称补全：术语表优先，免费翻译接口兜底。

为什么不直接用免费接口？Dalong 只有日/英/韩三语名，机翻对模型机体名
（片假名专名）几乎全错，例如 ディザード 会被译成「抖动」。
所以这里的策略是：

1. 先用 data/glossary.json 里的模型/机体/系列术语做最长匹配替换（可解释、可维护）；
2. 术语覆盖率不够时，可选地调用 MyMemory 免费接口做机翻，并标记 [机翻]；
3. 两样都不可靠时返回 None，交给上层（模型/用户）决定，绝不硬编一个假中文名。

注意：机翻结果只能当「参考」，最终输出必须由模型用通行译名（如 刹帝利、
风灵高达、骨装机兵）校正，不能把机翻当官方名称。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
GLOSSARY_PATH = SKILL_DIR / "data" / "glossary.json"

MYMEMORY_URL = "https://api.mymemory.translated.net/get"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36"

# 术语表里用这些语言键登记，顺序即匹配优先级
SOURCE_LANGS = ("ja", "en", "ko")


class Glossary:
    def __init__(self, entries: list[dict[str, Any]], aliases: dict[str, str] | None = None) -> None:
        # 术语条目形如 {"zh": "高达", "ja": ["ガンダム"], "en": ["Gundam"], "ko": ["건담"]}
        self.entries = entries
        self.aliases = aliases or {}
        self.by_lang: dict[str, list[tuple[str, str]]] = {lang: [] for lang in SOURCE_LANGS}
        for entry in entries:
            zh = entry["zh"]
            for lang in SOURCE_LANGS:
                for term in entry.get(lang) or []:
                    if term:
                        self.by_lang[lang].append((term, zh))
        # 长词优先，避免「ガンダムベース」被「ガンダム」先吃掉
        for lang in SOURCE_LANGS:
            self.by_lang[lang].sort(key=lambda pair: len(pair[0]), reverse=True)
        self._zh_to_multi = self._build_reverse()

    def _build_reverse(self) -> list[tuple[str, dict[str, str]]]:
        """中文 → 各语言写法，用于把中文关键词扩展成日/英/韩查询词。"""
        table: list[tuple[str, dict[str, str]]] = []
        for entry in self.entries:
            zh = entry["zh"]
            mapping = {lang: (entry.get(lang) or [""])[0] for lang in SOURCE_LANGS}
            table.append((zh, mapping))
        table.sort(key=lambda item: len(item[0]), reverse=True)
        return table

    def match(self, text: str | None, lang: str = "ja") -> tuple[str, float]:
        """对文本做最长匹配替换，返回 (替换后文本, 命中字符占比)。

        占比 = 被术语覆盖的字符数 / 原文非空白字符数。
        """
        if not text:
            return "", 0.0
        text = self.aliases.get(text, text) if text in self.aliases else text
        pairs = self.by_lang.get(lang, [])
        if not pairs:
            return text, 0.0
        pattern = re.compile("|".join(re.escape(term) for term, _ in pairs))
        lookup: dict[str, str] = {}
        for term, zh in pairs:
            lookup.setdefault(term, zh)  # 同一写法多个中文名时，取先登记的那个
        covered = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal covered
            covered += len(match.group(0))
            return lookup[match.group(0)]

        result = pattern.sub(replace, text)
        total = len(re.sub(r"\s+", "", text))
        ratio = covered / total if total else 0.0
        return result, ratio

    def zh_to_terms(self, text: str, lang: str) -> list[str]:
        """把一段中文扩成某语言的查询关键词（逐术语替换 + 整段替换）。"""
        if not text:
            return []
        out: list[str] = []
        replaced = text
        for zh, mapping in self._zh_to_multi:
            if zh and zh in replaced:
                term = mapping.get(lang) or ""
                if term:
                    out.append(term)
                replaced = replaced.replace(zh, " ")
        if replaced.strip() == text.strip() and not out:
            return []
        return _uniq(out)


_GLOSSARY: Glossary | None = None


def glossary() -> Glossary:
    global _GLOSSARY
    if _GLOSSARY is None:
        with GLOSSARY_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        _GLOSSARY = Glossary(data.get("entries") or [], data.get("aliases") or {})
    return _GLOSSARY


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


# --------------------------------------------------------------------------
# 免费翻译（MyMemory，匿名可用；超限会返回配额提示）
# --------------------------------------------------------------------------

def _mt_cache_path() -> Path:
    env = os.environ.get("GUNPLA_CACHE_DIR")
    base = Path(env).expanduser() if env else Path.home() / ".cache" / "gunpla-skill"
    return base / "mt.json"


def _mt_cache_load() -> dict[str, str]:
    try:
        with _mt_cache_path().open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _mt_cache_save(cache: dict[str, str]) -> None:
    path = _mt_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False)
        tmp.replace(path)
    except OSError:
        pass


def free_translate(text: str, source: str, target: str = "zh-CN", *, timeout: float = 20.0) -> str | None:
    """MyMemory 免费机翻。失败/超限返回 None，绝不抛异常打断主流程。"""
    text = (text or "").strip()
    if not text:
        return None
    cache = _mt_cache_load()
    key = f"{source}|{target}|{text}"
    if key in cache:
        return cache[key]

    query = urllib.parse.urlencode({"q": text, "langpair": f"{source}|{target}"})
    request = urllib.request.Request(f"{MYMEMORY_URL}?{query}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None

    translated = (payload.get("responseData") or {}).get("translatedText") or ""
    translated = translated.strip()
    if not translated or payload.get("quotaFinished"):
        return None
    # MyMemory 有时候会原样返回，视为失败
    if translated.lower() == text.lower():
        return None
    cache[key] = translated
    _mt_cache_save(cache)
    return translated


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def zh_name(text: str | None, lang: str = "ja", *, allow_mt: bool = False) -> dict[str, str] | None:
    """给一个日/英/韩名称产出中文参考名。

    返回 {"text": ..., "source": "glossary"|"glossary-partial"|"mt"}；
    无法可靠得到时返回 None。
    """
    if not text:
        return None
    if lang not in SOURCE_LANGS:
        lang = "ja"

    candidate, ratio = glossary().match(text, lang)
    if ratio >= 0.85:
        return {"text": _tidy(candidate), "source": "glossary"}

    partial = _keep_meaningful(candidate)
    if partial != text and len(_cjk(partial)) >= 2 and partial not in _GENERIC_ONLY:
        return {"text": _tidy(partial), "source": "glossary-partial"}

    if allow_mt:
        translated = free_translate(text, lang, "zh-CN")
        if translated:
            return {"text": _tidy(translated), "source": "mt"}
    return None


def _cjk(text: str) -> str:
    return "".join(re.findall(r"[\u3400-\u9fff]", text))


# 只有这些泛用词命中时，给「中文名」没有意义
_GENERIC_ONLY = {"限定", "特典", "泳装", "竞泳泳装", "替换脸", "高达", "寿屋"}


# 部分命中时，术语之外的英文单词（系列描述等）通常没有信息量，删掉更干净；
# 但型号/比例（HG、MGSD、NJ-666、1/144）要留着。
_GRADE_TOKENS = re.compile(
    r"^(?:HG|HGUC|HGCE|HGAW|HGAC|HGBF|HGBD|MG|MGSD|RG|PG|SD|EG|RE|FM|BC|BB|1/\d+|\d+)$",
    re.IGNORECASE,
)


def _keep_meaningful(text: str) -> str:
    """只留下汉字、型号/比例；丢掉未翻译的假名、韩文和英文修饰语。"""
    # 假名/谚文整段抹掉，避免「菲尼克斯(デストロイモード)GOLD」这种半成品
    stripped = re.sub(r"[\u3040-\u30ff\uac00-\ud7af]+", " ", text)
    kept: list[str] = []
    for token in stripped.split():
        core = token.strip("()（）[]【】,，.。&")
        if not core:
            continue
        if re.search(r"[\u3400-\u9fff]", core):
            kept.append(core)
        elif _GRADE_TOKENS.match(core) or re.match(r"^[A-Za-z]*\d", core):
            kept.append(core)
    return " ".join(kept) if kept else text


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"\s*([（）()\[\]【】])\s*", r"\1", text)
    return text


def query_variants(keyword: str, lang: str, *, allow_mt: bool = True, mt_timeout: float = 8.0) -> list[str]:
    """把用户（可能是中文）的关键词扩成 Dalong 某语言的查询候选词。

    顺序即尝试顺序：原词（如果本来就是该语言）→ 术语表替换 → 机翻。
    allow_mt=False 时跳过机翻（时间预算不够时用）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    variants: list[str] = []
    if lang == "ja" and re.search(r"[\u3040-\u30ff]", keyword):
        variants.append(keyword)
    if lang == "ko" and re.search(r"[\uac00-\ud7af]", keyword):
        variants.append(keyword)
    if lang == "en" and re.fullmatch(r"[\x00-\x7f]+", keyword):
        variants.append(keyword)

    # 本来就是日/英/韩混写的情况，直接也丢给三种语言试一次
    if not variants and re.search(r"[\u3040-\u30ff\uac00-\ud7af]", keyword):
        variants.append(keyword)

    # 术语表能覆盖就不必机翻，省一次请求
    terms = glossary().zh_to_terms(keyword, lang)
    variants.extend(terms)

    if allow_mt and re.search(r"[\u4e00-\u9fff]", keyword) and not terms:
        translated = free_translate(keyword, "zh-CN", {"ja": "ja", "ko": "ko", "en": "en"}[lang], timeout=mt_timeout)
        if translated:
            variants.append(translated)

    variants.append(keyword)  # 万能兜底：说不定对方接口能模糊命中
    return _uniq(variants)
