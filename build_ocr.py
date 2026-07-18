#!/usr/bin/env python3
"""学食メニュー有志サイト ビルダー（OCR版・PaddleOCRで栄養解析＝API課金ゼロ）

本家を「トップGET → shop_id POST → current_day切替GET」でスクレイプし、
3キャンパス×7日分のメニュー画像URLを取得。各メニュー画像を PaddleOCR で
解析して栄養・価格を抽出し、自己完結 index.html を生成する。

解析結果は cache/<画像ID>.json に保存（画像ID単位＝同じ画像は二度と再解析しない）。
既存の Claude 解析済みキャッシュはそのまま有効（新規画像だけ OCR で解析）。

解析ロジック: 半田パッキアの実画像で正解15品・105フィールド100%一致を確認済み(2026-07-02)
- 料理名 = 文字高さが大きいテキスト(実測: 料理名h32-73 vs ラベル類h8-16)
- 料理名をアンカーに近傍からカード矩形を自動算出(列数はハードコードしない)
- kcal付き数値(料理名に最も近いもの)を栄養行アンカーに、右隣を P/F/C と対応
- ¥付き数値から価格。小中大ラベル+「中より大きい無ラベル→大」等の推定でsizes構築

依存: httpx, beautifulsoup4, paddlepaddle, paddleocr, pillow, python-dotenv
ローカル実行: ~/.local/menu-ocr-venv/bin/python3 build.py
"""
import io
import json
import os
import re
import ssl
import sys
import time
import tempfile
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from PIL import Image

# ローカル実行用に .env を読む（Actions では無害にスキップ）
try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".local" / "menu-venv" / ".env")
except Exception:
    pass

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
BASE = ("https://signage.univcoop-tokai.net/smt_menu_ants2/view_list.php"
        "?uv=13&current_day=0&current_page=no_page")
DAYS = 7
WD = "月火水木金土日"

SHOPS = [
    {"key": "pacchia",  "id": "29",  "name": "半田キャンパス パッキア", "emoji": "🏫"},
    {"key": "shokusai", "id": "74",  "name": "美浜キャンパス 食菜",     "emoji": "🌊"},
    {"key": "lupo",     "id": "130", "name": "東海キャンパス ルポ",     "emoji": "🚗"},
]

CACHE_DIR = Path(__file__).resolve().parent / "cache"

_ctx = ssl.create_default_context()
_ctx.set_ciphers("DEFAULT@SECLEVEL=1")  # 本家の古いTLS対策


# ============================================================
# 画像URL抽出（変更なし）
# ============================================================
def parse_image_urls(html):
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for li in soup.select("li.item"):
        img = li.find("img")
        if not img or not img.get("src"):
            continue
        url = img["src"]
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://signage.univcoop-tokai.net" + url
        if url.endswith("s.png"):
            url = url[:-5] + ".png"
        url = url.replace("http://", "https://")
        urls.append(url)
    return urls


def _nearest_date(mon, day, today):
    """月/日だけの表記に、today に最も近い年を補う（年末年始の跨ぎ対策）"""
    best = None
    for off in (-1, 0, 1):
        try:
            cand = date(today.year + off, mon, day)
        except ValueError:
            continue
        if best is None or abs((cand - today).days) < abs((best - today).days):
            best = cand
    return best


def parse_day_nav(html, today):
    """ページ上部ナビ <a href="...current_day=N...">M／D</a> から
    current_day → 実日付 の対応を取る。

    本家の current_day はカレンダー日ではなく営業日インデックス
    （土日祝をスキップし、夕方には翌営業日へ繰り上がる）ため、
    today + d 日のカレンダー計算では日付がずれる。ナビの表記が唯一の正。

    生HTMLを正規表現で読む（bs4経由にしない）。href の "&current_day" を
    BeautifulSoup 4.15 系が実体参照 &curren;(¤) として復号し current_day を
    壊すため、属性値ではなく生の markup から index と日付を拾う。
    """
    seen = {}
    for m in re.finditer(
            r"current_day=(\d+)[^>]*>\s*(\d{1,2})\s*[／/]\s*(\d{1,2})\s*<", html):
        idx = int(m.group(1))
        dt = _nearest_date(int(m.group(2)), int(m.group(3)), today)
        if dt is not None:
            seen.setdefault(idx, dt)  # 同一 index は最初の出現を採用
    return sorted(seen.items())


# ============================================================
# 料理名・正規化・妥当性判定（既存ロジックを流用）
# ============================================================
NAME_FIXES = {
    "スカツカレー": "ロースカツカレー", "スカツ丼": "ロースカツ丼",
    "ースカツカレー": "ロースカツカレー", "ースカツ丼": "ロースカツ丼",
}
NAME_PATTERNS = [
    (re.compile(r"^スカツ"), "ロースカツ"),
    (re.compile(r"^ースカツ"), "ロースカツ"),
]
SIZE_ORDER = {"小": 0, "並": 1, "中": 2, "大": 3}


def fix_dish_name(name):
    if not name:
        return name
    if name in NAME_FIXES:
        return NAME_FIXES[name]
    for pat, repl in NAME_PATTERNS:
        if pat.search(name):
            return pat.sub(repl, name)
    return name


def normalize_dish(d):
    """dishを整える。サイズ・価格を正規化して描画用dictにする"""
    sizes = {k: int(v) for k, v in (d.get("sizes") or {}).items() if v}
    if not sizes:
        sizes = {"並": int(d.get("price") or 0)}
    if "中" in sizes:
        price = sizes["中"]
    else:
        price = int(d.get("price") or 0)
        if price not in sizes.values():
            price = sorted(sizes.items(), key=lambda kv: SIZE_ORDER.get(kv[0], 9))[0][1]
    name = fix_dish_name(d.get("name", ""))
    return {
        "name": name,
        "price": price,
        "sizes": sizes,
        "energy": d.get("energy"),
        "protein": d.get("protein"),
        "fat": d.get("fat"),
        "carb": d.get("carb"),
        # categoryは整形後の料理名から毎回導出（分類ロジックの更新に追従させる）
        "category": _guess_category(name),
    }


