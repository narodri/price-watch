#!/usr/bin/env python3
"""
Ulike AirPro S 価格ウォッチャー (楽天市場 / Yahoo!ショッピング)

商品名に埋め込まれたクーポン価格を「実質価格」として拾い、
**歴代最安値 (ATL) を更新したときだけ** Gmail で通知する。

    python watch.py                 # 通常実行
    python watch.py --dry-run       # メール送信も state/history 更新もしない
    python watch.py --show-atl      # 現在の ATL を表示
    python watch.py --reset-atl     # ATL を削除 (次回実行で再設定)
    python watch.py --set-atl 29800 # ATL を手動指定
    python watch.py --test-mail     # 疎通確認メールのみ
    python watch.py --selftest      # ネットワーク不要の内部テスト
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))
HISTORY_PATH = Path(os.environ.get("HISTORY_PATH", ROOT / "docs" / "history.jsonl"))

# --- 定数 (SPEC 5.2) -------------------------------------------------------
MIN_DROP_YEN = 500      # この幅未満の更新はメールを送らない
SANE_MIN = 18_000       # 実質価格の下限 (誤検出ガード)
SANE_MAX = 80_000       # 実質価格の上限
TIMEOUT_SEC = 20
RETRY = 2               # 初回 + リトライ2回、バックオフ 2s -> 4s
RAKUTEN_INTERVAL_SEC = 1.0   # 楽天 API は 1 秒 1 リクエスト

NO_RESULT_STREAK_LIMIT = 3        # 3 回連続で候補 0 件なら警告
NO_COUPON_DAYS_LIMIT = 7          # 7 日連続でクーポン解析 0 件なら警告
ALERT_COOLDOWN_DAYS = 7           # 警告メールのクールダウン

# --- 監視対象 (SPEC 1.1: 単一だが構造は配列で保持) -------------------------
TARGETS = [
    {
        "key": "ulike-airpro-s",
        "label": "Ulike AirPro S",
        "keyword": "Ulike AirPro S 光美容器",
        "list_price": 49_800,
        "must_include": ["AirPro S", "AirProS", "Air Pro S"],
        "must_exclude": [
            "カートリッジ", "替え", "交換", "ケース", "収納", "カバー",
            "フィルム", "保護", "中古", "未使用品", "訳あり", "ジャンク",
            "並行輸入", "レンタル",
        ],
    },
]

# 商品名からクーポン後価格を拾うパターン (SPEC 1.3)
COUPON_PATTERNS = [
    r"クーポン(?:利用|適用)?で\s*([\d,]+)\s*円",
    r"[→⇒➡]\s*([\d,]+)\s*円",
    r"([\d,]+)\s*円\s*!",          # NFKC 正規化後なので全角 ! も含む
    r"実質\s*([\d,]+)\s*円",
]

SITE_LABEL = {"rakuten": "楽天", "yahoo": "Yahoo"}


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------
@dataclass
class Offer:
    site: str            # "rakuten" / "yahoo"
    name: str
    list_price: int      # API が返した価格 (クーポン前)
    price: int           # 実質価格
    url: str
    shop: str
    coupon: bool         # 商品名からクーポン価格を拾えたか

    @property
    def site_label(self) -> str:
        return SITE_LABEL.get(self.site, self.site)


@dataclass
class SiteResult:
    site: str
    ok: bool = True
    error: str | None = None
    candidates: int = 0      # フィルタを通過した件数
    coupon_hits: int = 0     # うちクーポン価格を解析できた件数
    offers: list[Offer] = field(default_factory=list)

    def best(self) -> Offer | None:
        return min(self.offers, key=lambda o: o.price) if self.offers else None


# ---------------------------------------------------------------------------
# 文字列正規化・パース
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    """全角/半角・大小文字・空白の揺れを吸収した比較用の文字列。"""
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s　_/・･]+", "", text)


def extract_coupon_price(item_name: str) -> int | None:
    """商品名に埋め込まれたクーポン後価格。見つからなければ None。"""
    text = unicodedata.normalize("NFKC", item_name)
    candidates: list[int] = []
    for pattern in COUPON_PATTERNS:
        for raw in re.findall(pattern, text):
            try:
                value = int(raw.replace(",", ""))
            except ValueError:
                continue
            if SANE_MIN <= value <= SANE_MAX:
                candidates.append(value)
    return min(candidates) if candidates else None


def matches_target(item_name: str, target: dict) -> bool:
    """SPEC 4.2 の誤検出フィルタ (包含条件 / 除外条件)。"""
    name = normalize(item_name)
    includes = [normalize(tok) for tok in target.get("must_include") or []]
    if includes and not any(tok in name for tok in includes):
        return False
    for tok in target.get("must_exclude") or []:
        if normalize(tok) in name:
            return False
    return True


def build_offer(site: str, name: str, api_price: int, url: str, shop: str) -> Offer | None:
    """価格ガードを通ったら Offer を返す。外れていれば None。"""
    coupon = extract_coupon_price(name)
    price = min(api_price, coupon) if coupon else api_price
    if not (SANE_MIN <= price <= SANE_MAX):
        return None
    return Offer(
        site=site,
        name=name,
        list_price=api_price,
        price=price,
        url=url,
        shop=shop,
        coupon=coupon is not None and coupon < api_price,
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def redact(text: str) -> str:
    """SPEC 4.7: 例外メッセージに API キー付き URL を出さない。"""
    text = re.sub(r"(applicationId|appid|Client-?Id)=[^&\s\"']+", r"\1=***", text, flags=re.I)
    return re.sub(r"\?[^\s\"']*", "?<redacted>", text)


def http_get_json(url: str, headers: dict | None = None) -> dict:
    last: Exception | None = None
    for attempt in range(RETRY + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - リトライして最後に投げ直す
            last = exc
            if attempt < RETRY:
                time.sleep(2 ** (attempt + 1))  # 2s -> 4s
    raise RuntimeError(redact(f"{type(last).__name__}: {last}"))


# ---------------------------------------------------------------------------
# 楽天市場 商品検索API v2
# ---------------------------------------------------------------------------
_last_rakuten_call = 0.0


def search_rakuten(target: dict, app_id: str, hits: int = 30) -> SiteResult:
    global _last_rakuten_call
    result = SiteResult(site="rakuten")
    endpoint = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
    params = {
        "applicationId": app_id,
        "keyword": target["keyword"],
        "hits": hits,
        "sort": "+itemPrice",
        "availability": 1,
        "format": "json",
    }

    wait = RAKUTEN_INTERVAL_SEC - (time.monotonic() - _last_rakuten_call)
    if wait > 0:
        time.sleep(wait)
    try:
        data = http_get_json(f"{endpoint}?{urllib.parse.urlencode(params)}")
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = redact(str(exc))
        return result
    finally:
        _last_rakuten_call = time.monotonic()

    for wrapper in data.get("Items", []):
        item = wrapper.get("Item", wrapper)
        name = item.get("itemName", "")
        if not matches_target(name, target):
            continue
        try:
            if int(item.get("availability", 1) or 0) != 1:   # 在庫ありのみ
                continue
        except (TypeError, ValueError):
            continue
        try:
            api_price = int(item.get("itemPrice") or 0)
        except (TypeError, ValueError):
            continue
        offer = build_offer("rakuten", name, api_price,
                            item.get("itemUrl", ""), item.get("shopName", ""))
        if offer is None:
            continue
        result.candidates += 1
        result.coupon_hits += int(offer.coupon)
        result.offers.append(offer)
    return result


# ---------------------------------------------------------------------------
# Yahoo!ショッピング 商品検索API v3
# ---------------------------------------------------------------------------
def search_yahoo(target: dict, client_id: str, hits: int = 30) -> SiteResult:
    result = SiteResult(site="yahoo")
    endpoint = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
    params = {
        "appid": client_id,
        "query": target["keyword"],
        "results": hits,
        "sort": "+price",
        "in_stock": "true",
        "condition": "new",
    }
    try:
        data = http_get_json(f"{endpoint}?{urllib.parse.urlencode(params)}")
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.error = redact(str(exc))
        return result

    for item in data.get("hits", []):
        name = item.get("name", "")
        if not matches_target(name, target):
            continue
        if item.get("inStock") is False:          # 在庫ありのみ
            continue
        if str(item.get("condition", "new")).lower() == "used":
            continue
        try:
            api_price = int(item.get("price") or 0)
        except (TypeError, ValueError):
            continue
        seller = item.get("seller") or {}
        offer = build_offer("yahoo", name, api_price,
                            item.get("url", ""), seller.get("name", ""))
        if offer is None:
            continue
        result.candidates += 1
        result.coupon_hits += int(offer.coupon)
        result.offers.append(offer)
    return result


# ---------------------------------------------------------------------------
# state / history
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] state.json を読めなかったので初期化します", file=sys.stderr)
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def append_history(now: datetime, results: list[SiteResult]) -> None:
    """実行あたりサイト別に最安 1 件だけ追記する (JSONL)。"""
    rows = []
    for res in results:
        best = res.best()
        if best is None:
            continue
        rows.append({
            "ts": now.isoformat(timespec="seconds"),
            "site": res.site,
            "price": best.price,
            "list": best.list_price,
            "shop": best.shop,
            "url": best.url,
            "name": best.name,
        })
    if not rows:
        return
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# メール
# ---------------------------------------------------------------------------
def send_mail(subject: str, body_text: str, body_html: str | None = None) -> None:
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to_addr = os.environ.get("MAIL_TO") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_atl_mail(target: dict, offer: Offer, prev_atl: int,
                   prev_at: str, now: datetime) -> tuple[str, str, str]:
    """SPEC 3.1: 商品名全文と URL を必ず載せて誤検出に気づけるようにする。"""
    drop = prev_atl - offer.price
    stamp = now.strftime("%Y-%m-%d %H:%M")
    subject = (f"【最安更新】{target['label']} {offer.price:,}円 "
               f"({offer.site_label}) ▼{drop:,}円")

    text = "\n".join([
        f"{target['label']} が歴代最安値を更新しました。",
        "",
        f"  実質価格 : {offer.price:,}円",
        f"  定価     : {target['list_price']:,}円 (API 価格 {offer.list_price:,}円)",
        f"  値引き幅 : -{target['list_price'] - offer.price:,}円 (定価比)",
        f"  前回 ATL : {prev_atl:,}円 ({prev_at})  ▼{drop:,}円",
        f"  サイト   : {offer.site_label}",
        f"  ストア   : {offer.shop}",
        f"  取得時刻 : {stamp} JST",
        "",
        "商品名 (全文):",
        f"  {offer.name}",
        "",
        f"URL: {offer.url}",
        "",
        "※ 商品名が本体でない (ケース/カートリッジ等) 場合は誤検出です。",
        "   その場合は `python watch.py --reset-atl` で ATL を作り直してください。",
    ])

    html = f"""<html><body style="font-family:sans-serif;font-size:14px">
