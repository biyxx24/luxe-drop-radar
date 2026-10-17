#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
奢侈品官网上新监控 —— 官网一出新品就推送到 Telegram（图 + 价格 + 码数 + 链接）

用法（在本文件所在的文件夹里运行）：
  python monitor.py probe [品牌...]     第一次先跑：逐个品牌探测能不能抓、用哪种抓法
  python monitor.py demo  <品牌>        把这个品牌最新的 2 件商品推给你，看看推送长什么样
  python monitor.py once  [品牌...]     跑一轮（每个品牌第一轮只记录现有商品，不推送）
  python monitor.py run                 常驻运行，每隔几分钟检查一次（日常用这个）
  python monitor.py test-notify         给 Telegram 发一条测试消息
  python monitor.py chat-id             查你的 Telegram chat_id（先给你的 bot 发一句话）
  python monitor.py recent [N]          看最近推送过的 N 条上新（含首次发现时间）

可选：-c/--config 指定配置文件（默认同目录下的 config.yaml）
"""
from __future__ import annotations

import argparse
import gzip
import html as htmllib
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：请先运行  pip install -r requirements.txt")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
log = logging.getLogger("monitor")


# ════════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════════
DEFAULT_SETTINGS = {
    "interval_minutes": 10,          # 多久检查一轮
    "sitemap_interval_minutes": 60,  # sitemap 体积大，单独设间隔
    "request_delay_seconds": 1.5,    # 同一网站两次请求之间至少隔多久
    "timeout_seconds": 25,
    "gender": "all",                 # all / men / women
    "my_sizes": [],
    "only_my_sizes": False,
    "include_keywords": [],
    "exclude_keywords": [],
    "max_cards_per_brand": 15,       # 一个品牌一轮上新超过这个数，改成发汇总清单
    "max_new_urls_per_cycle": 300,   # 超过这个数视为网站改版，静默记录 + 提醒
    "fail_alert_after": 6,           # 连续失败几轮后提醒你
    "browser_headless": True,
    "db_path": "seen.sqlite",
    "log_path": "monitor.log",
}


def as_list(v):
    if v is None or v is False:
        return []
    return v if isinstance(v, list) else [v]


def keyword_regex(words):
    """关键词 → 正则；自动兼容复数（bag→bags）和 连字符/空格（card holder→card-holder）。"""
    words = [str(w).strip().lower() for w in as_list(words) if str(w).strip()]
    if not words:
        return None
    parts = [r"[\s_\-]*".join(re.escape(t) for t in w.split()) + r"(?:e?s)?" for w in words]
    return re.compile(r"(?<![a-z])(?:" + "|".join(parts) + r")(?![a-z])", re.I)


class Config:
    def __init__(self, path: str):
        p = Path(path)
        if not p.is_absolute():
            p = (Path.cwd() / p) if (Path.cwd() / p).exists() else (HERE / p)
        if not p.exists():
            sys.exit(f"找不到配置文件：{p}")
        self.path = p
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        s = dict(DEFAULT_SETTINGS)
        s.update({k: v for k, v in (raw.get("settings") or {}).items() if v is not None})
        self.s = s
        tg = raw.get("telegram") or {}
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or str(tg.get("bot_token") or "").strip()
        chats = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id") or ""
        self.tg_chats = [str(c).strip() for c in as_list(chats) if str(c).strip()]
        self.brands = []
        for b in raw.get("brands") or []:
            if not b or not b.get("name") or not b.get("url"):
                continue
            b = dict(b)
            b["url"] = str(b["url"]).rstrip("/")
            b.setdefault("enabled", True)
            self.brands.append(b)
        self.exclude_re = keyword_regex(s.get("exclude_keywords"))
        self.include_re = keyword_regex(s.get("include_keywords"))
        self.my_sizes = [str(x) for x in as_list(s.get("my_sizes"))]
        self.gender = str(s.get("gender") or "all").lower()
        self.db_path = p.parent / s["db_path"]
        self.log_path = p.parent / s["log_path"]


# ════════════════════════════════════════════════════════════════════
# 网络请求：curl_cffi 模拟真实 Chrome 的 TLS 指纹，能过掉大部分基础反爬；
# 个别品牌可在配置里开 browser: true 改用真浏览器（Playwright）
# ════════════════════════════════════════════════════════════════════
class FetchError(Exception):
    def __init__(self, msg, blocked=False):
        super().__init__(msg)
        self.blocked = blocked


class Resp:
    def __init__(self, status, content=b"", url="", error=None, ctype=""):
        self.status, self.content, self.url, self.error, self.ctype = status, content or b"", url, error, ctype or ""
        self._text = None

    @property
    def text(self):
        if self._text is None:
            data = self.content
            if data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            self._text = data.decode("utf-8", "replace")
        return self._text


BLOCK_MARKERS = ("px-captcha", "datadome", "captcha-delivery", "_incapsula_resource", "cf-chl",
                 "attention required", "access denied", "pardon our interruption", "are you a robot",
                 "request unsuccessful", "verify you are human")


def is_blocked(r: Resp) -> bool:
    if r.status in (401, 403, 429):
        return True
    if r.status in (200, 503) and len(r.content) < 30000:
        low = r.text[:30000].lower()
        return any(m in low for m in BLOCK_MARKERS)
    return False


def check_ok(r: Resp, url: str):
    if r.error:
        raise FetchError(f"网络错误：{r.error[:150]}")
    if is_blocked(r):
        raise FetchError(f"被反爬拦截（HTTP {r.status}）", blocked=True)
    if r.status != 200:
        raise FetchError(f"HTTP {r.status}：{url}")


class Fetcher:
    def __init__(self, s: dict):
        try:
            from curl_cffi import requests as creq
        except ImportError:
            sys.exit("缺少依赖 curl_cffi：请先运行  pip install -r requirements.txt")
        self.session = creq.Session(impersonate="chrome")
        self.session.headers.update({"Accept-Language": "en-US,en;q=0.9"})
        self.delay = float(s.get("request_delay_seconds", 1.5))
        self.timeout = float(s.get("timeout_seconds", 25))
        self.headless = bool(s.get("browser_headless", True))
        self._last: dict[str, float] = {}
        self._pw = self._browser = self._ctx = self._page = None

    def _throttle(self, url):
        host = urlparse(url).netloc
        wait = self.delay * random.uniform(0.7, 1.3) - (time.time() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.time()

    def get(self, url: str, browser: bool = False) -> Resp:
        self._throttle(url)
        if browser:
            return self._browser_get(url)
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            return Resp(r.status_code, r.content, str(r.url), ctype=r.headers.get("content-type", ""))
        except Exception as e:
            return Resp(0, b"", url, error=str(e))

    def _browser_get(self, url: str) -> Resp:
        if self._page is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                return Resp(0, b"", url, error="没装 playwright：pip install playwright && playwright install chromium")
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)
            self._ctx = self._browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
            self._page = self._ctx.new_page()
        try:
            if re.search(r"\.(xml|gz|json|txt|js)$", urlparse(url).path, re.I):
                # sitemap / robots / JSON 这类文件不用打开页面，直接用同一浏览器会话（带 cookie）请求
                r = self._ctx.request.get(url, timeout=int(self.timeout * 1000))
                return Resp(r.status, r.body(), r.url, ctype=r.headers.get("content-type", ""))
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            ctype = (resp.headers.get("content-type", "") if resp else "") or ""
            if resp and ("xml" in ctype or "json" in ctype or url.endswith(".gz")):
                body = resp.body()
            else:
                self._page.wait_for_timeout(2500)  # 等前端把商品渲染出来
                body = self._page.content().encode("utf-8")
            return Resp(resp.status if resp else 0, body, self._page.url, ctype=ctype)
        except Exception as e:
            return Resp(0, b"", url, error=str(e))

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
# 通用解析工具
# ════════════════════════════════════════════════════════════════════
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def norm_url(u: str) -> str:
    p = urlparse(u.strip())
    path = re.sub(r"/{2,}", "/", p.path).rstrip("/") or "/"
    return urlunparse(((p.scheme or "https").lower(), p.netloc.lower(), path, "", "", ""))


def site_root(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(url: str, root: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host == root or host.endswith("." + root)


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


W_RE = re.compile(r"(?<![a-z])(women|womens|woman|womenswear|female|femme|donna|ladies)(?![a-z])", re.I)
M_RE = re.compile(r"(?<![a-z])(men|mens|menswear|male|homme|uomo)(?![a-z])", re.I)


def gender_of(text: str) -> str:
    w, m = bool(W_RE.search(text or "")), bool(M_RE.search(text or ""))
    return "women" if w and not m else "men" if m and not w else "unisex"


_YY = r"(?:20)?(2[4-9])(?!\d)"
SEASON_PATTERNS = [
    (re.compile(r"(?<![a-z])(?:fw|aw)[\s\-_]?" + _YY, re.I), "FW{}"),
    (re.compile(r"(?<![a-z])ss[\s\-_]?" + _YY, re.I), "SS{}"),
    (re.compile(r"pre[\s\-]?fall[\s\-]?" + _YY, re.I), "Pre-Fall {}"),
    (re.compile(r"pre[\s\-]?spring[\s\-]?" + _YY, re.I), "Pre-SS{}"),
    (re.compile(r"(?:resort|cruise)[\s\-]?" + _YY, re.I), "Resort {}"),
    (re.compile(r"(?:fall|autumn)[\s\-/]*winter[\s\-]?" + _YY, re.I), "FW{}"),
    (re.compile(r"spring[\s\-/]*summer[\s\-]?" + _YY, re.I), "SS{}"),
]


def detect_season(*texts) -> str:
    blob = " ".join(str(t) for t in texts if t)
    for rx, fmt in SEASON_PATTERNS:
        m = rx.search(blob)
        if m:
            return fmt.format(m.group(1))
    return ""


def size_tokens(s: str) -> set:
    up = str(s).upper()
    toks = [t for t in re.split(r"[^A-Z0-9.½]+", up) if t]
    pairs = {a + b for a, b in zip(toks, toks[1:])}  # "EU 42 / US 9" → 也能匹配 "US9"、"EU42"
    return set(toks) | pairs | {up.replace(" ", "")}


def size_hits(sizes, my_sizes):
    mine = {str(m).upper().replace(" ", "") for m in my_sizes}
    return [s for s in sizes if size_tokens(s) & mine]


SIZE_OPT_RE = re.compile(r"size|taille|taglia|gr[öo]sse|größe|talla|尺码|尺寸|サイズ", re.I)
CURRENCY_SYM = {"USD": "$", "EUR": "€", "GBP": "£", "CNY": "¥", "JPY": "¥", "HKD": "HK$", "CAD": "CA$",
                "AUD": "A$", "SGD": "S$", "KRW": "₩", "CHF": "CHF "}


def to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        m = re.search(r"\d[\d,]*\.?\d*", str(v))
        return float(m.group(0).replace(",", "")) if m else None


def fmt_price(p, cur):
    if p is None:
        return "价格未标"
    num = f"{p:,.0f}" if abs(p - round(p)) < 0.005 else f"{p:,.2f}"
    sym = CURRENCY_SYM.get((cur or "").upper())
    return f"{sym}{num}" if sym else f"{num} {cur or ''}".strip()


# ---------- JSON-LD / meta ----------
LD_RE = re.compile(r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)


def iter_jsonld(text: str):
    for raw in LD_RE.findall(text or ""):
        raw = raw.strip()
        raw = re.sub(r"^\s*<!--|-->\s*$", "", raw).strip()
        data = None
        for cand in (raw, htmllib.unescape(raw)):
            try:
                data = json.loads(cand, strict=False)
                break
            except ValueError:
                continue
        if data is not None:
            yield from _walk(data)


def _walk(x):
    if isinstance(x, list):
        for i in x:
            yield from _walk(i)
    elif isinstance(x, dict):
        yield x
        for v in x.values():
            if isinstance(v, (list, dict)):
                yield from _walk(v)


def ld_types(node) -> set:
    return {str(t).lower() for t in as_list(node.get("@type"))}


META_TAG_RE = re.compile(r"<meta\s[^>]*>", re.I)
ATTR_RE = re.compile(r"([a-zA-Z_:.\-]+)\s*=\s*(\"([^\"]*)\"|'([^']*)')")


def parse_meta(text: str) -> dict:
    out = {}
    for tag in META_TAG_RE.findall(text or ""):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in ATTR_RE.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key and "content" in attrs and key not in out:
            out[key] = htmllib.unescape(attrs["content"])
    m = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.S | re.I)
    if m:
        out.setdefault("title", strip_tags(m.group(1)))
    return out


def first_image(v, base):
    for x in as_list(v):
        u = x.get("url") or x.get("contentUrl") if isinstance(x, dict) else x
        if isinstance(u, str) and u.strip():
            u = u.strip()
            return ("https:" + u) if u.startswith("//") else urljoin(base, u)
    return None


def _flat_offers(o):
    out = []
    for x in as_list(o):
        if isinstance(x, dict):
            out.append(x)
            if x.get("offers"):
                out.extend(_flat_offers(x.get("offers")))
    return out


def _available(o) -> bool:
    a = str(o.get("availability") or "").lower()
    if not a:
        return True
    return any(k in a for k in ("instock", "limitedavailability", "preorder", "presale", "onlineonly"))


def _size_of(v):
    s = v.get("size")
    if isinstance(s, dict):
        s = s.get("name") or s.get("value")
    if not s and isinstance(v.get("itemOffered"), dict):
        s = v["itemOffered"].get("size")
    return str(s).strip() if s else None


def parse_product_page(text: str, url: str):
    """从商品页解析：名称、价格、币种、图片、码数。识别不出商品返回 None。"""
    nodes = list(iter_jsonld(text))
    group = next((n for n in nodes if "productgroup" in ld_types(n)), None)
    prod = group or next((n for n in nodes if "product" in ld_types(n) and n.get("name")), None)
    meta = parse_meta(text)
    if not prod and meta.get("og:type", "").lower() not in ("product", "og:product", "product.item"):
        return None
    prod = prod or {}
    title = strip_tags(str(prod.get("name") or meta.get("og:title") or meta.get("title") or ""))
    if not title:
        return None

    offers = _flat_offers(prod.get("offers"))
    variants = [v for v in as_list(prod.get("hasVariant")) if isinstance(v, dict)]
    sizes_in, sizes_out, prices, cur = [], [], [], None
    for v in variants:
        vo = _flat_offers(v.get("offers"))
        offers.extend(vo)
        s = _size_of(v)
        if s:
            (sizes_in if (not vo or any(_available(o) for o in vo)) else sizes_out).append(s)
    if not variants and len(offers) > 1:
        for o in offers:
            s = _size_of(o) or (str(o.get("name")).strip() if o.get("name") and len(str(o.get("name"))) <= 12 else None)
            if s:
                (sizes_in if _available(o) else sizes_out).append(s)
    for o in offers:
        p = to_float(o.get("price") or o.get("lowPrice"))
        if p:
            prices.append(p)
            hp = to_float(o.get("highPrice"))
            if hp:
                prices.append(hp)
        cur = cur or o.get("priceCurrency")
    if not prices:
        p = to_float(meta.get("product:price:amount") or meta.get("og:price:amount") or meta.get("price"))
        if p:
            prices.append(p)
        cur = cur or meta.get("product:price:currency") or meta.get("og:price:currency") or meta.get("pricecurrency")

    sizes_in = dedupe(sizes_in)
    sizes_out = [s for s in dedupe(sizes_out) if s not in sizes_in]
    category = prod.get("category")
    if isinstance(category, list):
        category = category[-1] if category else ""
    category = re.split(r"\s*[>/|›]\s*", str(category or ""))[-1][:30]
    audience = prod.get("audience")
    gender_hint = audience.get("suggestedGender", "") if isinstance(audience, dict) else ""
    return {
        "title": title[:200],
        "price": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "currency": cur or "",
        "image": first_image(prod.get("image"), url) or (urljoin(url, meta["og:image"]) if meta.get("og:image") else None),
        "sizes_in": sizes_in,
        "sizes_out": sizes_out,
        "category": category,
        "desc": strip_tags(str(prod.get("description") or meta.get("og:description") or ""))[:600],
        "gender_hint": str(gender_hint),
    }


# ---------- 链接提取 ----------
HREF_RE = re.compile(r"href\s*=\s*[\"']([^\"'#\s>]+)", re.I)
QUOTED_PATH_RE = re.compile(r"[\"']((?:https?://[^\"'\s<>]+)|(?:/[\w\-.~%/]{6,250}))[\"']")
NON_PRODUCT_RE = re.compile(
    r"/(search|account|login|register|cart|bag|checkout|wishlist|stores?|store-locator|help|faq|"
    r"customer-?(care|service)|contact|legal|privacy|terms|cookies?|careers|sitemap|blogs?|journal|"
    r"stories|magazine|pages|static|assets|_next|cdn)(/|$)", re.I)
ASSET_EXT_RE = re.compile(r"\.(js|mjs|css|map|json|xml|png|jpe?g|gif|webp|avif|svg|ico|woff2?|ttf|mp4|pdf|txt)$", re.I)


def extract_links(text: str, base: str):
    text = (text or "").replace("\\/", "/")
    out = []
    for h in HREF_RE.findall(text) + QUOTED_PATH_RE.findall(text):
        h = htmllib.unescape(h)
        if h.startswith(("javascript:", "mailto:", "tel:", "data:")):
            continue
        out.append(urljoin(base, h))
    return dedupe(out)


def looks_like_product(url: str) -> bool:
    path = urlparse(url).path
    if not path or path == "/" or NON_PRODUCT_RE.search(path) or ASSET_EXT_RE.search(path):
        return False
    low = path.lower()
    if re.search(r"/(products?|p|pd|item)/[^/]+", low):
        return True
    last = low.rstrip("/").rsplit("/", 1)[-1]
    if re.search(r"\d{5,}", last):
        return True
    if last.endswith((".html", ".htm")) and re.search(r"\d{4,}", last):
        return True
    return any(re.fullmatch(r"(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{8,}", t) for t in re.split(r"[-_.]", last))


NEWIN_RE = re.compile(r"(new[-_]?(arrivals?|in|now|season|collection|this-week)|what-?s-?new|just-?(in|landed)|"
                      r"latest|nouveaut|novita|neuheiten|nuevo)", re.I)


# ════════════════════════════════════════════════════════════════════
# 抓法 1：Shopify（直接读 /products.json，含码数库存）
# ════════════════════════════════════════════════════════════════════
def shopify_detect(http: Fetcher, b) -> bool | None:
    r = http.get(b["url"] + "/products.json?limit=1")
    if r.error or is_blocked(r):
        return None
    if r.status != 200:
        return False
    try:
        d = json.loads(r.text)
        return isinstance(d, dict) and isinstance(d.get("products"), list)
    except ValueError:
        return False


def shopify_currency(http: Fetcher, b):
    r = http.get(b["url"] + "/cart.js")
    if r.status == 200:
        try:
            return json.loads(r.text).get("currency")
        except ValueError:
            return None
    return None


def shopify_products(http: Fetcher, b, max_pages=40, first_page_only=False):
    coll = b.get("shopify_collection")
    path = f"/collections/{coll}/products.json" if coll else "/products.json"
    out = []
    for page in range(1, max_pages + 1):
        url = f"{b['url']}{path}?limit=250&page={page}"
        r = http.get(url)
        check_ok(r, url)
        try:
            prods = json.loads(r.text).get("products") or []
        except ValueError:
            raise FetchError("products.json 返回的不是 JSON")
        out.extend(prods)
        if len(prods) < 250 or first_page_only:
            break
    return out


def shopify_item(p: dict, b: dict, currency: str) -> dict:
    size_idx = None
    for i, opt in enumerate(p.get("options") or [], start=1):
        name = opt.get("name", "") if isinstance(opt, dict) else str(opt)
        if SIZE_OPT_RE.search(name):
            size_idx = i
            break
    variants = p.get("variants") or []
    sizes_in, sizes_out, prices = [], [], []
    for v in variants:
        pr = to_float(v.get("price"))
        if pr:
            prices.append(pr)
        if size_idx:
            s = v.get(f"option{size_idx}")
            if s:
                (sizes_in if v.get("available", True) else sizes_out).append(str(s))
    sizes_in = dedupe(sizes_in)
    sizes_out = [s for s in dedupe(sizes_out) if s not in sizes_in]
    img = None
    if p.get("images"):
        img = p["images"][0].get("src")
        if img and img.startswith("//"):
            img = "https:" + img
        if img and ("cdn.shopify.com" in img or "/cdn/shop/" in img):
            img += ("&" if "?" in img else "?") + "width=1200"
    tags = p.get("tags") or []
    tags = tags if isinstance(tags, list) else [t.strip() for t in str(tags).split(",")]
    desc = strip_tags(p.get("body_html") or "")[:600]
    text = " ".join([p.get("title") or "", p.get("product_type") or "", " ".join(tags)])
    return {
        "key": f"shopify:{p.get('id')}",
        "url": f"{b['url']}/products/{p.get('handle')}",
        "title": (p.get("title") or "").strip(),
        "brand": b["name"],
        "price": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "currency": currency or "",
        "image": img,
        "sizes_in": sizes_in,
        "sizes_out": sizes_out,
        "category": (p.get("product_type") or "").strip()[:30],
        "gender": gender_of(text),
        "season": detect_season(text, desc),
        "filter_text": f"{text} {p.get('handle') or ''}",
        "published": p.get("published_at") or p.get("created_at") or "",
        "source": "shopify",
    }


# ════════════════════════════════════════════════════════════════════
# 抓法 2：网页（New In 页面 + sitemap 找新链接，再读商品页 JSON-LD）
# ════════════════════════════════════════════════════════════════════
def url_ok(u: str, b: dict, cfg: Config) -> bool:
    low = u.lower()
    inc = [str(s).lower() for s in as_list(b.get("url_include"))]
    if inc and not any(s in low for s in inc):
        return False
    if any(str(s).lower() in low for s in as_list(b.get("url_exclude"))):
        return False
    path = urlparse(u).path
    if cfg.exclude_re and cfg.exclude_re.search(path):
        return False
    g = gender_of(path)
    if (cfg.gender == "men" and g == "women") or (cfg.gender == "women" and g == "men"):
        return False
    return True


def discover_new_in(http: Fetcher, b: dict, cfg: Config):
    r = http.get(b["url"] + "/", browser=bool(b.get("browser")))
    check_ok(r, b["url"])
    root = site_root(b["url"])
    inc = [str(s).lower() for s in as_list(b.get("url_include"))]
    pages = []
    for h in extract_links(r.text, r.url):
        if not same_site(h, root) or ASSET_EXT_RE.search(urlparse(h).path):
            continue
        path = urlparse(h).path
        if not NEWIN_RE.search(path):
            continue
        if inc and not any(s in h.lower() for s in inc):
            continue
        g = gender_of(path)
        if (cfg.gender == "men" and g == "women") or (cfg.gender == "women" and g == "men"):
            continue
        if looks_like_product(h):
            continue
        pages.append(norm_url(h))
    return dedupe(pages)[:4]


def listing_links(http: Fetcher, page: str, b: dict, cfg: Config) -> set:
    r = http.get(page, browser=bool(b.get("browser")))
    check_ok(r, page)
    root = site_root(b["url"])
    links = set()
    for node in iter_jsonld(r.text):
        if "itemlist" in ld_types(node):
            for el in as_list(node.get("itemListElement")):
                if not isinstance(el, dict):
                    continue
                u = el.get("url")
                if not u:
                    it = el.get("item")
                    u = it.get("url") or it.get("@id") if isinstance(it, dict) else it
                if isinstance(u, str) and u:
                    links.add(urljoin(r.url, u))
    pat = re.compile(b["product_pattern"]) if b.get("product_pattern") else None
    for h in extract_links(r.text, r.url):
        if same_site(h, root) and (pat.search(h) if pat else looks_like_product(h)):
            links.add(h)
    page_norm = norm_url(r.url)
    return {norm_url(u) for u in links if url_ok(u, b, cfg) and norm_url(u) != page_norm}


LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.S | re.I)


def discover_sitemaps(http: Fetcher, b: dict):
    explicit = as_list(b.get("sitemap_url"))
    if explicit:
        return [urljoin(b["url"] + "/", u) for u in explicit]
    r = http.get(b["url"] + "/robots.txt", browser=bool(b.get("browser")))
    if is_blocked(r):
        raise FetchError(f"robots.txt 被拦截（HTTP {r.status}）", blocked=True)
    sms = re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", r.text) if r.status == 200 else []
    return dedupe(sms) or [b["url"] + "/sitemap.xml"]


def _pick(locs, include, prio):
    if include:
        low = [(l, l.lower()) for l in locs]
        picked = [l for l, ll in low if all(k in ll for k in include)] or \
                 [l for l, ll in low if any(k in ll for k in include)] or locs
    else:
        picked = list(locs)
    if prio:
        picked.sort(key=lambda l: 0 if any(t in l.lower() for t in prio) else 1)
    return picked


def sitemap_urls(http: Fetcher, b: dict, cfg: Config, debug: list | None = None) -> set:
    include = [str(s).lower() for s in as_list(b.get("sitemap_include", ["product"]))]
    prio = []
    for t in as_list(b.get("url_include")):
        t = str(t).strip("/").lower()
        prio += [t, t.replace("-", "_"), t.replace("/", "_"), t.replace("/", "-")]
    max_files = int(b.get("sitemap_max_files", 40))
    queue = _pick(discover_sitemaps(http, b), include, prio)
    seen, urls, ok_files, first_err = set(), [], 0, None
    while queue and len(seen) < max_files:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        r = http.get(sm, browser=bool(b.get("browser")))
        try:
            check_ok(r, sm)
        except FetchError as e:
            first_err = first_err or e
            if debug is not None:
                debug.append(f"读取失败 {sm}：{e}")
            continue
        ok_files += 1
        text = r.text
        locs = [htmllib.unescape(x).strip() for x in LOC_RE.findall(text)]
        if "<sitemapindex" in text[:5000].lower():
            children = _pick(locs, include, prio)
            queue.extend(children)
            if debug is not None:
                debug.append(f"索引 {sm} → {len(locs)} 个子 sitemap，选中 {len(children)} 个，例如 "
                             + ", ".join(c.rsplit('/', 1)[-1] for c in children[:3]))
        else:
            urls.extend(locs)
            if debug is not None:
                debug.append(f"读取 {sm.rsplit('/', 1)[-1]} → {len(locs)} 个链接")
    if ok_files == 0:
        raise first_err or FetchError("没读到任何 sitemap")
    return {norm_url(u) for u in urls if url_ok(u, b, cfg)}


def fetch_product(http: Fetcher, url: str, b: dict):
    r = http.get(url, browser=bool(b.get("browser")))
    if r.status in (404, 410):
        return None
    check_ok(r, url)
    info = parse_product_page(r.text, r.url)
    if not info:
        return None
    filter_text = " ".join([info["title"], info["category"], urlparse(url).path, info["gender_hint"]])
    info.update(
        url=url,
        brand=b["name"],
        gender=gender_of(filter_text),
        season=detect_season(info["title"], info["desc"], urlparse(url).path),
        filter_text=filter_text,
    )
    return info


# ════════════════════════════════════════════════════════════════════
# 推送：Telegram（没填 token 时打印到终端）
# ════════════════════════════════════════════════════════════════════
GENDER_LABEL = {"men": "男装", "women": "女装"}


def caption(it: dict, my_sizes, compact=False) -> str:
    e = htmllib.escape
    head = f"🆕 <b>{e(it['brand'])}</b>"
    extras = [x for x in (it.get("category"), GENDER_LABEL.get(it.get("gender", ""))) if x]
    if extras:
        head += " · " + " · ".join(e(str(x)) for x in extras)
    title = (it.get("title") or "(无标题)")[: 120 if compact else 200]
    lines = [head, f"<b>{e(title)}</b>"]
    price = fmt_price(it.get("price"), it.get("currency"))
    if it.get("price_max") and it.get("price") and it["price_max"] > it["price"] + 0.5:
        price += " – " + fmt_price(it["price_max"], it.get("currency"))
    lines.append(f"💰 {price}")
    n = 10 if compact else 24
    sin, sout = it.get("sizes_in") or [], it.get("sizes_out") or []
    if sin:
        lines.append("📏 有货：" + " · ".join(e(s) for s in sin[:n]) + (" …" if len(sin) > n else ""))
    if sout:
        lines.append("　 缺货：" + " · ".join(e(s) for s in sout[:n]) + (" …" if len(sout) > n else ""))
    if not sin and not sout:
        lines.append("📏 码数：页面没写，点链接看")
    if my_sizes and (sin or sout):
        hits = size_hits(sin, my_sizes)
        lines.append("✅ 你的码有货：" + " · ".join(e(h) for h in hits) if hits else "⚪ 你的码暂时没货")
    if it.get("season"):
        lines.append(f"🏷 {e(it['season'])}")
    lines.append(f'<a href="{e(it["url"], quote=True)}">→ 去官网看</a>')
    return "\n".join(lines)


def to_console(text: str):
    print("─" * 50)
    print(htmllib.unescape(re.sub(r"<a href=\"([^\"]+)\">[^<]*</a>", r"\1", text).replace("<b>", "").replace("</b>", "")))


class Notifier:
    def __init__(self, token, chats, http: Fetcher):
        self.token, self.chats, self.http = token, chats, http
        self.api = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

    @property
    def enabled(self):
        return bool(self.token and self.chats)

    def _post(self, method, data=None, body=None, ctype=None, retry=True):
        url = f"{self.api}/bot{self.token}/{method}"
        if body is None:
            body, ctype = json.dumps(data).encode("utf-8"), "application/json"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                res = json.loads(e.read().decode("utf-8"))
            except Exception:
                res = {"ok": False, "description": f"HTTP {e.code}"}
        except Exception as e:
            res = {"ok": False, "description": str(e)}
        if not res.get("ok") and res.get("error_code") == 429 and retry:
            time.sleep(int((res.get("parameters") or {}).get("retry_after", 5)) + 1)
            return self._post(method, data, body, ctype, retry=False)
        return res

    def send_text(self, text: str, preview=False) -> bool:
        if not self.enabled:
            to_console(text)
            return True
        ok = True
        for chat in self.chats:
            res = self._post("sendMessage", {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML",
                                             "link_preview_options": {"is_disabled": not preview}})
            if not res.get("ok"):
                log.warning("Telegram 发送失败：%s", res.get("description"))
                ok = False
            time.sleep(0.4)
        return ok

    def send_item(self, it: dict, my_sizes) -> bool:
        cap = caption(it, my_sizes)
        if len(cap) > 1024:
            cap = caption(it, my_sizes, compact=True)[:1024]
        if not self.enabled:
            to_console(cap + (f"\n🖼 {it['image']}" if it.get("image") else ""))
            return True
        all_ok = True
        for chat in self.chats:
            sent = False
            if it.get("image"):
                res = self._post("sendPhoto", {"chat_id": chat, "photo": it["image"], "caption": cap, "parse_mode": "HTML"})
                sent = bool(res.get("ok")) or self._upload_photo(chat, it["image"], cap)
            if not sent:
                res = self._post("sendMessage", {"chat_id": chat, "text": cap, "parse_mode": "HTML",
                                                 "link_preview_options": {"is_disabled": False}})
                sent = bool(res.get("ok"))
                if not sent:
                    log.warning("Telegram 推送失败：%s", res.get("description"))
            all_ok = all_ok and sent
            time.sleep(0.6)
        return all_ok

    def _upload_photo(self, chat, image_url, cap) -> bool:
        """Telegram 自己下载图片失败（图太大/被拦）时，本机下载后上传。"""
        r = self.http.get(image_url)
        if r.status != 200 or not r.content or len(r.content) > 9_500_000:
            return False
        ctype = r.ctype.split(";")[0] if r.ctype.startswith("image/") else "image/jpeg"
        boundary = "----lux" + uuid.uuid4().hex
        parts = []
        for k, v in {"chat_id": chat, "caption": cap, "parse_mode": "HTML"}.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8"))
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"photo\"\r\n"
                     f"Content-Type: {ctype}\r\n\r\n".encode("utf-8") + r.content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        res = self._post("sendPhoto", body=b"".join(parts), ctype=f"multipart/form-data; boundary={boundary}")
        return bool(res.get("ok"))


def digest_messages(brand: str, items: list) -> list:
    e = htmllib.escape
    lines = [f'• <a href="{e(it["url"], quote=True)}">{e(it.get("title") or "(无标题)")}</a> — '
             f'{fmt_price(it.get("price"), it.get("currency"))}' for it in items]
    msgs, cur = [], f"🆕 <b>{e(brand)}</b> 一次上新 {len(items)} 件（太多不逐条发图）："
    for ln in lines:
        if len(cur) + len(ln) + 1 > 3900:
            msgs.append(cur)
            cur = f"（{e(brand)} 续）"
        cur += "\n" + ln
    msgs.append(cur)
    return msgs


# ════════════════════════════════════════════════════════════════════
# 存储：SQLite 记录见过的每个商品 + 首次发现时间
# ════════════════════════════════════════════════════════════════════
class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS items(
                brand TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL, source TEXT, url TEXT,
                title TEXT, price REAL, currency TEXT, image TEXT, sizes_in TEXT, season TEXT,
                first_seen TEXT NOT NULL, notified_at TEXT, attempts INTEGER DEFAULT 0,
                PRIMARY KEY(brand, key));
            CREATE INDEX IF NOT EXISTS idx_items_status ON items(brand, status);
            CREATE TABLE IF NOT EXISTS state(brand TEXT, k TEXT, v TEXT, PRIMARY KEY(brand, k));
        """)

    def keys(self, brand) -> set:
        return {r[0] for r in self.db.execute("SELECT key FROM items WHERE brand=?", (brand,))}

    def get(self, brand, k, default=None):
        row = self.db.execute("SELECT v FROM state WHERE brand=? AND k=?", (brand, k)).fetchone()
        return row[0] if row else default

    def set(self, brand, k, v):
        self.db.execute("INSERT OR REPLACE INTO state(brand,k,v) VALUES(?,?,?)", (brand, k, None if v is None else str(v)))
        self.db.commit()

    def add_many(self, brand, pairs, status, source):
        ts = now_iso()
        self.db.executemany(
            "INSERT OR IGNORE INTO items(brand,key,status,source,url,first_seen) VALUES(?,?,?,?,?,?)",
            [(brand, k, status, source, u, ts) for k, u in pairs])
        self.db.commit()

    def pending(self, brand, limit):
        return self.db.execute(
            "SELECT key,url,source,attempts FROM items WHERE brand=? AND status='pending' ORDER BY first_seen LIMIT ?",
            (brand, limit)).fetchall()

    def save_item(self, brand, it, status):
        self.db.execute("""
            INSERT INTO items(brand,key,status,source,url,title,price,currency,image,sizes_in,season,first_seen)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(brand,key) DO UPDATE SET status=excluded.status, url=excluded.url, title=excluded.title,
                price=excluded.price, currency=excluded.currency, image=excluded.image,
                sizes_in=excluded.sizes_in, season=excluded.season""",
            (brand, it["key"], status, it.get("source"), it.get("url"), it.get("title"), it.get("price"),
             it.get("currency"), it.get("image"), json.dumps(it.get("sizes_in") or [], ensure_ascii=False),
             it.get("season"), now_iso()))
        self.db.commit()

    def set_status(self, brand, key, status):
        self.db.execute("UPDATE items SET status=? WHERE brand=? AND key=?", (status, brand, key))
        self.db.commit()

    def bump(self, brand, key, max_attempts=5):
        self.db.execute("UPDATE items SET attempts=attempts+1 WHERE brand=? AND key=?", (brand, key))
        self.db.execute("UPDATE items SET status='gaveup' WHERE brand=? AND key=? AND attempts>=?", (brand, key, max_attempts))
        self.db.commit()

    def mark_notified(self, brand, key):
        self.db.execute("UPDATE items SET notified_at=? WHERE brand=? AND key=?", (now_iso(), brand, key))
        self.db.commit()

    def unsent(self, since, limit):
        return self.db.execute(
            "SELECT * FROM items WHERE status='product' AND notified_at IS NULL AND first_seen>=? "
            "ORDER BY first_seen LIMIT ?", (since, limit)).fetchall()

    def recent(self, n):
        return self.db.execute(
            "SELECT brand,title,price,currency,url,first_seen,notified_at,source FROM items "
            "WHERE status='product' ORDER BY first_seen DESC LIMIT ?", (n,)).fetchall()