def is_valid_menu(dishes):
    """ちゃんと食事メニューか判定（告知ポスターを弾く）"""
    if len(dishes) < 2:
        return False
    priced = sum(1 for d in dishes if d.get("price"))
    return priced >= max(2, len(dishes) // 2)


# ============================================================
# PaddleOCR 解析（★ Claude の代替。ここが無料化の本体）
# ============================================================
NUTRI_WORDS = ["エネルギー", "タンパク質", "脂質", "炭水化物", "食塩相当量",
               "カルシウム", "野菜量", "アレルゲン", "食材中"]
JUNK = ["本体", "税込", "税抜", "サイズ", "のもの", "栄養", "おすすめ", "今日",
        "MENU", "TODAY", "CO-OP", "univ", "組価", "です", "表示", "当店"]
JP = re.compile(r"[ぁ-んァ-ヶ一-龥]")
VALUE_PAT = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:g|kcal|mg)?$")

_OCR = None


def get_ocr():
    """PaddleOCRインスタンスを1回だけ作って使い回す（初期化が重いため）"""
    global _OCR
    if _OCR is None:
        from paddleocr import PaddleOCR
        try:
            # enable_mkldnn=False: paddlepaddle 3.3.x は CPU の oneDNN(PIR) 経路に
            # 未実装バグがあり全推論が NotImplementedError で落ちる（CI の x86 で発症）。
            # https://github.com/PaddlePaddle/Paddle/issues/77340
            # https://github.com/PaddlePaddle/PaddleOCR/issues/18162
            _OCR = PaddleOCR(lang="japan", use_textline_orientation=True,
                             enable_mkldnn=False)
        except TypeError:
            _OCR = PaddleOCR(lang="japan", use_angle_cls=True)
    return _OCR


def _is_name(o, name_min_h):
    """料理名か判定。カギは文字高さ(料理名は他テキストの2倍以上デカい)"""
    t = o["text"].strip()
    h = o["y1"] - o["y0"]
    if len(t) < 2 or h < name_min_h:
        return False
    if "¥" in t or "￥" in t or re.search(r"\d", t):
        return False
    if not JP.search(t):
        return False
    return not any(w in t for w in NUTRI_WORDS + JUNK)


def _numi(text):
    s = re.sub(r"[^0-9]", "", text)
    return int(s) if s else None


def _numf(text):
    s = text.replace("kcal", "").replace("mg", "")
    s = re.sub(r"[^0-9.]", "", s)
    if not s or s == ".":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v < 1000 else None  # 桁が異常なら誤読として破棄


def _is_value(text):
    """数値セルらしいテキストか（誤読ラベル『クンバ2』等の混入を防ぐ）"""
    t = text.replace("O", "0").replace("o", "0").strip()
    return bool(VALUE_PAT.fullmatch(t))


def _build_cards(names, w, h):
    """各料理名をアンカーに、カード矩形(L,R,T,B)を近傍の料理名から自動算出"""
    row_tol = 0.021 * h
    x_margin = 0.010 * w
    cards = []
    for n in names:
        row = sorted([m for m in names if abs(m["cy"] - n["cy"]) < row_tol],
                     key=lambda o: o["x0"])
        i = row.index(n)
        left = row[i - 1]["x1"] + x_margin if i > 0 else -1e9
        right = row[i + 1]["x0"] - x_margin / 2 if i + 1 < len(row) else 1e9
        below = sorted([m for m in names
                        if m["y0"] > n["y1"] and not (m["x1"] < n["x0"] - 0.04 * w
                                                      or m["x0"] > right)],
                       key=lambda o: o["y0"])
        bottom = below[0]["y0"] - 0.008 * h if below else 1e9
        cards.append({"name": n["text"].strip(), "cx": n["cx"], "L": left, "R": right,
                      "T": n["y0"] - 0.015 * h, "B": bottom})
    return cards


def _extract_nutrition(elems, name_cx, h):
    """kcal付き数値をアンカーに、同じ行の右隣を P/F/C として読む"""
    band_tol = 0.010 * h
    out = {"energy": None, "protein": None, "fat": None, "carb": None}
    kcal = [e for e in elems if "kcal" in e["text"].lower()]
    if not kcal:
        return out
    # カードに複数kcalが混入したら料理名に最も近いものを採用
    a = min(kcal, key=lambda e: abs(e["cx"] - name_cx))
    out["energy"] = _numi(a["text"])
    band = sorted([e for e in elems
                   if _is_value(e["text"]) and "¥" not in e["text"]
                   and "￥" not in e["text"] and "mg" not in e["text"]
                   and e["cx"] > a["cx"] and abs(e["cy"] - a["cy"]) < band_tol],
                  key=lambda e: e["cx"])
    for k, e in zip(["protein", "fat", "carb"], band[:3]):
        out[k] = _numf(e["text"])
    return out


def _extract_price(elems):
    """¥付き数値から price と sizes を組む"""
    yen = [e for e in elems
           if ("¥" in e["text"] or "￥" in e["text"]) and re.search(r"\d", e["text"])
           and not any(w in e["text"] for w in ("本体", "税", "(", "（"))]
    if not yen:
        return None, {}
    sizes = {}
    unlabeled = []
    for e in yen:
        v = _numi(e["text"])
        if not v or v > 5000:
            continue
        m = re.search(r"[小中大]", e["text"])
        if m:
            sizes[m.group()] = v
        else:
            unlabeled.append((v, e))
    main = max(yen, key=lambda e: e["y1"] - e["y0"])
    main_v = _numi(main["text"])
    if main_v and "中" not in sizes and (sizes or len(unlabeled) > 1):
        sizes["中"] = main_v  # メイン(最大文字)は中サイズ扱い(「甲」等の誤読対策)
    mid = sizes.get("中")
    if mid:
        # ラベル無し価格をサイズ推定: 中より大きい→大 / 小さい→小 (各最大値を採用)
        ups = [v for v, _ in unlabeled if v > mid]
        downs = [v for v, _ in unlabeled if v < mid]
        if ups and "大" not in sizes:
            sizes["大"] = max(ups)
        if downs and "小" not in sizes:
            sizes["小"] = max(downs)
    price = sizes.get("中") or main_v
    if len(sizes) <= 1:
        sizes = {}
    return price, sizes