<h2 style="margin:0 0 8px">{esc(target['label'])} 歴代最安更新</h2>
<p style="font-size:28px;margin:0 0 12px"><b>{offer.price:,}円</b>
<span style="font-size:14px;color:#666">({esc(offer.site_label)})</span></p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr><th align="left">定価</th><td>{target['list_price']:,}円</td></tr>
<tr><th align="left">API 価格</th><td>{offer.list_price:,}円</td></tr>
<tr><th align="left">前回 ATL</th><td>{prev_atl:,}円 ({esc(prev_at)}) ▼{drop:,}円</td></tr>
<tr><th align="left">ストア</th><td>{esc(offer.shop)}</td></tr>
<tr><th align="left">取得時刻</th><td>{stamp} JST</td></tr>
<tr><th align="left">商品名</th><td>{esc(offer.name)}</td></tr>
</table>
<p><a href="{esc(offer.url)}">{esc(offer.url)}</a></p>
<p style="color:#a00">商品名が本体でなければ誤検出です。
<code>python watch.py --reset-atl</code> で作り直してください。</p>
</body></html>"""
    return subject, text, html


def build_baseline_mail(target: dict, offer: Offer, now: datetime) -> tuple[str, str, str]:
    stamp = now.strftime("%Y-%m-%d %H:%M")
    subject = f"【初期設定】{target['label']} baseline {offer.price:,}円 ({offer.site_label})"
    text = "\n".join([
        "価格ウォッチャーの初回実行です。今回は baseline を記録するだけで、"
        "値下げ通知は送りません。",
        "",
        f"  現在の最安 : {offer.price:,}円 ({offer.site_label})",
        f"  定価       : {target['list_price']:,}円",
        f"  ストア     : {offer.shop}",
        f"  取得時刻   : {stamp} JST",
        "",
        "商品名 (全文):",
        f"  {offer.name}",
        "",
        f"URL: {offer.url}",
        "",
        f"次回以降、この価格を {MIN_DROP_YEN:,}円以上下回ったときに通知します。",
    ])
    html = f"""<html><body style="font-family:sans-serif;font-size:14px">
