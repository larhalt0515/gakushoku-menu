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