def _guess_category(name):
    if name.strip() in ("ライス", "ごはん", "ご飯", "白米"):
        return "ご飯"
    if re.search(r"カレー|丼|ライス", name):
        return "丼"
    if re.search(r"ラーメン|うどん|そば|麺|ヌードル", name):
        return "麺"
    if re.search(r"味噌汁|豚汁|スープ|汁", name):
        return "汁物"
    if re.search(r"サラダ", name):
        return "サラダ"
    if re.search(r"ケーキ|タルト|もち|餅|プリン|ゼリー|デザート"
                 r"|エクレア|シュークリーム|ヨーグルト|ムース|クレープ|パフェ"
                 r"|アイス|ドーナ|ワッフル|ショート|ティラミス|パンナコッタ"
                 r"|ババロア|マカロン|あんみつ|ぜんざい|白玉|大福|どら焼き|大学芋", name):
        return "デザート"
    if re.search(r"ハンバーグ|フライ|焼|天ぷら|カツ|揚げ|ステーキ|炒め", name):
        return "主菜"
    if re.search(r"煮|和え|お浸し|おひたし|きんぴら", name):
        return "小鉢"
    return "その他"


def ocr_dishes(img_bytes):
    """画像bytes → dishのraw list（Claudeのdishes相当）。PaddleOCRで解析。"""
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = im.width, im.height
    im2 = im.resize((w * 2, h * 2), Image.LANCZOS)  # 小さい文字対策で2倍拡大
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    im2.save(tmp.name)
    tmp.close()

    ocr = get_ocr()
    items = []

    def add(poly, text, conf):
        xs = [float(p[0]) / 2 for p in poly]
        ys = [float(p[1]) / 2 for p in poly]
        items.append({"text": text, "conf": float(conf),
                      "x0": min(xs), "y0": min(ys),
                      "x1": max(xs), "y1": max(ys),
                      "cx": sum(xs) / len(xs), "cy": sum(ys) / len(ys)})

    parsed = False
    try:
        # PaddleOCR 3.x 系 API
        for page in ocr.predict(tmp.name):
            texts = page.get("rec_texts") or []
            scores = page.get("rec_scores") or []
            polys = page.get("rec_polys") if page.get("rec_polys") is not None else page.get("dt_polys")
            for t, s, p in zip(texts, scores, polys):
                add(p, t, s)
            parsed = True
    except Exception as e:
        sys.stderr.write("  predict NG, fallback .ocr(): %s\n" % e)
    if not parsed:
        # 旧 2.x 系 API フォールバック
        items.clear()
        for page in ocr.ocr(tmp.name):
            if not page:
                continue
            for line in page:
                add(line[0], line[1][0], line[1][1])
    try:
        os.unlink(tmp.name)
    except OSError:
        pass

    name_min_h = 0.022 * w
    names = sorted([o for o in items if _is_name(o, name_min_h)],
                   key=lambda o: (o["y0"], o["x0"]))
    dishes = []
    for c in _build_cards(names, w, h):
        elems = [o for o in items
                 if c["L"] <= o["cx"] <= c["R"] and c["T"] <= o["cy"] <= c["B"]]
        price, sizes = _extract_price(elems)
        if price is None:
            continue  # 価格が無い＝料理カードではない
        nut = _extract_nutrition(elems, c["cx"], h)
        dishes.append({"name": c["name"], "price": price, "sizes": sizes,
                       "energy": nut["energy"], "protein": nut["protein"],
                       "fat": nut["fat"], "carb": nut["carb"],
                       "category": _guess_category(c["name"])})
    return dishes


# OCR実行の健全性メトリクス（analyze_image_url が更新し、main末尾のゲートが判定）
STATS = {"cache_hit": 0, "ocr_ok": 0, "ocr_crash": 0, "dl_fail": 0}
CRASH_RATE_LIMIT = 0.5  # 新規解析のうちこの割合超がOCR例外なら「環境全滅の疑い」


def health_verdict(stats, limit=CRASH_RATE_LIMIT):
    """OCRメトリクスから健全性を判定して (ok: bool, reason: str) を返す。

    新規に解析を試みた画像(ocr_ok + ocr_crash)のうち OCR例外の割合が limit を
    超えたら「OCR環境全滅の疑い」でNG。全キャッシュヒットやDL失敗のみの回、
    コールドスタート(新規解析0)は判定対象外＝正常扱いにする（誤発報を防ぐ）。
    週末等の正当な空は OCR自体は成功(ocr_ok)しており crash に乗らないので誤発報しない。
    """
    fresh = stats["ocr_ok"] + stats["ocr_crash"]
    if fresh == 0:
        return True, f"新規OCR解析なし(cache_hit={stats['cache_hit']} dl_fail={stats['dl_fail']})＝判定スキップ"
    rate = stats["ocr_crash"] / fresh
    if rate > limit:
        return False, f"OCR環境全滅の疑い: crash {stats['ocr_crash']}/{fresh}={rate:.0%} > 上限{limit:.0%}"
    return True, f"OK: ocr_ok={stats['ocr_ok']} crash={stats['ocr_crash']}/{fresh}={rate:.0%}"