<h2 style="margin:0 0 8px">{esc(target['label'])} baseline 設定完了</h2>
<p style="font-size:28px;margin:0 0 12px"><b>{offer.price:,}円</b>
<span style="font-size:14px;color:#666">({esc(offer.site_label)})</span></p>
<p>初回実行のため通知は送りません。次回以降 {MIN_DROP_YEN:,}円以上の更新で通知します。</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr><th align="left">定価</th><td>{target['list_price']:,}円</td></tr>
<tr><th align="left">ストア</th><td>{esc(offer.shop)}</td></tr>
<tr><th align="left">取得時刻</th><td>{stamp} JST</td></tr>
<tr><th align="left">商品名</th><td>{esc(offer.name)}</td></tr>
</table>
<p><a href="{esc(offer.url)}">{esc(offer.url)}</a></p>
</body></html>"""
    return subject, text, html


def build_health_mail(title: str, detail: str, now: datetime) -> tuple[str, str, str]:
    stamp = now.strftime("%Y-%m-%d %H:%M")
    subject = f"【要点検】価格ウォッチャー: {title}"
    text = f"{title}\n\n{detail}\n\n検知時刻: {stamp} JST\n"
    html = (f"<html><body style='font-family:sans-serif;font-size:14px'>"
            f"<h2>{esc(title)}</h2>"
            f"<pre style='white-space:pre-wrap'>{esc(detail)}</pre>"
            f"<p>検知時刻: {stamp} JST</p></body></html>")
    return subject, text, html


# ---------------------------------------------------------------------------
# ヘルスチェック (SPEC 4.4)
# ---------------------------------------------------------------------------
def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def cooldown_ok(last: str | None, now: datetime) -> bool:
    last_dt = parse_ts(last)
    return last_dt is None or (now - last_dt) >= timedelta(days=ALERT_COOLDOWN_DAYS)


def check_health(health: dict, candidates: int, coupon_hits: int,
                 now: datetime) -> list[tuple[str, str]]:
    """state の health を更新し、送るべき警告のリストを返す。"""
    alerts: list[tuple[str, str]] = []

    if candidates == 0:
        health["no_result_streak"] = int(health.get("no_result_streak", 0)) + 1
    else:
        health["no_result_streak"] = 0

    if coupon_hits > 0:
        health["last_coupon_ok_ts"] = now.isoformat(timespec="seconds")
    health.setdefault("last_coupon_ok_ts", now.isoformat(timespec="seconds"))

    if health["no_result_streak"] >= NO_RESULT_STREAK_LIMIT and cooldown_ok(
        health.get("last_no_result_alert_ts"), now
    ):
        alerts.append((
            "検索結果 0 件が続いています",
            f"{health['no_result_streak']} 回連続で候補 0 件でした。\n"
            "検索キーワードが効かなくなったか、フィルタ (must_include / must_exclude) が\n"
            "厳しすぎる可能性があります。TARGETS の keyword と must_include を確認してください。",
        ))
        health["last_no_result_alert_ts"] = now.isoformat(timespec="seconds")

    last_ok = parse_ts(health.get("last_coupon_ok_ts"))
    if (
        coupon_hits == 0
        and last_ok is not None
        and (now - last_ok) >= timedelta(days=NO_COUPON_DAYS_LIMIT)
        and cooldown_ok(health.get("last_parse_alert_ts"), now)
    ):
        alerts.append((
            "クーポン価格の解析が止まっています",
            f"{NO_COUPON_DAYS_LIMIT} 日以上、商品名からクーポン価格を 1 件も抽出できていません。\n"
            f"最後に成功した時刻: {health.get('last_coupon_ok_ts')}\n"
            "セール自体がないだけの可能性もありますが、ストアの表記が変わって\n"
            "COUPON_PATTERNS が効かなくなっていないか確認してください。",
        ))
        health["last_parse_alert_ts"] = now.isoformat(timespec="seconds")

    return alerts


# ---------------------------------------------------------------------------
# 実行本体
# ---------------------------------------------------------------------------
def pad(text: str, width: int) -> str:
    """全角文字を 2 桁と数えて右詰めの空白を入れる (ログの桁揃え用)。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def log_run(target: dict, now: datetime, results: list[SiteResult],
            best: Offer | None, atl: dict | None) -> None:
    """SPEC 3.2 の実行ログ。候補件数と解析成功件数を必ず出す。"""
    print(f"[{now.strftime('%Y-%m-%d %H:%M')} JST] {target['label']}")
    for res in results:
        label = pad(SITE_LABEL.get(res.site, res.site), 6)
        if not res.ok:
            print(f"  {label}: API 失敗 ({res.error})")
            continue
        site_best = res.best()
        price = f"{site_best.price:,}円" if site_best else "該当なし"
        print(
            f"  {label}: 実質 {price} (定価 {target['list_price']:,}円) / "
            f"候補 {res.candidates:>2}件 / クーポン解析成功 {res.coupon_hits:>2}件"
        )
    if best is not None:
        print(f"  最安  : {best.price:,}円 ({best.site_label}) {best.shop}")
    else:
        print("  最安  : なし")
    if atl:
        print(f"  ATL   : {atl['price']:,}円 ({str(atl.get('ts', ''))[:10]})")
    else:
        print("  ATL   : 未設定")


