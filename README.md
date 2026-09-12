# 🍱 日福 学食メニュー（非公式）

日本福祉大学の学食メニューを、キャンパス選択なしでサッと見れる非公式ページ。

**公開URL**: https://larhalt0515.github.io/gakushoku-menu/

- 🏫 半田パッキア → `#pacchia`
- 🌊 美浜 食菜 → `#shokusai`
- 🚗 東海 ルポ → `#lupo`

URL末尾にハッシュを付けると、そのキャンパスを直接開けます（例: `.../gakushoku-menu/#pacchia`）。

## 更新のしかた

本家サイトをスクレイプして `index.html` を作り直します。画面のPFCガイドは
生成データと分離した `pfc.js` から読み込むため、再生成後もそのまま利用できます。

```bash
~/.local/menu-venv/bin/python3 build.py
git add index.html && git commit -m "update menu" && git push
```

## しくみ

本家 `signage.univcoop-tokai.net` は「トップに GET でセッション確立 → `shop_id` を POST」しないと
店舗メニューを返さない作り。`build.py` がその2連リクエストでメニュー画像URLを取得し、
3キャンパス分を埋め込んだ自己完結 HTML を生成します（Python の `httpx` のみ、ブラウザ不要）。

メニュー画像は本家から直接配信（このリポジトリには画像を持ちません）。

## 注意

非公式・有志ページです。本家への負荷を避けるため、スクレイプは控えめに（1日数回程度）。