def analyze_image_url(url):
    """画像URLをOCR解析して栄養付きdishリストを返す。画像ID単位でキャッシュ（再解析しない）"""
    img_id = url.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # 0000042541
    cf = CACHE_DIR / f"{img_id}.json"
    if cf.exists():
        try:
            cached = json.loads(cf.read_text(encoding="utf-8"))
            STATS["cache_hit"] += 1
            if not cached.get("valid"):
                return []
            dishes = cached["dishes"]
            for d in dishes:
                # 「その他」落ちしたものだけ再判定してデザート等に救済する。
                # Claude版(build.py)cache由来の細かい分類（主菜/小鉢/デザート等）は壊さない。
                if d.get("category", "その他") == "その他":
                    d["category"] = _guess_category(d.get("name", ""))
            return dishes
        except Exception:
            pass

    # 画像ダウンロード
    try:
        with httpx.Client(verify=_ctx, timeout=30, follow_redirects=True,
                          headers={"User-Agent": UA}) as c:
            r = c.get(url)
            if not r.is_success:
                STATS["dl_fail"] += 1
                return []
            img_bytes = r.content
    except Exception as e:
        STATS["dl_fail"] += 1
        print(f"  ⚠️ 画像DL失敗 {img_id}: {e}", file=sys.stderr)
        return []

    print(f"  🔍 OCR解析中 {img_id} …", file=sys.stderr)
    try:
        raw = ocr_dishes(img_bytes)
    except Exception as e:
        STATS["ocr_crash"] += 1
        print(f"  ❌ OCR解析失敗 {img_id}: {e}", file=sys.stderr)
        return []

    STATS["ocr_ok"] += 1
    dishes = [normalize_dish(d) for d in raw if d.get("name")]
    valid = is_valid_menu(dishes)
    CACHE_DIR.mkdir(exist_ok=True)
    cf.write_text(json.dumps({"dishes": dishes, "valid": valid}, ensure_ascii=False, indent=1),
                  encoding="utf-8")
    print(f"     → {len(dishes)}品 / valid={valid}", file=sys.stderr)
    return dishes if valid else []


# ============================================================
# 取得＋解析
# ============================================================
def fetch_all():
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).date()
    with httpx.Client(verify=_ctx, timeout=30, follow_redirects=True,
                      headers={"User-Agent": UA, "Accept-Language": "ja-JP"}) as c:
        c.get(BASE)
        for shop in SHOPS:
            print(f"{shop['emoji']} {shop['name']}", file=sys.stderr)
            rp = c.post(BASE, data={"shop_id": shop["id"], "client_id": "13",
                                    "shop_name": shop["name"]})
            # 日付は本家ナビの表記を正本にする。current_day は土日を飛ばす
            # 営業日インデックスで夕方に翌日へ繰り上がるため、today+d では1日ずれる。
            nav = parse_day_nav(rp.text, today)
            if not nav:
                print("  ⚠️ 日付ナビを取得できず、カレンダー計算にフォールバック",
                      file=sys.stderr)
                nav = [(d, today + timedelta(days=d)) for d in range(DAYS)]

            raw = {}
            for idx, _dt in nav:
                if idx == 0:
                    raw[idx] = parse_image_urls(rp.text)
                else:
                    u = BASE.replace("current_day=0", f"current_day={idx}")
                    raw[idx] = parse_image_urls(c.get(u).text)
                    time.sleep(0.2)

            # ユニークな画像だけ1回解析（キャッシュで二重解析防止）
            analyzed = {}
            for idx, _dt in nav:
                for url in raw[idx]:
                    if url not in analyzed:
                        analyzed[url] = analyze_image_url(url)

            shop["days"] = []
            for idx, dt in nav:
                dishes, seen = [], set()
                for url in raw[idx]:
                    for dish in analyzed.get(url, []):
                        k = (dish["name"], dish["price"])
                        if k not in seen:
                            seen.add(k)
                            dishes.append(dish)
                shop["days"].append({
                    "date": f"{dt.month}/{dt.day}", "wday": WD[dt.weekday()],
                    "weekend": dt.weekday() >= 5, "images": raw[idx], "dishes": dishes,
                })
    return SHOPS


# ============================================================
# HTML
# ============================================================
def load_design_tokens():
    """design/design-tokens.json（正本SSoT）を読む"""
    p = Path(__file__).resolve().parent / "design" / "design-tokens.json"
    return json.loads(p.read_text(encoding="utf-8"))


def tokens_to_css_vars(tok):
    """トークンJSONを :root のCSS変数に展開（CSSは必ず var() でこれを参照する）"""
    lines = []
    for cat, grp in tok.get("color", {}).items():
        for k, v in grp.items():
            lines.append(f"  --color-{cat}-{k}: {v};")
    for k, v in tok.get("space", {}).items():
        lines.append(f"  --space-{k}: {v};")
    for k, v in tok.get("radius", {}).items():
        lines.append(f"  --radius-{k}: {v};")
    font = tok.get("font", {})
    for sub in ("family", "size", "weight", "line"):
        for k, v in font.get(sub, {}).items():
            lines.append(f"  --font-{sub}-{k}: {v};")
    return ":root {\n" + "\n".join(lines) + "\n}"


TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🍱 日福 学食メニュー</title>
<style>
__CSSVARS__
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: var(--font-family-base);
    background: var(--color-bg-base);
    color: var(--color-fg-base); min-height: 100vh; padding: var(--space-4);
    line-height: var(--font-line-base); font-size: var(--font-size-base);
  }
  header { text-align: center; padding: var(--space-3) 0 var(--space-4); }
  header h1 { font-size: var(--font-size-xl); font-weight: var(--font-weight-bold); letter-spacing: .02em; }
  header .updated { font-size: var(--font-size-xs); color: var(--color-fg-muted); margin-top: var(--space-1); }
  .budget-bar { text-align: center; margin: var(--space-3) 0 var(--space-4); font-size: var(--font-size-sm); }
  .budget-bar input {
    width: 92px; font-size: var(--font-size-base); font-weight: var(--font-weight-bold); text-align: right;
    padding: var(--space-2) var(--space-3); border-radius: var(--radius-md); border: 1px solid var(--color-border-base);
    background: var(--color-bg-subtle); color: var(--color-fg-base);
  }
  .budget-bar .preset { cursor: pointer; color: var(--color-accent-base); text-decoration: underline; margin-left: var(--space-2); }
  .half-toggle { display: inline-block; margin-left: var(--space-3); cursor: pointer; user-select: none; padding: var(--space-1) var(--space-3); border-radius: var(--radius-full); background: var(--color-bg-subtle); border: 1px solid var(--color-danger-base); color: var(--color-danger-base); font-weight: var(--font-weight-medium); }
  .half-toggle input { vertical-align: middle; margin-right: 3px; }
  .half-on { text-align: center; font-weight: var(--font-weight-bold); color: var(--color-danger-base); background: var(--color-bg-subtle); border: 1px solid var(--color-danger-base); border-radius: var(--radius-md); padding: var(--space-2); margin: var(--space-4) 0 var(--space-1); }
  .tabs { display: flex; gap: var(--space-2); justify-content: center; flex-wrap: wrap; margin-bottom: var(--space-4); }
  .tab {
    border: 1px solid var(--color-border-base); cursor: pointer; font-size: var(--font-size-sm); font-weight: var(--font-weight-medium);
    padding: var(--space-2) var(--space-4); border-radius: var(--radius-full); color: var(--color-fg-muted);
    background: var(--color-bg-subtle); transition: .15s;
  }
  .tab:hover { background: var(--color-bg-elevated); color: var(--color-fg-base); }
  .tab.active { background: var(--color-accent-base); color: #fff; border-color: transparent; }
  .panel { display: none; max-width: 880px; margin: 0 auto; }
  .panel.active { display: block; animation: fade .25s ease; }
  @keyframes fade { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  .panel h2 { text-align: center; font-size: var(--font-size-lg); font-weight: var(--font-weight-bold); margin-bottom: var(--space-3); }
  .daytabs {
    display: flex; gap: var(--space-2); margin-bottom: var(--space-4); overflow-x: auto;
    padding-bottom: var(--space-1); -webkit-overflow-scrolling: touch; scrollbar-width: thin;
  }
  .daytab {
    flex: 0 0 auto; border: 1px solid var(--color-border-base); cursor: pointer;
    font-size: var(--font-size-sm); font-weight: var(--font-weight-medium); padding: var(--space-2) var(--space-3); border-radius: var(--radius-md);
    color: var(--color-fg-muted); background: var(--color-bg-subtle); transition: .15s; line-height: var(--font-line-tight); text-align: center;
  }
  .daytab small { display: block; color: var(--color-fg-subtle); font-size: var(--font-size-xs); font-weight: var(--font-weight-regular); }
  .daytab:hover { background: var(--color-bg-elevated); }
  .daytab.weekend { color: var(--color-accent-base); }
  .daytab.active { background: var(--color-accent-base); color: #fff; border-color: transparent; }
  .daytab.active small { color: #fff; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 320px)); gap: var(--space-4); justify-content: center; }
  .card { background: var(--color-bg-elevated); border: 1px solid var(--color-border-strong); border-radius: var(--radius-lg); overflow: hidden; transition: border-color .15s; }
  .card:hover { border-color: var(--color-accent-base); }
  .card img { width: 100%; display: block; background: #fff; }
  .empty { text-align: center; padding: var(--space-8) var(--space-4); color: var(--color-fg-muted); line-height: var(--font-line-relaxed); background: var(--color-bg-subtle); border-radius: var(--radius-lg); }
  .sec-h { font-size: var(--font-size-sm); color: var(--color-fg-muted); margin: var(--space-6) 0 var(--space-2); font-weight: var(--font-weight-bold); }
  .nut-wrap { overflow-x: auto; border-radius: var(--radius-md); -webkit-overflow-scrolling: touch; border: 1px solid var(--color-border-base); }
  table.nut { width: 100%; min-width: 480px; border-collapse: collapse; font-size: var(--font-size-sm); background: var(--color-bg-subtle); }
  table.nut th { background: var(--color-bg-elevated); padding: var(--space-2); text-align: right; font-weight: var(--font-weight-medium); white-space: nowrap; }
  table.nut th:first-child, table.nut td:first-child { text-align: left; }
  table.nut th:nth-child(2), table.nut td:nth-child(2) { text-align: center; }
  table.nut td { padding: var(--space-2); text-align: right; border-top: 1px solid var(--color-border-base); white-space: nowrap; }
  table.nut td:first-child { white-space: normal; min-width: 120px; }
  .size-sel { display: inline-block; margin-left: var(--space-3); }
  .size-sel select, .dsel-bar select { background: var(--color-bg-subtle); color: var(--color-fg-base); border: 1px solid var(--color-border-base); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2); font-size: var(--font-size-sm); }
  .dsel-bar { margin: var(--space-4) 0 0; text-align: center; font-size: var(--font-size-sm); }
  .dsel-bar select { margin-left: var(--space-1); }
  table.nut tr:hover td { background: var(--color-bg-elevated); }
  .nut-cat { color: var(--color-fg-subtle); }
  .total { text-align: right; font-size: var(--font-size-xs); color: var(--color-fg-muted); margin-top: var(--space-1); }
  .combo-card {
    background: var(--color-bg-elevated); border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-md); padding: var(--space-3); margin-top: var(--space-3); position: relative;
    transition: border-color .15s;
  }
  .combo-card:hover { border-color: var(--color-accent-base); }
  .combo-rank { position: absolute; top: -9px; left: var(--space-3); background: var(--color-accent-base); color: #fff; font-weight: var(--font-weight-bold); font-size: var(--font-size-xs); padding: 2px var(--space-2); border-radius: var(--radius-full); }
  .combo-names { font-weight: var(--font-weight-bold); margin-bottom: var(--space-1); padding-top: 2px; }
  .combo-stats { font-size: var(--font-size-sm); color: var(--color-fg-muted); }
  .combo-stats .diff { color: var(--color-success-base); }
  .combo-badge { font-size: var(--font-size-xs); color: var(--color-fg-muted); margin-top: 3px; }
  .combo-empty { color: var(--color-fg-muted); padding: var(--space-3); text-align: center; }
  footer { text-align: center; font-size: var(--font-size-xs); color: var(--color-fg-subtle); margin-top: var(--space-8); line-height: var(--font-line-relaxed); }
  a { color: var(--color-accent-base); }
  code { background: var(--color-bg-subtle); padding: 1px var(--space-1); border-radius: var(--radius-sm); }
</style>
</head>
<body>
<header>
  <h1>🍱 日福 学食メニュー</h1>
  <div class="updated"></div>
</header>
<div class="budget-bar">
  🎫 予算 ¥<input type="number" id="budget" value="740" min="100" step="10">
  <span class="preset" data-v="740">学食パス740</span>
  <span class="preset" data-v="980">🌟高級980</span>
  <span class="preset" data-v="600">節約600</span>
  <label class="half-toggle"><input type="checkbox" id="half">🉐半額week</label>
  <span class="size-sel">🍚<select id="rsize">
    <option value="">サイズおまかせ</option>
    <option value="小">小で固定</option>
    <option value="中">中で固定</option>
    <option value="大">大で固定</option>
  </select></span>
  <span class="size-sel">🎯<select id="omode">
    <option value="">予算スレスレ</option>
    <option value="protein">💪高タンパク</option>
    <option value="lowcal">🥗低カロリー</option>
    <option value="cospa">💰コスパ</option>
    <option value="pfc">⚖️PFCバランス</option>
  </select></span>
</div>
<div class="tabs" id="tabs"></div>
<div id="panels"></div>
<footer>
  非公式・有志ページ / 画像は日本福祉大学生協 signage より / 栄養はAI解析（誤りがある場合あり・中サイズ基準）<br>
  キャンパスは上のタブ、または URL末尾 <code>#pacchia</code> で直接開けます
</footer>
<script>
const DATA = __DATA__;
const tabs = document.getElementById('tabs');
const panels = document.getElementById('panels');
document.querySelector('.updated').textContent = '取得: ' + DATA.updated;

const SIZE_ORDER = {"小":0,"並":1,"中":2,"大":3};
const SIZE_RICE_DELTA = {"小":[-100,-24],"並":[0,0],"中":[0,0],"大":[150,36]};
const MAIN = ["主菜","丼","麺"], CARB = ["丼","麺","ご飯"], SOLO_NG = ["汁物","小鉢","サラダ","ご飯","デザート"];
const MODE_LABEL = {protein:'💪高タンパク', lowcal:'🥗低カロリー', cospa:'💰コスパ', pfc:'⚖️PFCバランス'};

function num(x){ return (x==null)?null:Number(x); }

function cardsHtml(images) {
  if (!images.length) return '<div class="empty">🈚 この日はメニューがないみたい<br>（土日・休業日 / まだ未掲載かも）</div>';
  return '<div class="grid">' + images.map((u) =>
    '<a class="card" href="' + u + '" target="_blank" rel="noopener"><img loading="lazy" src="' + u + '" alt="menu"></a>'
  ).join('') + '</div>';
}

function fmtPrice(d){
  const ks = Object.keys(d.sizes||{});
  if (ks.length > 1) {
    return Object.entries(d.sizes).sort((a,b)=>(SIZE_ORDER[a[0]]??9)-(SIZE_ORDER[b[0]]??9)).map(e=>e[1]).join('/');
  }
  return '¥' + d.price;
}
function nutritionTable(dishes){
  if (!dishes.length) return '';
  const rows = dishes.map((d)=>{
    const p=num(d.protein), f=num(d.fat), c=num(d.carb), e=num(d.energy);
    return '<tr><td>'+d.name+'</td><td class="nut-cat">'+d.category+'</td><td>'+fmtPrice(d)+'</td>'+
      '<td>'+(e!=null?e:'-')+'</td><td>'+(p!=null?p.toFixed(1):'-')+'</td>'+
      '<td>'+(f!=null?f.toFixed(1):'-')+'</td><td>'+(c!=null?c.toFixed(1):'-')+'</td></tr>';
  }).join('');
  const tp = dishes.reduce((s,d)=>s+d.price,0);
  const tk = dishes.reduce((s,d)=>s+(num(d.energy)||0),0);
  return '<div class="sec-h">📋 栄養一覧</div><div class="nut-wrap"><table class="nut"><thead><tr>'+
    '<th>料理</th><th>区分</th><th>価格</th><th>kcal</th><th>P</th><th>F</th><th>C</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table></div>'+
    '<div class="total">全'+dishes.length+'品 / 全部頼むと ¥'+tp+'（'+tk+'kcal）</div>'+
    '<div class="total" style="opacity:.5">価格が複数値の料理は 小/中/大（kcal等は中基準）</div>';
}

function expandSizes(dishes, onlySize){
  const out=[];
  for(const d of dishes){
    const sizes = (d.sizes && Object.keys(d.sizes).length) ? d.sizes : {"並": d.price};
    const multi = Object.keys(sizes).length>1;
    for(const [sz,price] of Object.entries(sizes).sort((a,b)=>(SIZE_ORDER[a[0]]??9)-(SIZE_ORDER[b[0]]??9))){
      if(onlySize && multi && sz!==onlySize) continue;  // サイズ固定（指定サイズ以外は除外）
      const [dk,dc] = SIZE_RICE_DELTA[sz]||[0,0];
      let e=num(d.energy), c=num(d.carb);
      if(multi && e!=null) e=Math.max(0,e+dk);
      if(multi && c!=null) c=Math.max(0,c+dc);
      out.push({name: multi?(d.name+'('+sz+')'):d.name, price:Math.round(price),
                energy:e, protein:num(d.protein), fat:num(d.fat), carb:c,
                category:d.category, base:d.name});
    }
  }
  return out;
}
function* combinations(arr,r){
  const n=arr.length; if(r>n) return;
  const idx=[...Array(r).keys()];
  while(true){
    yield idx.map(i=>arr[i]);
    let i=r-1; while(i>=0 && idx[i]===i+n-r) i--;
    if(i<0) break;
    idx[i]++; for(let j=i+1;j<r;j++) idx[j]=idx[j-1]+1;
  }
}
function suggestCombos(dishes, budget, onlySize, mode, topN=3, maxItems=4){
  const v = expandSizes(dishes, onlySize), all=[];
  for(let r=1;r<=Math.min(v.length,maxItems);r++){
    for(const combo of combinations(v,r)){
      const bases=combo.map(d=>d.base);
      if(new Set(bases).size!==bases.length) continue;
      const price=combo.reduce((s,d)=>s+d.price,0);
      if(price>budget) continue;
      if(combo.filter((d)=>CARB.includes(d.category)).length > 1) continue;  // ご飯もの(丼/麺/ご飯)は1つまで＝丼+ライス等の重複を防ぐ
      if(r===1 && SOLO_NG.includes(combo[0].category)) continue;
      const energy=combo.reduce((s,d)=>s+(d.energy||0),0);
      const protein=combo.reduce((s,d)=>s+(d.protein||0),0);
      const fat=combo.reduce((s,d)=>s+(d.fat||0),0);
      const carb=combo.reduce((s,d)=>s+(d.carb||0),0);
      const balanced=combo.some(d=>MAIN.includes(d.category));
      const hasCarb=combo.some(d=>CARB.includes(d.category));
      if((mode==='lowcal'||mode==='cospa') && !balanced) continue;  // 低カロ/コスパは主菜あり必須（味噌汁だけ/ライスだけ等の極端を防ぐ）
      all.push({combo,price,diff:budget-price,energy,protein,fat,carb,balanced,hasCarb});
    }
  }
  // モード別スコア（全部「大きいほど良い」に正規化）。同点は予算スレスレ→主菜あり
  const score = (c)=>{
    if(mode==='protein') return c.protein;            // 高タンパク
    if(mode==='lowcal')  return c.energy>0 ? c.protein/c.energy : 0;  // 低カロリー高タンパク(kcalあたりタンパク質 最大)
    if(mode==='cospa')   return c.price>0 ? (c.energy + c.protein*4)/c.price : 0;  // コスパ
    if(mode==='pfc')     return c.protein*4 - c.fat;  // PFCバランス(タンパク多・脂質少)
    return -c.diff;                                   // 予算スレスレ(デフォルト)
  };
  all.sort((a,b)=> score(b)-score(a) || a.diff-b.diff || (b.balanced-a.balanced));
  return all.slice(0,topN);
}
function comboHtml(dishes, budget, onlySize, rice, dessert, mode){
  if(!dishes.length) return '';
  // 選んだご飯もの/デザートを先打ち確定 → 残予算で残りを最適化（指定は反映しつつ最適化も保つ）
  const picks = [rice, dessert].filter(Boolean);
  const pickPrice = picks.reduce((s,d)=>s+d.price,0);
  let pool = dishes;
  if(rice) pool = pool.filter(d=>!CARB.includes(d.category));   // ご飯ものを指定→他の主食を除外
  if(dessert) pool = pool.filter(d=>d.category!=='デザート');    // デザートを指定→デザート除外
  let cs;
  if(pickPrice > budget){
    cs = [];
  } else {
    cs = suggestCombos(pool, budget - pickPrice, onlySize, mode, 3).map(best=>{
      const pv = picks.map(p=>({name:p.name, price:p.price, energy:num(p.energy), protein:num(p.protein),
                                fat:num(p.fat), carb:num(p.carb), category:p.category, base:p.base||p.name}));
      const combo = [...pv, ...best.combo];
      const sum = k => pv.reduce((s,d)=>s+(d[k]||0),0);
      return {combo, price:best.price+pickPrice, diff:budget-(best.price+pickPrice),
              energy:best.energy+sum('energy'), protein:best.protein+sum('protein'),
              fat:best.fat+sum('fat'), carb:best.carb+sum('carb'),
              balanced:combo.some(d=>MAIN.includes(d.category)),
              hasCarb:combo.some(d=>CARB.includes(d.category))};
    });
  }
  let body;
  if(!cs.length){
    body='<div class="combo-empty">¥'+budget+'以内の組み合わせが見つからなかった〜！予算や指定を変えてみて</div>';
  } else {
    body=cs.map((c,i)=>{
      const names=c.combo.map(d=>d.name+'('+d.category+')').join(' + ');
      const b=(c.balanced?'⭐主菜あり ':'')+(c.hasCarb?'🍚炭水化物あり':'')||'🍃軽め';
      return '<div class="combo-card"><div class="combo-rank">#'+(i+1)+'</div>'+
        '<div class="combo-names">'+names+'</div>'+
        '<div class="combo-stats"><b>¥'+c.price+'</b> <span class="diff">(残¥'+c.diff+')</span> / '+
        c.energy+'kcal / P'+c.protein.toFixed(1)+' F'+c.fat.toFixed(1)+' C'+c.carb.toFixed(1)+'</div>'+
        '<div class="combo-badge">'+b+'</div></div>';
    }).join('');
  }
  const sztag = onlySize ? '（🍚'+onlySize+'）' : '';
  const rtag = rice ? '（'+rice.name+'）' : '';
  const dtag = dessert ? '（🍰'+dessert.name+'）' : '';
  const mlabel = MODE_LABEL[mode] || '🎫 予算スレスレ';
  return '<div class="sec-h">'+mlabel+'最適化 TOP3（¥<span class="bv">'+budget+'</span>）'+sztag+rtag+dtag+'</div>'+body;
}

DATA.shops.forEach((s) => {
  const btn = document.createElement('button');
  btn.className = 'tab'; btn.dataset.key = s.key;
  btn.textContent = s.emoji + ' ' + s.name.replace('キャンパス ', ' / ');
  btn.onclick = () => { location.hash = s.key; };
  tabs.appendChild(btn);

  const panel = document.createElement('div');
  panel.className = 'panel'; panel.id = 'panel-' + s.key;
  const dayTabs = s.days.map((dy, i) =>
    '<button class="daytab' + (i === 0 ? ' active' : '') + (dy.weekend ? ' weekend' : '') +
    '" data-day="' + i + '">' + dy.date + '<small>' + dy.wday + '</small></button>'
  ).join('');
  const dayViews = s.days.map((dy, i) =>
    '<div class="dayview" data-day="' + i + '"' + (i === 0 ? '' : ' hidden') + '>' +
      cardsHtml(dy.images) +
      (dy.dishes.length ? '<div class="dyn" id="dyn-' + s.key + '-' + i + '"></div>' : '') +
    '</div>'
  ).join('');
  panel.innerHTML = '<h2>' + s.emoji + ' ' + s.name + '</h2>' +
    '<div class="daytabs">' + dayTabs + '</div>' + dayViews;
  panel.querySelectorAll('.daytab').forEach((b) => {
    b.onclick = () => {
      panel.querySelectorAll('.daytab').forEach((x) => x.classList.toggle('active', x === b));
      panel.querySelectorAll('.dayview').forEach((v) => { v.hidden = v.dataset.day !== b.dataset.day; });
    };
  });
  panels.appendChild(panel);
});

function applyHalf(dishes){
  // 🉐半額week: 全品50%OFF（menu CLI の apply_discount 相当）
  return dishes.map((d) => ({
    ...d,
    price: Math.max(0, Math.round(d.price / 2)),
    sizes: Object.fromEntries(Object.entries(d.sizes || {}).map(([k, v]) => [k, Math.max(0, Math.round(v / 2))])),
  }));
}
const riceSel = {};     // "shopkey-dayidx" → 選択中のご飯もの variant名
const dessertSel = {};  // "shopkey-dayidx" → 選択中のデザート名
function renderAll(){
  const budget = parseInt(document.getElementById('budget').value) || 740;
  const half = document.getElementById('half').checked;
  const onlySize = document.getElementById('rsize').value || null;
  const mode = document.getElementById('omode').value || '';
  DATA.shops.forEach((s) => s.days.forEach((dy, i) => {
    const el = document.getElementById('dyn-' + s.key + '-' + i);
    if (!el) return;
    const dishes = half ? applyHalf(dy.dishes) : dy.dishes;
    const key = s.key + '-' + i;
    // ご飯もの(丼/麺/ご飯)をサイズ展開（サイズ固定があればそれに従う）して選択肢に
    const riceVariants = expandSizes(dishes.filter((d) => CARB.includes(d.category)), onlySize);
    const riceName = riceSel[key] || '';
    const rice = riceName ? riceVariants.find((v) => v.name === riceName) : null;
    let rselHtml = '';
    if (riceVariants.length) {
      rselHtml = '<div class="dsel-bar">🍚 ご飯もの: <select class="rsel" data-key="' + key + '">' +
        '<option value="">おまかせ</option>' +
        riceVariants.map((v) => '<option value="' + v.name + '"' + (v.name === riceName ? ' selected' : '') + '>' + v.name + ' ¥' + v.price + '</option>').join('') +
        '</select></div>';
    }
    const dayDesserts = dishes.filter((d) => d.category === 'デザート');
    const desName = dessertSel[key] || '';
    const dessert = desName ? dayDesserts.find((d) => d.name === desName) : null;
    let dselHtml = '';
    if (dayDesserts.length) {
      dselHtml = '<div class="dsel-bar">🍰 デザート: <select class="dsel" data-key="' + key + '">' +
        '<option value="">なし</option>' +
        dayDesserts.map((d) => '<option value="' + d.name + '"' + (d.name === desName ? ' selected' : '') + '>' + d.name + ' ¥' + d.price + '</option>').join('') +
        '</select></div>';
    }
    el.innerHTML = (half ? '<div class="half-on">🉐 半額week適用中！ 全品50%OFFで計算中</div>' : '') +
      nutritionTable(dishes) + rselHtml + dselHtml + comboHtml(dishes, budget, onlySize, rice, dessert, mode);
  }));
}
document.getElementById('budget').addEventListener('input', renderAll);
document.getElementById('half').addEventListener('change', renderAll);
document.getElementById('rsize').addEventListener('change', renderAll);
document.getElementById('omode').addEventListener('change', renderAll);
document.addEventListener('change', (e) => {
  const t = e.target;
  if (!t.classList) return;
  if (t.classList.contains('rsel')) { riceSel[t.dataset.key] = t.value; renderAll(); }
  else if (t.classList.contains('dsel')) { dessertSel[t.dataset.key] = t.value; renderAll(); }
});
document.querySelectorAll('.budget-bar .preset').forEach((p) => {
  p.onclick = () => { document.getElementById('budget').value = p.dataset.v; renderAll(); };
});
renderAll();

function show(key) {
  if (!DATA.shops.some((s) => s.key === key)) key = DATA.shops[0].key;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.key === key));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + key));
}
show(location.hash.slice(1));
window.addEventListener('hashchange', () => show(location.hash.slice(1)));
</script>
</body>
</html>
"""


def _ping_heartbeat():
    """正常完了をGitHub外の死活監視(healthchecks.io等)へ通知する。
    HEARTBEAT_URL 未設定なら何もしない(ハルがsecret登録するまでno-op)。
    cronの自動無効化やYAML破損など「ジョブが一度も走らない」障害はActions内の
    ゲートでは原理的に検知できないため、外部サービスで鮮度を監視する。"""
    url = os.environ.get("HEARTBEAT_URL")
    if not url:
        return
    try:
        httpx.get(url, timeout=10)
        print("heartbeat ping OK", file=sys.stderr)
    except Exception as e:
        print(f"heartbeat ping失敗(無視): {e}", file=sys.stderr)


def main():
    shops = fetch_all()
    jst = timezone(timedelta(hours=9))
    payload = {
        "updated": datetime.now(jst).strftime("%Y-%m-%d %H:%M JST"),
        "shops": [{"key": s["key"], "name": s["name"], "emoji": s["emoji"],
                   "days": s["days"]} for s in shops],
    }
    out = Path(__file__).resolve().parent / "index.html"
    tok = load_design_tokens()
    html = TEMPLATE.replace("__CSSVARS__", tokens_to_css_vars(tok))
    html = html.replace("__DATA__", json.dumps(payload, ensure_ascii=False))

    # 健全性ゲート: OCRが新規画像の過半数で例外死する環境全滅(バグ1型)を検出したら
    # index.htmlを上書きせず exit 1 する。CIが赤くなり、Pagesは直前の良版のまま残る。
    ok, reason = health_verdict(STATS)
    print(f"health: {reason}", file=sys.stderr)
    if not ok:
        print(f"❌ 健全性ゲート NG: {reason} — index.htmlを更新せず終了", file=sys.stderr)
        sys.exit(1)

    out.write_text(html, encoding="utf-8")
    print(f"✅ 生成完了: {out}", file=sys.stderr)
    _ping_heartbeat()


if __name__ == "__main__":
    main()