def run(args: argparse.Namespace) -> int:
    rakuten_id = os.environ.get("RAKUTEN_APP_ID")
    yahoo_id = os.environ.get("YAHOO_CLIENT_ID")
    if not rakuten_id and not yahoo_id:
        print("RAKUTEN_APP_ID か YAHOO_CLIENT_ID のどちらかが必要です", file=sys.stderr)
        return 1

    now = datetime.now(JST)
    state = load_state()
    dirty = False
    mails: list[tuple[str, str, str]] = []

    for target in TARGETS:
        results: list[SiteResult] = []
        if rakuten_id:
            results.append(search_rakuten(target, rakuten_id))
        if yahoo_id:
            results.append(search_yahoo(target, yahoo_id))

        entry = state.setdefault(target["key"], {})
        atl = entry.get("atl")
        offers = [o for res in results for o in res.offers]
        best = min(offers, key=lambda o: o.price) if offers else None
        log_run(target, now, results, best, atl)

        # SPEC 4.3: 両方失敗した回は何も記録せず終了
        if all(not res.ok for res in results):
            print("  -> 全サイトで API 失敗。今回は記録しません。")
            continue

        candidates = sum(res.candidates for res in results if res.ok)
        coupon_hits = sum(res.coupon_hits for res in results if res.ok)
        health = entry.setdefault("health", {})
        for title, detail in check_health(health, candidates, coupon_hits, now):
            mails.append(build_health_mail(f"{target['label']} — {title}", detail, now))
        dirty = True

        if best is None:
            print("  -> 候補 0 件。ATL は据え置き。")
            continue

        if not args.dry_run:
            append_history(now, results)

        if atl is None:
            # SPEC 2.2 コールドスタート: baseline のみ記録し値下げ通知はしない
            entry["atl"] = {
                "price": best.price, "site": best.site,
                "ts": now.isoformat(timespec="seconds"),
                "name": best.name, "url": best.url, "shop": best.shop,
            }
            entry["baseline_at"] = now.isoformat(timespec="seconds")
            print(f"  -> baseline を {best.price:,}円 で設定しました (値下げ通知なし)")
            mails.append(build_baseline_mail(target, best, now))
            continue

        if best.price >= atl["price"]:
            print("  -> 更新なし")
            continue

        drop = atl["price"] - best.price
        prev_price, prev_at = atl["price"], str(atl.get("ts", ""))[:10]
        entry["atl"] = {
            "price": best.price, "site": best.site,
            "ts": now.isoformat(timespec="seconds"),
            "name": best.name, "url": best.url, "shop": best.shop,
            "prev_price": prev_price,
        }
        if drop >= MIN_DROP_YEN:
            print(f"  -> ATL 更新 {prev_price:,}円 -> {best.price:,}円 "
                  f"(▼{drop:,}円) 通知します")
            mails.append(build_atl_mail(target, best, prev_price, prev_at, now))
        else:
            # SPEC 2.3: 更新はするがメールは出さない
            print(f"  -> ATL 更新 {prev_price:,}円 -> {best.price:,}円 "
                  f"(▼{drop:,}円) < {MIN_DROP_YEN:,}円 のため通知は抑制")

    if args.dry_run:
        print("--- DRY RUN: メール送信も state/history 更新も行いません ---")
        for subject, text, _ in mails:
            print(f"\n[MAIL] {subject}\n{text}")
        return 0

    for subject, text, html in mails:
        try:
            send_mail(subject, text, html)
            print(f"送信しました: {subject}")
        except Exception as exc:  # noqa: BLE001 - 送信失敗で state を落とさない
            print(f"[warn] メール送信失敗: {redact(str(exc))}", file=sys.stderr)

    if dirty:
        save_state(state)
    return 0


