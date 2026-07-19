"""健全性ゲート health_verdict() の回帰テスト（paddle不要・高速）。

Sonnet5 の指摘（「ゲート自体のテストが無いと恒久ブロック/恒久素通りに
倒れうる」）への対応。バグ1型の全滅・週末の正当な空・コールドスタート・
境界(ちょうど50%)を機械的に固定する。
"""
import build_ocr as B


def V(cache_hit=0, ocr_ok=0, ocr_crash=0, dl_fail=0):
    return B.health_verdict({
        "cache_hit": cache_hit, "ocr_ok": ocr_ok,
        "ocr_crash": ocr_crash, "dl_fail": dl_fail,
    })


def test_normal_all_ok():
    ok, _ = V(ocr_ok=20)
    assert ok is True


def test_all_crash_is_ng():
    # バグ1型: 新規画像が全部OCR例外 → 全滅と判定してNG
    ok, reason = V(ocr_crash=20)
    assert ok is False
    assert "全滅" in reason


def test_minor_crash_stays_ok():
    # 一部だけcrash(<50%)は通す（個別画像の稀な失敗で誤発報しない）
    ok, _ = V(ocr_ok=18, ocr_crash=2)
    assert ok is True


def test_majority_crash_is_ng():
    ok, _ = V(ocr_ok=8, ocr_crash=12)
    assert ok is False


def test_exactly_half_is_ok():
    # ちょうど50%は通す（判定は「> 上限」なので）
    ok, _ = V(ocr_ok=10, ocr_crash=10)
    assert ok is True


def test_weekend_empty_is_ok():
    # 週末等: OCR自体は成功(ocr_ok)しdishesが空でvalid=Falseなだけ。
    # crashではないので誤発報しない。
    ok, _ = V(ocr_ok=6, ocr_crash=0)
    assert ok is True


def test_cold_start_is_skipped():
    # 全キャッシュヒット/新規解析0 は判定スキップ＝正常扱い
    ok, reason = V(cache_hit=53)
    assert ok is True
    assert "スキップ" in reason


def test_dl_fail_only_is_skipped():
    # DL失敗のみ(OCR未実行)も判定スキップ（OCR環境の健全性とは別軸）
    ok, _ = V(dl_fail=3)
    assert ok is True
