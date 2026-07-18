"""分類・正規化・妥当性判定の回帰テスト（paddle不要・高速）。

バグ2(デザートが「その他」に沈んで絞り込みUIが消えた件)の実データを
恒久ガード化する。以降 _guess_category を触っても後退0件を機械保証。
"""
import pytest

import build_ocr as B


# --- バグ2の回帰: 「その他」に沈んでいたデザートの救済を固定 ---
@pytest.mark.parametrize("name", [
    "エクレア",
    "フルーツミックスヨーグルト",
    "あまおう莓ムース",
    "チョコクレープ",
    "大学芋",
    "キャラメルナッツショート",
])
def test_dessert_rescued(name):
    assert B._guess_category(name) == "デザート"


# --- 既存デザートも維持されること ---
@pytest.mark.parametrize("name", [
    "いももち", "焼きプリンタルト", "りんごのタルト", "さつま芋と栗のタルト",
])
def test_dessert_existing(name):
    assert B._guess_category(name) == "デザート"


# --- 各カテゴリの代表例（分類順序に依存する境界を含む）---
@pytest.mark.parametrize("name,cat", [
    ("ライス", "ご飯"),
    ("チキンカツカレー", "丼"),      # カレー → 丼（カツより優先）
    ("ビビンバ丼", "丼"),
    ("担々麺", "麺"),
    ("かき揚げうどん", "麺"),         # うどん → 麺（揚げより優先）
    ("豚汁", "汁物"),
    ("味噌汁", "汁物"),
    ("ミニサラダ", "サラダ"),
    ("蒸し鶏わかめサラダ", "サラダ"),
    ("塩だれハンバーグ", "主菜"),
    ("さば塩焼き", "主菜"),
    ("ほうれん草胡麻和え", "小鉢"),
    ("さば生姜煮", "小鉢"),
])
def test_category_representatives(name, cat):
    assert B._guess_category(name) == cat


# --- 誤爆防止: 主食/主菜/汁物/小鉢/サラダがデザートに化けない ---
@pytest.mark.parametrize("name", [
    "チキンカツカレー", "担々麺", "塩だれハンバーグ",
    "豚汁", "ミニサラダ", "さば生姜煮",
])
def test_no_false_dessert(name):
    assert B._guess_category(name) != "デザート"


# --- normalize_dish: category は料理名から毎回導出（cacheの古い値に依存しない）---
def test_normalize_category_is_rederived():
    # 入力の category が古い「その他」でも、名前から再導出される
    d = B.normalize_dish({"name": "エクレア", "price": 100, "category": "その他"})
    assert d["category"] == "デザート"
    d2 = B.normalize_dish({"name": "チキンカツカレー", "price": 300})
    assert d2["category"] == "丼"


def test_normalize_name_fix():
    d = B.normalize_dish({"name": "スカツカレー", "price": 528})
    assert d["name"] == "ロースカツカレー"


def test_normalize_price_and_sizes():
    # 「中」があれば中を代表価格に
    d = B.normalize_dish({"name": "x", "sizes": {"小": 440, "中": 528, "大": 660}})
    assert d["price"] == 528
    assert d["sizes"] == {"小": 440, "中": 528, "大": 660}
    # サイズ無しは price を「並」に
    d2 = B.normalize_dish({"name": "y", "price": 297})
    assert d2["price"] == 297
    assert d2["sizes"] == {"並": 297}


# --- is_valid_menu: 告知ポスター等を弾く境界 ---
def test_is_valid_menu_boundaries():
    assert B.is_valid_menu([]) is False
    assert B.is_valid_menu([{"name": "a", "price": 100}]) is False           # 1品
    assert B.is_valid_menu([{"name": "a", "price": 100},
                            {"name": "b", "price": 200}]) is True            # 2品全価格
    assert B.is_valid_menu([{"name": "a", "price": 100},
                            {"name": "b", "price": 0}]) is False             # 価格付き1品<必要2
    assert B.is_valid_menu([{"name": "a", "price": 1}, {"name": "b", "price": 1},
                            {"name": "c", "price": 0}, {"name": "d", "price": 0}]) is True  # 4品中2品価格