# ---------------------------------------------------------------------------
# ATL 手動操作 (SPEC 2.4)
# ---------------------------------------------------------------------------
def show_atl() -> int:
    state = load_state()
    for target in TARGETS:
        atl = state.get(target["key"], {}).get("atl")
        if not atl:
            print(f"{target['label']}: ATL 未設定")
            continue
        site = SITE_LABEL.get(atl.get("site"), atl.get("site"))
        print(f"{target['label']}: {atl['price']:,}円 ({site}, {str(atl.get('ts', ''))[:10]})")
        print(f"  {atl.get('shop', '')} / {atl.get('name', '')}")
        print(f"  {atl.get('url', '')}")
    return 0


def reset_atl() -> int:
    state = load_state()
    for target in TARGETS:
        entry = state.get(target["key"])
        if not entry:
            continue
        entry.pop("atl", None)
        entry.pop("baseline_at", None)
    save_state(state)
    print("ATL を削除しました。次回実行で baseline を再設定します。")
    return 0


def set_atl(price: int) -> int:
    if not (SANE_MIN <= price <= SANE_MAX):
        print(f"{price:,}円 は {SANE_MIN:,}〜{SANE_MAX:,}円 の範囲外です", file=sys.stderr)
        return 1
    state = load_state()
    now = datetime.now(JST)
    for target in TARGETS:
        entry = state.setdefault(target["key"], {})
        entry["atl"] = {"price": price, "site": "manual",
                        "ts": now.isoformat(timespec="seconds"),
                        "name": "(手動設定)", "url": "", "shop": ""}
        entry["baseline_at"] = entry.get("baseline_at") or now.isoformat(timespec="seconds")
    save_state(state)
    print(f"ATL を {price:,}円 に設定しました。")
    return 0


