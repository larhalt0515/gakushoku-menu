# menu-site 運用・復旧 HANDOFF

学食メニュー有志サイトの堅牢化(2026-07-19, Fable5/Opus4.8/Sonnet5 の3モデル相談)で
入れた検知網の「何が鳴るか」と「鳴った時に何をするか」をまとめる。片手間運用でも
復旧できるように。

## 検知網の全体像

| 層 | 何を見る | 鳴り方 |
|---|---|---|
| 純関数テスト (CI) | 分類器/パーサの回帰 | CI赤 → `ci-failure` Issue |
| OCRスモーク (CI) | paddle推論が実際に動くか | CI赤 → `ci-failure` Issue |
| 健全性ゲート (build_ocr.py) | OCRクラッシュ率(全滅検知) | CI赤・index.html非更新 → `ci-failure` Issue |
| watchdog.yml (1日1回) | index.htmlの更新停止 | `update-stalled` Issue |
| 外部ハートビート (healthchecks.io) | 「ジョブが走らない」系 | ハルへ直接通知(要登録・後述) |

ゲート/テストはどれも「落ちたら Build/push に到達させない」ので、壊れた index.html で
本番ページが上書きされることはない(直前の良版が残る)。

## Issue が立ったら

### `ci-failure`（CIが赤）
1. Actions のログで赤いステップを見る。
2. **Unit tests 赤** → 分類器/パーサのコード変更が回帰。該当テストを読んで直す。
3. **OCR smoke 赤** → paddle の OCR 環境が壊れた(バグ1型)。
   - まず `requirements-ocr.txt` の `paddlepaddle==3.3.1` を **3.2.2** に下げて再実行(known-good)。
   - `build_ocr.py get_ocr()` の `enable_mkldnn=False` が効いているか確認。
4. **健全性ゲート赤(OCR環境全滅の疑い)** → 上と同じ。index.html は上書きされていない
   (良版維持)ので急がず原因を直す。
5. 直したら push。CI が緑になったら `gh issue close <番号>`。

### `update-stalled`（更新停止）
- **夏休み等の長期休業中なら正常**(学食が閉まると画像が更新されない)。無視して close でよい。
- 営業期間中なら cron停止/全run失敗/YAML破損の疑い。Actions の schedule 実行履歴を確認。
  - cron が止まっていたら手動: `gh workflow run update-menu`。
  - 60日無活動で GitHub が schedule を自動無効化することがある → Actions タブで再有効化。

## 手動操作チートシート
- 強制再ビルド: `gh workflow run update-menu`
- watchdog 手動チェック: `gh workflow run watchdog.yml`
- 特定画像を再OCRさせる: `cache/<画像ID>.json` を削除して push → 次回 run で再解析
- 抽出ロジック(`_extract_price` 等)を直して全キャッシュ再解析したい:
  `build_ocr.py` の `PARSER_VERSION` を +1 → v2キャッシュは自動で再解析される
  (旧スキーマのファイルは手動削除)

## 外部ハートビート(healthchecks.io)の登録 ※ハル手動・任意
「ジョブが一度も走らない」障害(cron自動無効化・YAML破損・GitHub障害)は Actions 内の
ゲートでは原理的に検知できないため、外部サービスで鮮度を監視する。
1. https://healthchecks.io で無料アカウントを作り、チェックを1つ作る(period=1day, grace=12h 目安)。
2. その ping URL をコピー。
3. GitHub リポの Settings → Secrets and variables → Actions で
   `HEARTBEAT_URL` という名前の secret に貼る。
4. 以降 update-menu が正常完了するたび ping が飛ぶ。一定時間 ping 途絶で healthchecks.io が
   メール等で通知。
未登録でも `build_ocr.py` 側は no-op なので害はない。

## テストの回し方
- 純関数(paddle不要・数秒): `python -m pytest tests/ --ignore=tests/test_smoke_ocr.py -q`
- OCRスモーク(paddle・数分): `python -m pytest tests/test_smoke_ocr.py -q`

## 未着手・今後(ロードマップ Phase4+ 相当)
- 上流(生協signage)のHTMLレイアウト変更ドリフト検知(現状は固定fixtureのみ)。
- 組立後payloadの不変条件アサート(各店1日以上/表示日付が本家ナビと一致 等)。
- 学食営業日カレンダーに基づく watchdog 閾値の精緻化(現状は緩め60h固定)。