# ════════════════════════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════════════════════════
class Monitor:
    def __init__(self, cfg: Config):
        self.cfg, self.s = cfg, cfg.s
        self.store = Store(cfg.db_path)
        self.http = Fetcher(self.s)
        self.notifier = Notifier(cfg.tg_token, cfg.tg_chats, self.http)

    def close(self):
        self.http.close()

    def brands(self, names=None):
        if not names:
            return [b for b in self.cfg.brands if b.get("enabled", True)]
        want = {n.lower() for n in names}
        found = [b for b in self.cfg.brands if b["name"].lower() in want]
        missing = want - {b["name"].lower() for b in found}
        if missing:
            print("配置里找不到这些品牌：", ", ".join(missing))
        return found

    # ---------- 过滤 ----------
    def passes(self, it) -> bool:
        text = it.get("filter_text") or it.get("title") or ""
        if self.cfg.include_re and not self.cfg.include_re.search(text):
            return False
        if self.cfg.exclude_re and self.cfg.exclude_re.search(text):
            return False
        g = it.get("gender", "unisex")
        if (self.cfg.gender == "men" and g == "women") or (self.cfg.gender == "women" and g == "men"):
            return False
        if self.s.get("only_my_sizes") and self.cfg.my_sizes and not size_hits(it.get("sizes_in") or [], self.cfg.my_sizes):
            return False
        return True

    # ---------- 抓法选择 ----------
    def method_for(self, b) -> str:
        m = str(b.get("method") or "auto").lower()
        if m == "shopify":
            return "shopify"
        if m in ("web", "sitemap"):
            return "web"
        cached = self.store.get(b["name"], "method")
        if cached:
            return cached
        d = shopify_detect(self.http, b)
        if d is None:
            return "web"  # 检测被拦：本轮先按网页方式，下轮再测
        m = "shopify" if d else "web"
        self.store.set(b["name"], "method", m)
        log.info("[%s] 识别为：%s", b["name"], "Shopify" if d else "普通网页（New In + sitemap）")
        return m

    def check_brand(self, b):
        if self.method_for(b) == "shopify":
            try:
                self._check_shopify(b)
            except FetchError as e:
                if not e.blocked and str(b.get("method") or "auto").lower() == "auto":
                    self.store.set(b["name"], "method", "")  # 下轮重新识别
                raise
        else:
            self._check_web(b)

    def _check_shopify(self, b):
        name = b["name"]
        prods = shopify_products(self.http, b)
        cur = self.store.get(name, "currency")
        if cur is None:
            cur = shopify_currency(self.http, b) or ""
            self.store.set(name, "currency", cur)
        items = [shopify_item(p, b, cur) for p in prods if p.get("id")]
        known = self.store.keys(name)
        fresh = [it for it in items if it["key"] not in known]
        if not self.store.get(name, "baseline:shopify"):
            self.store.add_many(name, [(it["key"], it["url"]) for it in fresh], "baseline", "shopify")
            self.store.set(name, "baseline:shopify", now_iso())
            log.info("[%s] 首次运行：记录现有 %d 件商品作为基线（不推送）", name, len(items))
            return
        if len(fresh) > int(self.s["max_new_urls_per_cycle"]):
            self._structural(name, "shopify", [(it["key"], it["url"]) for it in fresh])
            return
        if fresh:
            log.info("[%s] 发现 %d 件新品", name, len(fresh))
        self._deliver(b, fresh)

    def _new_in_pages(self, b):
        v = b.get("new_in", "auto")
        name = b["name"]
        if isinstance(v, str) and v.lower() == "auto":
            cached, ts = self.store.get(name, "new_in_pages"), self.store.get(name, "new_in_at")
            if cached is not None and ts and (datetime.now() - datetime.fromisoformat(ts)).total_seconds() < 86400:
                return json.loads(cached)
            try:
                pages = discover_new_in(self.http, b, self.cfg)
            except FetchError:
                return json.loads(cached) if cached else []
            self.store.set(name, "new_in_pages", json.dumps(pages))
            self.store.set(name, "new_in_at", now_iso())
            if pages:
                log.info("[%s] 自动找到 New In 页：%s", name, " | ".join(pages))
            return pages
        return [urljoin(b["url"] + "/", str(u)) for u in as_list(v)]

    def _check_web(self, b):
        name = b["name"]
        found, errors, attempted = {}, [], False
        pages = self._new_in_pages(b)
        if pages:
            attempted = True
            urls = set()
            for p in pages:
                try:
                    urls |= listing_links(self.http, p, b, self.cfg)
                except FetchError as e:
                    errors.append(e)
            if urls:
                found["new_in"] = urls
        if b.get("sitemap", True) is not False:
            last = self.store.get(name, "sitemap_at")
            due = not last or (datetime.now() - datetime.fromisoformat(last)).total_seconds() >= \
                float(self.s["sitemap_interval_minutes"]) * 60
            if due:
                attempted = True
                try:
                    found["sitemap"] = sitemap_urls(self.http, b, self.cfg)
                    self.store.set(name, "sitemap_at", now_iso())
                except FetchError as e:
                    errors.append(e)
        if attempted and not any(found.values()):
            if errors:
                raise errors[0]
            raise FetchError("New In 页和 sitemap 都没找到商品链接，跑一下 probe 看看")

        known = self.store.keys(name)
        for src, urls in found.items():
            fresh = sorted(u for u in urls if u not in known)
            if not self.store.get(name, f"baseline:{src}"):
                self.store.add_many(name, [(u, u) for u in fresh], "baseline", src)
                self.store.set(name, f"baseline:{src}", now_iso())
                log.info("[%s] 首次运行（%s）：记录现有 %d 个商品链接作为基线（不推送）", name, src, len(urls))
            elif len(fresh) > int(self.s["max_new_urls_per_cycle"]):
                self._structural(name, src, [(u, u) for u in fresh])
            elif fresh:
                self.store.add_many(name, [(u, u) for u in fresh], "pending", src)
                log.info("[%s] %s 出现 %d 个新链接，去读商品页", name, src, len(fresh))
            known |= set(fresh)

        ready = []
        for row in self.store.pending(name, int(self.s["max_new_urls_per_cycle"])):
            try:
                it = fetch_product(self.http, row["url"], b)
            except FetchError as e:
                log.info("[%s] 商品页暂时读不了，下轮重试：%s（%s）", name, row["url"], e)
                self.store.bump(name, row["key"])
                continue
            if it is None:
                self.store.set_status(name, row["key"], "nonproduct")
                continue
            it["key"], it["source"] = row["key"], row["source"]
            ready.append(it)
        self._deliver(b, ready)

    def _deliver(self, b, items):
        name = b["name"]
        to_send = []
        for it in items:
            if self.passes(it):
                self.store.save_item(name, it, "product")
                to_send.append(it)
            else:
                self.store.save_item(name, it, "filtered")
                log.info("[%s] 过滤掉（不符合你的条件）：%s", name, it.get("title"))
        if not to_send:
            return
        if len(to_send) <= int(self.s["max_cards_per_brand"]):
            for it in to_send:
                if self.notifier.send_item(it, self.cfg.my_sizes):
                    self.store.mark_notified(name, it["key"])
                    log.info("[%s] 已推送：%s  %s", name, it.get("title"), fmt_price(it.get("price"), it.get("currency")))
                else:
                    log.warning("[%s] 推送没发出去，下轮自动补发：%s", name, it.get("title"))
        else:
            ok = all(self.notifier.send_text(m) for m in digest_messages(name, to_send))
            if ok:
                for it in to_send:
                    self.store.mark_notified(name, it["key"])
            log.info("[%s] 一次 %d 件新品，已发汇总清单", name, len(to_send))

    def _structural(self, name, src, pairs):
        self.store.add_many(name, pairs, "baseline", src)
        log.warning("[%s] %s 一次出现 %d 个新链接，疑似网站改版，静默记录", name, src, len(pairs))
        self.notifier.send_text(
            f"⚠️ <b>{htmllib.escape(name)}</b>：{src} 一次冒出 {len(pairs)} 个新链接，像是网站改版或链接格式变了。"
            f"已静默记录、没逐条推送。如果以后经常这样，跑一下 probe 检查配置。")

    # ---------- 一轮 / 常驻 ----------
    def run_cycle(self, names=None):
        t0 = time.time()
        for b in self.brands(names):
            name = b["name"]
            try:
                self.check_brand(b)
                fails = int(self.store.get(name, "fails") or 0)
                if fails >= int(self.s["fail_alert_after"]):
                    self.notifier.send_text(f"✅ <b>{htmllib.escape(name)}</b> 恢复正常了")
                self.store.set(name, "fails", 0)
                self.store.set(name, "last_ok", now_iso())
            except Exception as e:
                if not isinstance(e, FetchError):
                    log.exception("[%s] 程序出错", name)
                fails = int(self.store.get(name, "fails") or 0) + 1
                self.store.set(name, "fails", fails)
                self.store.set(name, "last_error", str(e)[:300])
                log.warning("[%s] 本轮失败（连续 %d 次）：%s", name, fails, e)
                if fails == int(self.s["fail_alert_after"]):
                    hint = "；可以在 config.yaml 给它加 browser: true（需装 playwright）" \
                        if getattr(e, "blocked", False) and not b.get("browser") else ""
                    self.notifier.send_text(f"⚠️ <b>{htmllib.escape(name)}</b> 已连续 {fails} 轮抓取失败："
                                            f"{htmllib.escape(str(e)[:200])}{hint}")
        self.retry_unsent()
        log.info("本轮完成，用时 %.0f 秒", time.time() - t0)

    def retry_unsent(self):
        """之前因为断网等原因没推出去的上新（24 小时内），这轮补发。"""
        if not self.notifier.enabled:
            return
        since = datetime.fromtimestamp(time.time() - 86400).isoformat(timespec="seconds")
        for r in self.store.unsent(since, 20):
            it = {"key": r["key"], "brand": r["brand"], "url": r["url"], "title": r["title"], "price": r["price"],
                  "currency": r["currency"], "image": r["image"], "season": r["season"],
                  "sizes_in": json.loads(r["sizes_in"] or "[]")}
            if not self.notifier.send_item(it, self.cfg.my_sizes):
                break  # 还是发不出去，下轮再说
            self.store.mark_notified(r["brand"], r["key"])
            log.info("[%s] 补发成功：%s", r["brand"], r["title"])

    def run_forever(self):
        n = len(self.brands())
        interval = float(self.s["interval_minutes"]) * 60
        log.info("开始监控 %d 个品牌，每 %s 分钟一轮。Ctrl+C 停止。", n, self.s["interval_minutes"])
        if not self.notifier.enabled:
            log.warning("没填 Telegram bot_token / chat_id，上新只会打印在这个窗口里")
        else:
            self.notifier.send_text(f"▶️ 上新监控已启动：{n} 个品牌，每 {self.s['interval_minutes']} 分钟检查一次")
        while True:
            start = time.time()
            self.run_cycle()
            wait = max(30.0, interval * random.uniform(0.85, 1.15) - (time.time() - start))
            log.info("下一轮在 %.0f 秒后", wait)
            time.sleep(wait)

    # ---------- demo：推最新 2 件看效果 ----------
    def demo(self, names):
        for b in self.brands(names):
            print(f"━━ {b['name']}")
            try:
                items = []
                if self.method_for(b) == "shopify":
                    cur = shopify_currency(self.http, b) or ""
                    prods = shopify_products(self.http, b, first_page_only=True)
                    prods.sort(key=lambda p: p.get("published_at") or p.get("created_at") or "", reverse=True)
                    items = [shopify_item(p, b, cur) for p in prods]
                    items = [it for it in items if self.passes(it)][:2]
                else:
                    urls = []
                    for p in self._new_in_pages(b):
                        urls += sorted(listing_links(self.http, p, b, self.cfg))
                    if not urls:
                        urls = sorted(sitemap_urls(self.http, b, self.cfg))[:20]
                    for u in urls[:12]:
                        it = fetch_product(self.http, u, b)
                        if it and self.passes(it):
                            items.append(it)
                        if len(items) >= 2:
                            break
                if not items:
                    print("  没取到商品，先跑 probe 看看是哪一步不行")
                for it in items:
                    self.notifier.send_item(it, self.cfg.my_sizes)
                    print("  已发送：", it["title"])
            except FetchError as e:
                print("  失败：", e)

    # ---------- probe：体检每个品牌 ----------
    def probe(self, names=None, force_browser=False):
        summary = []
        for b in self.brands(names):
            if force_browser:
                b = dict(b, browser=True)
            name = b["name"]
            print(f"\n━━ {name}  {b['url']}" + ("  [浏览器模式]" if b.get("browser") else ""))
            verdict, how, note, blocked = "❌", "-", "", False
            try:
                d = shopify_detect(self.http, b) if str(b.get("method") or "auto").lower() in ("auto", "shopify") else False
                if d:
                    cur = shopify_currency(self.http, b) or "?"
                    prods = shopify_products(self.http, b, first_page_only=True)
                    print(f"  ✔ Shopify：products.json 可读，第一页 {len(prods)} 件，货币 {cur}")
                    prods.sort(key=lambda p: p.get("published_at") or "", reverse=True)
                    if prods:
                        it = shopify_item(prods[0], b, cur)
                        print(f"  最新一件：{it['title']} | {fmt_price(it['price'], cur)} | "
                              f"有货码 {', '.join(it['sizes_in'][:10]) or '-'} | 图 {'有' if it['image'] else '无'}")
                    verdict, how = "✅", "Shopify"
                    note = f"{len(prods)}{'+' if len(prods) == 250 else ''} 件，{cur}"
                else:
                    print("  · 不是 Shopify（或 products.json 不开放）" if d is False else "  · Shopify 检测请求失败/被拦")
                    samples, n_sm, new_in_all = [], 0, set()
                    try:
                        pages = self._new_in_pages(b) if not (isinstance(b.get("new_in", "auto"), str) and
                                                              str(b.get("new_in", "auto")).lower() == "auto") \
                            else discover_new_in(self.http, b, self.cfg)
                        print(f"  New In 页：{' | '.join(pages) if pages else '没自动找到（可在配置里手填 new_in）'}")
                        for p in pages[:3]:
                            try:
                                links = sorted(listing_links(self.http, p, b, self.cfg))
                                new_in_all |= set(links)
                                print(f"    {p} → {len(links)} 个商品链接" + (f"，例：{links[0]}" if links else ""))
                                samples += links[:2]
                            except FetchError as e:
                                blocked |= e.blocked
                                print(f"    {p} → 失败：{e}")
                    except FetchError as e:
                        blocked |= e.blocked
                        print(f"  首页读取失败：{e}")
                    if b.get("sitemap", True) is not False:
                        dbg = []
                        try:
                            sm = sorted(sitemap_urls(self.http, b, self.cfg, debug=dbg))
                            n_sm = len(sm)
                            for ln in dbg[:6]:
                                print("  sitemap · " + ln)
                            print(f"  sitemap 过滤后商品链接：{n_sm} 个" + (f"，例：{sm[0]}" if sm else
                                  "（0 个：多半是 url_include 写错了，看上面的链接长什么样再改）"))
                            samples += sm[len(sm) // 2: len(sm) // 2 + 2]
                        except FetchError as e:
                            blocked |= e.blocked
                            for ln in dbg[:4]:
                                print("  sitemap · " + ln)
                            print(f"  sitemap 失败：{e}")
                    cands = dedupe([c for c in samples if c])[:4]
                    if cands:
                        last_err, it = None, None
                        for c in cands:  # 链接里可能混着专题页，多试几个
                            try:
                                it = fetch_product(self.http, c, b)
                            except FetchError as e:
                                blocked |= e.blocked
                                last_err = e
                                it = None
                            if it:
                                break
                        if it:
                            print(f"  商品页解析：{it['title']} | {fmt_price(it['price'], it['currency'])} | "
                                  f"有货码 {', '.join(it['sizes_in'][:10]) or '-'} | 图 {'有' if it['image'] else '无'}")
                            verdict, how = "✅", "网页"
                            note = f"New In {len(new_in_all)} 链接 / sitemap {n_sm} 链接"
                            if not it["sizes_in"] and not it["sizes_out"]:
                                note += "（页面没给码数）"
                        elif last_err:
                            print(f"  商品页读取失败：{last_err}")
                            verdict, how, note = "⚠️", "网页", f"链接有了，商品页失败：{last_err}"
                        else:
                            print("  商品页解析：试了几个链接都没识别出商品信息（可能是前端渲染，试试 --browser）")
                            verdict, how, note = "⚠️", "网页", "能拿到链接，但商品页解析不出"
                    elif not note:
                        note = "没拿到任何商品链接"
                    if blocked and not b.get("browser"):
                        note += "；被拦了 → 试试 python monitor.py probe \"%s\" --browser" % name
            except FetchError as e:
                note = str(e)
            summary.append((name, verdict, how, note))
        print("\n" + "═" * 60 + "\n体检结果：")
        for name, v, how, note in summary:
            print(f"  {v} {name:<20} {how:<8} {note}")
        print("\n✅ = 能用；⚠️ = 部分能用；❌ = 不行。改完 config.yaml 可以只测单个品牌：python monitor.py probe \"品牌名\"")


# ════════════════════════════════════════════════════════════════════
# 命令行
# ════════════════════════════════════════════════════════════════════
def setup_logging(path: Path):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%m-%d %H:%M:%S")
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    try:
        fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="奢侈品官网上新监控")
    ap.add_argument("-c", "--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="体检：每个品牌能不能抓、用哪种抓法")
    p.add_argument("brands", nargs="*")
    p.add_argument("--browser", action="store_true", help="用真浏览器（Playwright）再试")
    sub.add_parser("demo", help="推送某品牌最新 2 件，看看效果").add_argument("brands", nargs="+")
    sub.add_parser("once", help="跑一轮").add_argument("brands", nargs="*")
    sub.add_parser("run", help="常驻运行")
    sub.add_parser("test-notify", help="发测试消息")
    sub.add_parser("chat-id", help="查 Telegram chat_id")
    r = sub.add_parser("recent", help="最近的上新记录")
    r.add_argument("n", nargs="?", type=int, default=20)
    args = ap.parse_args()

    cfg = Config(args.config)
    setup_logging(cfg.log_path)

    if args.cmd == "chat-id":
        if not cfg.tg_token:
            sys.exit("先在 config.yaml 的 telegram.bot_token 填上 BotFather 给你的 token")
        n = Notifier(cfg.tg_token, [], None)
        res = n._post("getUpdates", {})
        chats = {}
        for u in res.get("result") or []:
            msg = u.get("message") or u.get("channel_post") or {}
            c = msg.get("chat") or {}
            if c.get("id"):
                chats[c["id"]] = c.get("title") or c.get("username") or c.get("first_name") or ""
        if not chats:
            print("没查到。请先在 Telegram 里打开你的 bot，发一句任意的话，再运行一次。", res.get("description", ""))
        for cid, nm in chats.items():
            print(f"chat_id: {cid}   ({nm})  ← 把这个数字填进 config.yaml 的 telegram.chat_id")
        return

    mon = Monitor(cfg)
    try:
        if args.cmd == "probe":
            mon.probe(args.brands, args.browser)
        elif args.cmd == "demo":
            mon.demo(args.brands)
        elif args.cmd == "once":
            mon.run_cycle(args.brands)
        elif args.cmd == "run":
            mon.run_forever()
        elif args.cmd == "test-notify":
            if not mon.notifier.enabled:
                print("还没填 telegram.bot_token 和 telegram.chat_id，见 README 第 2 步")
            elif mon.notifier.send_text("✅ 测试成功！上新监控能给你发消息了。"):
                print("已发送，去 Telegram 看看")
        elif args.cmd == "recent":
            rows = mon.store.recent(args.n)
            if not rows:
                print("还没有推送过的上新记录")
            for row in rows:
                print(f"{row['first_seen'].replace('T', ' ')}  [{row['brand']}] {row['title']}  "
                      f"{fmt_price(row['price'], row['currency'])}  ({row['source']})\n    {row['url']}")
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        mon.close()


if __name__ == "__main__":
    main()
