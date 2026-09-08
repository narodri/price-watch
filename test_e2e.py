"""watch.py の実行フローをネットワークなしで通す E2E テスト。

API レスポンスを差し替えて、ATL 更新 / コールドスタート / 誤検出フィルタ /
API 障害 / --dry-run / ヘルス警告 の分岐を実際に走らせる。

    python test_e2e.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="price-watch-e2e-"))
os.environ["STATE_PATH"] = str(WORK / "state.json")
os.environ["HISTORY_PATH"] = str(WORK / "docs" / "history.jsonl")
os.environ["RAKUTEN_APP_ID"] = "dummy-rakuten"
os.environ["YAHOO_CLIENT_ID"] = "dummy-yahoo"
os.environ["GMAIL_USER"] = "x@example.com"
os.environ["GMAIL_APP_PASSWORD"] = "dummy"

spec = importlib.util.spec_from_file_location("watch", Path(__file__).with_name("watch.py"))
watch = importlib.util.module_from_spec(spec)
sys.modules["watch"] = watch
spec.loader.exec_module(watch)
watch.RAKUTEN_INTERVAL_SEC = 0        # テストでは待たない

SENT: list[str] = []
watch.send_mail = lambda subject, text, html=None: SENT.append(subject)

STATE = Path(os.environ["STATE_PATH"])
HIST = Path(os.environ["HISTORY_PATH"])
fails: list[str] = []


def rakuten_payload(name: str, price: int, availability: int = 1) -> dict:
    return {"Items": [{"Item": {"itemName": name, "itemPrice": price,
                                "availability": availability,
                                "itemUrl": "https://item.rakuten.co.jp/x/",
                                "shopName": "ULIKE CARE"}}]}


def yahoo_payload(name: str, price: int, in_stock: bool = True) -> dict:
    return {"hits": [{"name": name, "price": price, "inStock": in_stock,
                      "condition": "new",
                      "url": "https://store.shopping.yahoo.co.jp/x/",
                      "seller": {"name": "Ulike公式"}}]}


def stub(rakuten=None, yahoo=None, fail_rakuten=False, fail_yahoo=False) -> None:
    def _get(url, headers=None):
        if "rakuten" in url:
            if fail_rakuten:
                raise RuntimeError("HTTP Error 429: ?<redacted>")
            return rakuten or {"Items": []}
        if fail_yahoo:
            raise RuntimeError("HTTP Error 500: ?<redacted>")
        return yahoo or {"hits": []}
    watch.http_get_json = _get


def run(dry: bool = False) -> tuple[int, list[str]]:
    SENT.clear()
    return watch.run(Namespace(dry_run=dry)), list(SENT)


def state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def history() -> list[dict]:
    if not HIST.exists():
        return []
    return [json.loads(line) for line in HIST.read_text(encoding="utf-8").splitlines()]


def check(label: str, got, want) -> None:
    if got != want:
        fails.append(f"{label}: got={got!r} want={want!r}")


NAME_LIST = "Ulike 公式 IPL光美容器 AirPro S 本体"
NAME_COUPON = "＼クーポンで{}円！9/1-9/5／Ulike 公式 IPL光美容器 AirPro S 本体"

print("== 1. 初回実行 → baseline のみ、値下げ通知なし ==")
stub(rakuten_payload(NAME_LIST, 49800), yahoo_payload(NAME_LIST, 48000))
code, sent = run()
check("1/exit", code, 0)
check("1/メール数", len(sent), 1)
check("1/baseline メール", "baseline" in sent[0], True)
check("1/ATL", state()["ulike-airpro-s"]["atl"]["price"], 48000)
check("1/history 行数", len(history()), 2)
check("1/history サイト", sorted(r["site"] for r in history()), ["rakuten", "yahoo"])

print("== 2. 微更新 (300円) → ATL は更新、メールは抑制 ==")
stub(rakuten_payload(NAME_COUPON.format("47,700"), 49800), yahoo_payload(NAME_LIST, 48000))
code, sent = run()
check("2/メール数", len(sent), 0)
check("2/ATL", state()["ulike-airpro-s"]["atl"]["price"], 47700)

print("== 3. 大幅更新 → 通知 ==")
stub(rakuten_payload(NAME_COUPON.format("26,892"), 49800), yahoo_payload(NAME_LIST, 48000))
code, sent = run()
check("3/メール数", len(sent), 1)
check("3/件名", sent[0], "【最安更新】Ulike AirPro S 26,892円 (楽天) ▼20,808円")
check("3/ATL", state()["ulike-airpro-s"]["atl"]["price"], 26892)
check("3/採用サイト", state()["ulike-airpro-s"]["atl"]["site"], "rakuten")

print("== 4. 値上がり → 更新なし ==")
stub(rakuten_payload(NAME_LIST, 49800), yahoo_payload(NAME_LIST, 48000))
code, sent = run()
check("4/メール数", len(sent), 0)
check("4/ATL 据え置き", state()["ulike-airpro-s"]["atl"]["price"], 26892)

print("== 5. 誤検出フィルタ (ケース 3,000円 / 在庫なし 20,000円) ==")
stub(rakuten_payload("Ulike AirPro S 専用 収納ケース", 3000),
     yahoo_payload("Ulike AirPro S 本体 特価", 20000, in_stock=False))
before = len(history())
code, sent = run()
check("5/メール数", len(sent), 0)
check("5/ATL 汚染なし", state()["ulike-airpro-s"]["atl"]["price"], 26892)
check("5/history 追記なし", len(history()), before)
check("5/候補0件カウント", state()["ulike-airpro-s"]["health"]["no_result_streak"], 1)

print("== 6. 両サイト API 失敗 → 何も記録しない ==")
snap_state, snap_hist = STATE.read_text(encoding="utf-8"), len(history())
stub(fail_rakuten=True, fail_yahoo=True)
code, sent = run()
check("6/exit", code, 0)
check("6/メール数", len(sent), 0)
check("6/state 不変", STATE.read_text(encoding="utf-8"), snap_state)
check("6/history 不変", len(history()), snap_hist)

print("== 7. 片側だけ失敗 → もう片方で継続 ==")
stub(yahoo=yahoo_payload(NAME_COUPON.format("25,000"), 49800), fail_rakuten=True)
code, sent = run()
check("7/メール数", len(sent), 1)
check("7/ATL", state()["ulike-airpro-s"]["atl"]["price"], 25000)

print("== 8. --dry-run は state / history を触らない ==")
snap_state, snap_hist = STATE.read_text(encoding="utf-8"), len(history())
stub(rakuten_payload(NAME_COUPON.format("19,800"), 49800), yahoo_payload(NAME_LIST, 48000))
code, sent = run(dry=True)
check("8/メール数", len(sent), 0)
check("8/state 不変", STATE.read_text(encoding="utf-8"), snap_state)
check("8/history 不変", len(history()), snap_hist)

print("== 9. 候補0件が3回連続 → 警告メール、以後クールダウン ==")
stub()  # API は成功するが該当なし
for _ in range(3):
    code, sent = run()
check("9/警告メール", len(sent), 1)
check("9/件名", "検索結果 0 件" in sent[0], True)
code, sent = run()
check("9/クールダウン", len(sent), 0)

print("== 10. ATL 手動操作 ==")
watch.set_atl(29800)
check("10/set-atl", state()["ulike-airpro-s"]["atl"]["price"], 29800)
watch.reset_atl()
check("10/reset-atl", state()["ulike-airpro-s"].get("atl"), None)
check("10/health 保持", "health" in state()["ulike-airpro-s"], True)

print()
for line in fails:
    print("FAIL", line)
print(f"e2e: {'OK' if not fails else str(len(fails)) + ' failed'}")
shutil.rmtree(WORK, ignore_errors=True)
raise SystemExit(1 if fails else 0)
