"""OCRスモークテスト（paddle実推論・低速）。

固定した実メニュー画像を毎回OCRし、料理がちゃんと取れることを確認する。
paddle 3.3.x の oneDNN/PIRバグ型「CI環境でだけOCRが全滅」を、cacheの
状態と無関係に Build 前へ確実に検出するためのゲート。

閾値の根拠: このfixture(shokusai 0000042959.png)をローカルvenv
(paddleocr 3.7.0 / paddlepaddle 3.3.1, enable_mkldnn=False)でOCRすると
priced=12 / energy非null=12 / valid=True が得られる(2026-07-19実測)。
環境差・OCR精度の変動マージンを見て閾値は半分の6に置く。0品や激減は
バグ1型のOCR環境全滅を意味するので、ここで即failさせる。
"""
import pathlib

import build_ocr as B

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "menu_sample.png"


def test_ocr_extracts_dishes():
    raw = FIXTURE.read_bytes()
    dishes = B.ocr_dishes(raw)
    norm = [B.normalize_dish(x) for x in dishes if x.get("name")]
    priced = [x for x in norm if x.get("price")]
    energy = [x for x in norm if x.get("energy") is not None]

    # paddle推論が壊れると全画像0品になる（バグ1のenable_mkldnn型を毎run検出）
    assert len(priced) >= 6, f"価格付き{len(priced)}品(<6)=OCR環境全滅の疑い"
    assert B.is_valid_menu(norm), "is_valid_menu=False（メニューとして成立せず）"
    assert len(energy) >= 1, "栄養(energy)抽出が全滅している"
