"""cache読み込み(dishes_from_cache)の回帰テスト（paddle不要・高速）。

Phase3: cacheに導出値を焼かず生OCR出力(raw)だけ保存し、読み込み時に再導出する
スキーマv2と、旧スキーマ(dishes焼き込み)の後方互換を固定する。
「導出値をcacheに焼いて固着させる」バグ2型の再発を構造的に防ぐ。
"""
import build_ocr as B


def test_cache_v2_rederives_category():
    # v2: 生OCR(raw)から表示dishesを導出。categoryはrawの値でなく料理名から再判定
    cached = {"schema": 2, "pv": B.PARSER_VERSION, "raw": [
        {"name": "エクレア", "price": 100, "sizes": {}, "energy": 94, "category": "その他"},
        {"name": "チキンカツカレー", "price": 297, "sizes": {}, "energy": 351},
    ]}
    dishes = B.dishes_from_cache(cached)
    assert dishes is not None
    cats = {d["name"]: d["category"] for d in dishes}
    assert cats["エクレア"] == "デザート"        # rawの'その他'でなく再導出
    assert cats["チキンカツカレー"] == "丼"


def test_cache_v2_stale_pv_triggers_reanalyze():
    # パーサ版が上がった古いv2は None(=再解析させる)
    cached = {"schema": 2, "pv": B.PARSER_VERSION - 1, "raw": [{"name": "x", "price": 1}]}
    assert B.dishes_from_cache(cached) is None


def test_cache_v2_invalid_menu_returns_empty():
    # 品数不足(告知ポスター相当)は空
    cached = {"schema": 2, "pv": B.PARSER_VERSION, "raw": [{"name": "a", "price": 100}]}
    assert B.dishes_from_cache(cached) == []


def test_cache_legacy_backward_compat():
    # 旧スキーマ(dishes焼き込み): 「その他」だけ再判定、既存分類は保護
    cached = {"valid": True, "dishes": [
        {"name": "モンブラン", "price": 100, "category": "デザート", "sizes": {}},  # 既存分類は維持
        {"name": "エクレア", "price": 100, "category": "その他", "sizes": {}},      # その他→デザート救済
    ]}
    dishes = B.dishes_from_cache(cached)
    cats = {d["name"]: d["category"] for d in dishes}
    assert cats["モンブラン"] == "デザート"   # 後退させない
    assert cats["エクレア"] == "デザート"     # 救済


def test_cache_legacy_invalid_returns_empty():
    assert B.dishes_from_cache({"valid": False, "dishes": []}) == []