# ---------------------------------------------------------------------------
# 内部テスト (ネットワーク不要)
# ---------------------------------------------------------------------------
def selftest() -> int:
    target = TARGETS[0]
    failures: list[str] = []

    def check(label: str, got, want) -> None:
        if got != want:
            failures.append(f"{label}: got={got!r} want={want!r}")

    # クーポン価格の抽出
    check("coupon/クーポンで", extract_coupon_price(
        "＼クーポンで29,120円！7/27 19:00-7/30／Ulike 公式 IPL光美容器 AirPro S"), 29120)
    check("coupon/矢印", extract_coupon_price(
        "【46%OFFクーポンで49,800円 → 26,892円！7/4〜7/12】Ulike 公式"), 26892)
    check("coupon/実質", extract_coupon_price(
        "Ulike AirPro S 実質 27,800円 光美容器"), 27800)
    check("coupon/なし", extract_coupon_price(
        "Ulike 公式 IPL光美容器 AirPro S 本体"), None)
    check("coupon/下限外", extract_coupon_price("専用ケース クーポンで3,000円！"), None)

    # 表記ゆれの吸収
    for name in ["Ulike AirPro S 本体", "Ulike AirProS 本体", "Ulike Air Pro S 本体",
                 "Ｕｌｉｋｅ　ＡｉｒＰｒｏ　Ｓ 本体", "ulike airpro s"]:
        if not matches_target(name, target):
            failures.append(f"must_include で落ちた: {name}")
    for name in ["Ulike AirPro S 専用カートリッジ", "AirPro S 収納ケース",
                 "AirProS 中古 美品", "Air Pro S 並行輸入品"]:
        if matches_target(name, target):
            failures.append(f"must_exclude をすり抜けた: {name}")
    if matches_target("Ulike Air 10 Max 光美容器", target):
        failures.append("別モデルが混入した: Air 10 Max")

    # 実質価格の決定と価格ガード
    offer = build_offer("rakuten", "【クーポンで26,892円！】Ulike AirPro S", 49800, "u", "s")
    check("offer/実質価格", offer.price if offer else None, 26892)
    check("offer/クーポンフラグ", offer.coupon if offer else None, True)
    check("offer/下限ガード",
          build_offer("rakuten", "Ulike AirPro S 交換用パーツ", 3000, "u", "s"), None)
    check("offer/上限ガード",
          build_offer("rakuten", "Ulike AirPro S 3台セット", 120000, "u", "s"), None)

    # 例外メッセージの秘匿
    leaked = redact(
        "HTTP Error 429: https://app.rakuten.co.jp/x?applicationId=SECRET123&keyword=a")
    if "SECRET123" in leaked:
        failures.append(f"API キーが漏れている: {leaked}")

    # ヘルスチェック
    now = datetime.now(JST)
    health: dict = {}
    for _ in range(NO_RESULT_STREAK_LIMIT - 1):
        check("health/早すぎる警告", check_health(health, 0, 0, now), [])
    if not check_health(health, 0, 0, now):
        failures.append("health: 3 回連続 0 件で警告が出なかった")
    check("health/クールダウン", check_health(health, 0, 0, now), [])
    stale = {"last_coupon_ok_ts": (now - timedelta(days=8)).isoformat(timespec="seconds")}
    if not any("解析" in title for title, _ in check_health(stale, 5, 0, now)):
        failures.append("health: 7 日超のクーポン解析停止で警告が出なかった")

    for line in failures:
        print(f"FAIL {line}")
    print(f"selftest: {'OK' if not failures else str(len(failures)) + ' failed'}")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Ulike AirPro S 価格ウォッチャー")
    parser.add_argument("--dry-run", action="store_true",
                        help="メールを送らず state/history も更新しない")
    parser.add_argument("--show-atl", action="store_true", help="現在の ATL を表示")
    parser.add_argument("--reset-atl", action="store_true", help="ATL を削除")
    parser.add_argument("--set-atl", type=int, metavar="YEN", help="ATL を手動指定")
    parser.add_argument("--test-mail", action="store_true", help="疎通確認メールのみ送信")
    parser.add_argument("--selftest", action="store_true", help="ネットワーク不要の内部テスト")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if args.show_atl:
        return show_atl()
    if args.reset_atl:
        return reset_atl()
    if args.set_atl is not None:
        return set_atl(args.set_atl)
    if args.test_mail:
        send_mail("【テスト】価格ウォッチャー疎通確認",
                  f"送信できています。\n{datetime.now(JST).isoformat(timespec='seconds')}")
        print("test mail sent")
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
