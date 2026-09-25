"""HTMLパーサの回帰テスト（paddle不要・高速）。

日付ナビと画像URLの抽出を固定する。特に parse_day_nav は
BeautifulSoup 4.15 系が href 内の "&current_day" を実体参照として
復号し壊す罠(ローカル4.14で動きCI4.15で壊れる)を、生HTML正規表現で
回避している。その回避が効いていることをスナップショットで固定する。
"""
from datetime import date

import build_ocr as B


# --- parse_day_nav: bs4 4.15 の href実体参照罠の回帰 ---
def test_parse_day_nav_ampersand_before_current():
    today = date(2026, 7, 19)
    # current_day の直前が "&" でも壊れず index を拾えること
    html = (
        '<a href="?shop=29&current_day=0&client_id=13">7／19</a>'
        '<a href="?shop=29&current_day=1&client_id=13">7／22</a>'
        '<a href="?shop=29&current_day=2&client_id=13">7／23</a>'
    )
    nav = B.parse_day_nav(html, today)
    assert nav == [
        (0, date(2026, 7, 19)),
        (1, date(2026, 7, 22)),
        (2, date(2026, 7, 23)),
    ]


def test_parse_day_nav_slash_variants():
    today = date(2026, 7, 19)
    # 全角／と半角/の両対応
    html = '<a href="x?current_day=0">7/19</a><a href="y?current_day=1">7／22</a>'
    nav = dict(B.parse_day_nav(html, today))
    assert nav[0] == date(2026, 7, 19)
    assert nav[1] == date(2026, 7, 22)


def test_parse_day_nav_year_wrap():
    # 年末年始跨ぎ: 12/30 時点の 1／5 は翌年になる
    today = date(2026, 12, 30)
    html = '<a href="?current_day=0">1／5</a>'
    nav = B.parse_day_nav(html, today)
    assert nav[0][1] == date(2027, 1, 5)


def test_parse_day_nav_first_occurrence_wins():
    today = date(2026, 7, 19)
    # 同一 index が重複したら最初の出現を採用
    html = ('<a href="?current_day=0">7／19</a>'
            '<a href="?current_day=0">9／9</a>')
    nav = B.parse_day_nav(html, today)
    assert nav == [(0, date(2026, 7, 19))]


# --- parse_image_urls: URL補完（//→https:, /→signage, s.png→.png）---
def test_parse_image_urls_completion():
    html = (
        '<li class="item"><img src="//signage.univcoop-tokai.net/a/0000042843s.png"></li>'
        '<li class="item"><img src="/b/0000042868.png"></li>'
        '<li class="item"><span>no img</span></li>'
        '<li class="item"><img></li>'
    )
    urls = B.parse_image_urls(html)
    assert urls == [
        "https://signage.univcoop-tokai.net/a/0000042843.png",  # // 補完 + s.png除去
        "https://signage.univcoop-tokai.net/b/0000042868.png",  # / 補完
    ]


def test_parse_image_urls_http_to_https():
    html = '<li class="item"><img src="http://signage.univcoop-tokai.net/c/x.png"></li>'
    assert B.parse_image_urls(html) == ["https://signage.univcoop-tokai.net/c/x.png"]


def _yen(text, height, cx, cy=0):
    return {"text": text, "y0": 0, "y1": height, "cx": cx, "cy": cy}


def test_extract_price_does_not_infer_sizes_from_nearby_prices():
    price, sizes = B._extract_price([
        _yen("¥77", height=20, cx=0),
        _yen("¥99", height=20, cx=100),
    ], anchor_cx=100)
    assert price == 99
    assert sizes == {}


def test_extract_price_requires_multiple_explicit_size_labels():
    price, sizes = B._extract_price([
        _yen("小¥77", height=12, cx=0),
        _yen("¥99", height=20, cx=100),
    ], anchor_cx=100)
    assert price == 99
    assert sizes == {}


def test_extract_price_preserves_explicit_size_prices():
    price, sizes = B._extract_price([
        _yen("小¥440", height=12, cx=0),
        _yen("中¥528", height=20, cx=100),
        _yen("大¥660", height=12, cx=200),
    ], anchor_cx=100)
    assert price == 528
    assert sizes == {"小": 440, "中": 528, "大": 660}


def test_is_name_accepts_numeric_product_name_for_card_bounds():
    assert B._is_name({"text": "野菜生活100", "y0": 0, "y1": 100}, 50)
    assert not B._is_name({"text": "中528", "y0": 0, "y1": 100}, 50)


def test_extract_price_requires_center_size_label():
    price, sizes = B._extract_price([
        _yen("小¥77", height=12, cx=0),
        _yen("大¥99", height=12, cx=100),
    ], anchor_cx=100)
    assert price == 99
    assert sizes == {}


def test_extract_price_restores_aligned_unlabeled_sizes():
    price, sizes = B._extract_price([
        _yen("¥143", height=12, cx=0, cy=100),
        _yen("¥187", height=20, cx=100, cy=100),
        _yen("¥231", height=12, cx=200, cy=100),
    ], anchor_cx=100)
    assert price == 187
    assert sizes == {"小": 143, "中": 187, "大": 231}


def test_extract_price_ignores_extra_mini_price():
    price, sizes = B._extract_price([
        _yen("¥99", height=12, cx=0, cy=100),
        _yen("¥143", height=12, cx=40, cy=100),
        _yen("¥187", height=20, cx=100, cy=100),
        _yen("¥231", height=12, cx=160, cy=100),
    ], anchor_cx=100)
    assert price == 187
    assert sizes == {"小": 143, "中": 187, "大": 231}


def test_extract_price_completes_one_missing_size_label():
    price, sizes = B._extract_price([
        _yen("小¥143", height=12, cx=0, cy=100),
        _yen("中¥187", height=20, cx=100, cy=100),
        _yen("¥231", height=12, cx=200, cy=100),
    ], anchor_cx=100)
    assert price == 187
    assert sizes == {"小": 143, "中": 187, "大": 231}
