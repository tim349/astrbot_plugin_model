"""数据源封装：寿屋台湾预购接口 + Dalong 资讯/说明书接口。

只使用 Python 标准库（3.9+），无需安装任何依赖。

设计要点：
- 统一 UA / Referer / 超时 / 重试 / 退避；
- 带 TTL 的磁盘缓存（默认 ~/.cache/gunpla-skill），避免反复请求同一份数据；
- 同一主机的请求之间做最小间隔限速，不要打爆对方站点；
- 所有错误都收敛成 HttpError，方便上层给出可读提示。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_TIMEOUT = 25.0
DEFAULT_RETRIES = 3
MIN_INTERVAL = 0.5  # 同一主机两次请求之间至少间隔的秒数

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

KOTO_BASE = "https://www.kotobukiya.com.tw/zh-TW"
DALONG_BASE = "https://www.dalong.net/reviews"

# 寿屋网站的 Shopify/Cyberbiz 风格集合接口，pre-order 为预购集合
KOTO_COLLECTIONS = {
    "pre-order": "pre-order",
    "all": "all",
}

DALONG_LANGS = ("ja", "en", "ko")


class HttpError(RuntimeError):
    """网络层错误（含状态码），上层据此提示用户。"""

    def __init__(self, message: str, url: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


# --------------------------------------------------------------------------
# 缓存与限速
# --------------------------------------------------------------------------

def cache_dir() -> Path:
    """缓存目录，可用环境变量 GUNPLA_CACHE_DIR 覆盖。"""
    env = os.environ.get("GUNPLA_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cache" / "gunpla-skill"


_last_request: dict[str, float] = {}


def _throttle(host: str) -> None:
    now = time.monotonic()
    wait = MIN_INTERVAL - (now - _last_request.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.monotonic()


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return cache_dir() / "http" / f"{digest}.json"


def _cache_read(url: str, ttl: float) -> Any | None:
    if ttl <= 0:
        return None
    path = _cache_path(url)
    try:
        stat = path.stat()
    except OSError:
        return None
    if time.time() - stat.st_mtime > ttl:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    return payload.get("body")


def _cache_write(url: str, body: Any) -> None:
    path = _cache_path(url)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"url": url, "saved_at": int(time.time()), "body": body}, fh, ensure_ascii=False)
        tmp.replace(path)
    except OSError:
        pass  # 缓存写失败不影响主流程


def fetch_bytes(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    ttl: float = 0.0,
    headers: dict[str, str] | None = None,
) -> bytes:
    """GET 一个 URL 并返回 bytes（带重试、退避、gzip 解压、可选缓存）。"""
    headers = headers or {}
    host = urllib.parse.urlsplit(url).netloc

    last_error: Exception | None = None
    for attempt in range(retries):
        _throttle(host)
        request = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 4xx 除了 429 之外没有重试价值
            if 400 <= exc.code < 500 and exc.code != 429:
                raise HttpError(f"HTTP {exc.code}", url, exc.code) from exc
            time.sleep(min(2 ** attempt * 0.8, 5.0))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(min(2 ** attempt * 0.8, 5.0))

    raise HttpError(f"请求失败：{last_error}", url)


def fetch_json(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    ttl: float = 0.0,
) -> Any:
    """GET 一个 JSON 接口；带缓存时优先命中缓存。"""
    cached = _cache_read(url, ttl)
    if cached is not None:
        return cached
    raw = fetch_bytes(url, timeout=timeout, retries=retries)
    if raw[:2] == b"\x1f\x8b":  # 有些站点忽略 gzip 头
        raw = gzip.decompress(raw)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HttpError("返回内容不是合法 JSON", url) from exc
    if ttl > 0:
        _cache_write(url, body)
    return body


# --------------------------------------------------------------------------
# 寿屋台湾（Kotobukiya Taiwan）
# --------------------------------------------------------------------------

def koto_collection_url(page: int = 1, per: int = 24, collection: str = "pre-order", sort_by: str = "") -> str:
    """预购集合搜索接口。page 从 1 开始，per 为每页数量。"""
    slug = KOTO_COLLECTIONS.get(collection, collection)
    query = urllib.parse.urlencode(
        {
            "page": page,
            "per": per,
            "sort_by": sort_by,
            "product_filters": "[]",
            "tags": "",
        },
        quote_via=urllib.parse.quote,
    )
    return f"{KOTO_BASE}/collections/{slug}/search_products.json?{query}"


def koto_products(
    *,
    page: int = 1,
    per: int = 24,
    collection: str = "pre-order",
    sort_by: str = "",
    ttl: float = 300.0,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """取一页商品，返回 {total_count, total_pages, current_page, products}。"""
    return fetch_json(
        koto_collection_url(page, per, collection, sort_by),
        timeout=timeout,
        ttl=ttl,
    )


def koto_iter_products(
    *,
    collection: str = "pre-order",
    per: int = 24,
    max_pages: int = 20,
    sort_by: str = "",
    ttl: float = 300.0,
    timeout: float = DEFAULT_TIMEOUT,
    on_page: Any = None,
) -> Iterable[dict[str, Any]]:
    """遍历集合里的全部商品（按 total_pages 翻页）。"""
    first = koto_products(page=1, per=per, collection=collection, sort_by=sort_by, ttl=ttl, timeout=timeout)
    pages = int(first.get("total_pages") or 1)
    pages = max(1, min(pages, max_pages))
    if on_page:
        on_page(1, pages, int(first.get("total_count") or 0))
    yield from first.get("products") or []
    for page in range(2, pages + 1):
        data = koto_products(page=page, per=per, collection=collection, sort_by=sort_by, ttl=ttl, timeout=timeout)
        if on_page:
            on_page(page, pages, int(data.get("total_count") or 0))
        yield from data.get("products") or []


def koto_product(handle: str, *, ttl: float = 3600.0, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """单个商品详情 JSON（备用：需要完整描述/多规格时使用）。"""
    return fetch_json(f"{KOTO_BASE}/products/{urllib.parse.quote(handle)}.json", timeout=timeout, ttl=ttl)


# --------------------------------------------------------------------------
# Dalong
# --------------------------------------------------------------------------

def dalong_latest_url(timestamp: str | int | None = None) -> str:
    """最新收录列表。t 只是防缓存的时间戳，传秒级时间即可。"""
    ts = timestamp if timestamp is not None else int(time.time())
    return f"{DALONG_BASE}/search_latest.json?t={ts}"


def dalong_latest(
    *,
    timestamp: str | int | None = None,
    ttl: float = 120.0,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """返回 {"built": "261029_153224", "rows": [...]}。"""
    return fetch_json(dalong_latest_url(timestamp), timeout=timeout, ttl=ttl)


def dalong_search_url(
    keyword: str,
    *,
    lang: str = "ja",
    limit: int = 20,
    offset: int = 0,
    sort: str = "release",
    direction: str = "desc",
) -> str:
    """说明书搜索接口。lang 只能是 ja / en / ko，keyword 必须是该语言。"""
    if lang not in DALONG_LANGS:
        raise ValueError(f"lang 必须是 {DALONG_LANGS} 之一，收到 {lang!r}")
    query = urllib.parse.urlencode(
        {
            "action": "search",
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "dir": direction,
            "lang": lang,
            "q": keyword,
        },
        quote_via=urllib.parse.quote,
    )
    return f"{DALONG_BASE}/api/search.cgi?{query}"


def dalong_search(
    keyword: str,
    *,
    lang: str = "ja",
    limit: int = 20,
    offset: int = 0,
    ttl: float = 3600.0,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """返回 {"total": n, "limit": n, "results": [...], "offset": n}。"""
    return fetch_json(
        dalong_search_url(keyword, lang=lang, limit=limit, offset=offset),
        timeout=timeout,
        ttl=ttl,
    )


def dalong_thumb_dir(row: dict[str, Any]) -> str:
    """从 thumb 推出說明書所在目录（kotobukiya / hg / cg / sd / etc ...）。"""
    thumb = (row.get("thumb") or "").replace("\\", "/").lstrip("/")
    return thumb.split("/", 1)[0] if "/" in thumb else (row.get("category") or "").lower()


def dalong_manual_links(row: dict[str, Any]) -> dict[str, str]:
    """根据一条结果拼出日/韩/英说明书链接与图片链接。

    规则（来自 Dalong 站点结构，已实测可访问）：
        https://www.dalong.net/reviews/{dir}/{folder}/{folder}_p_j.htm   日文
        https://www.dalong.net/reviews/{dir}/{folder}/{folder}_p.htm     韩文
        https://www.dalong.net/reviews/{dir}/{folder}/{folder}_p_e.htm   英文
        https://www.dalong.net/reviews/{thumb}                           图片
    """
    folder = row.get("folder_name") or ""
    directory = dalong_thumb_dir(row)
    thumb = (row.get("thumb") or "").lstrip("/")
    links: dict[str, str] = {}
    if folder and directory:
        base = f"{DALONG_BASE}/{directory}/{folder}/{folder}"
        # 实测：_i_ 才是说明书/板件/产品信息页，_p_ 是成品评测页
        links["manual_ja"] = f"{base}_i_j.htm"
        links["manual_ko"] = f"{base}_i.htm"
        links["manual_en"] = f"{base}_i_e.htm"
        links["review_ja"] = f"{base}_p_j.htm"
        links["review_ko"] = f"{base}_p.htm"
        links["review_en"] = f"{base}_p_e.htm"
    if thumb:
        links["image"] = f"{DALONG_BASE}/{thumb}"
    return links


# Dalong 说明书目录的常见取值，用于「只给 folder 名」时逐个探测
DALONG_DIRS = ("kotobukiya", "hg", "mg", "rg", "sd", "sd2", "cg", "etc", "tomy", "fmp", "megalo")


def parse_dalong_ref(text: str) -> tuple[str | None, str] | None:
    """从 Dalong 链接或零件号里解析出 (目录, folder)。

        https://www.dalong.net/reviews/kotobukiya/mdv26/mdv26_p_e.htm -> ("kotobukiya", "mdv26")
        mdv26                                                         -> (None, "mdv26")
    """
    text = (text or "").strip()
    if not text:
        return None
    match = re.search(r"dalong\.net/reviews/([a-z0-9]+)/([a-z0-9]+)/", text, re.IGNORECASE)
    if match:
        return match.group(1).lower(), match.group(2).lower()
    if re.fullmatch(r"[a-z]{2,5}\d{1,4}[a-z]?", text, re.IGNORECASE):
        return None, text.lower()
    return None


def dalong_links_for(folder: str, directory: str) -> dict[str, str]:
    """已知目录 + folder 时，直接按命名规则拼说明书链接。

    注意：缩略图目录名不固定（`kotocata` / `hgcata` / `etccata` …），
    只凭 folder 推不出图片地址，所以这里不返回 image，避免给出坏链接。
    """
    base = f"{DALONG_BASE}/{directory}/{folder}/{folder}"
    return {
        "manual_ja": f"{base}_i_j.htm",
        "manual_ko": f"{base}_i.htm",
        "manual_en": f"{base}_i_e.htm",
        "review_ja": f"{base}_p_j.htm",
        "review_ko": f"{base}_p.htm",
        "review_en": f"{base}_p_e.htm",
    }


def resolve_dalong_folder(
    folder: str,
    directory: str | None = None,
    *,
    timeout: float = 12.0,
) -> dict[str, str] | None:
    """只给了 folder 名时，逐个目录探测说明书页是否存在。"""
    if directory:
        return dalong_links_for(folder, directory)
    for candidate in DALONG_DIRS:
        links = dalong_links_for(folder, candidate)
        if verify_url(links["manual_en"], timeout=timeout):
            return links
    return None


def verify_url(url: str, *, timeout: float = 15.0) -> bool:
    """用 HEAD（失败则 Range GET）确认链接可用，用于「说明书链接存在性」校验。"""
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-64"})):
        request = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **extra})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status < 400:
                    return True
        except urllib.error.HTTPError:
            continue
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return False
