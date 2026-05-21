# 壁紙積算システム

企画書「壁紙積算システムプロジェクト」に合わせたDjango製Webアプリです。

## 主な機能

- PDF図面ファイルの添付
- 部屋別の周長、天井高、開口部、天井面積入力
- ロス率を含めた必要壁紙面積の計算
- ロール本数と概算金額の算出
- CSVダウンロード
- 管理画面での案件確認
- 広告枠サンプル表示

## 起動方法

```bash
python3 manage.py migrate
python3 manage.py runserver 127.0.0.1:8000
```

ブラウザで `http://127.0.0.1:8000/` を開きます。

## 計算式

```text
壁面積 = 周長 × 天井高 - 開口部面積
必要面積 = (壁面積 + 天井面積) × (1 + ロス率 / 100)
ロール本数 = 必要面積 ÷ (ロール幅 × ロール長さ) を部屋ごとに切り上げ
概算金額 = 合計ロール本数 × 1ロール単価
```

PDFからの自動読取はOpenAI APIを使ったAI抽出として実装しています。APIキーが未設定の場合は、PDF自動読取を使わず手入力で積算できます。

## PDF AI読取

PDF自動読取を有効にする場合は、OpenAI APIキーを環境変数に設定します。

```bash
OPENAI_API_KEY=sk-...
OPENAI_PDF_ANALYSIS_MODEL=gpt-4o
```

AIはPDF図面から部屋名、周長、天井高、開口部面積、天井面積だけを抽出します。ロス率込みの必要面積、ロール本数、概算金額はDjango側の計算式で算出します。

## デプロイ

Render などの Python/Django 対応ホスティングで動かせます。

- Build command: `./build.sh`
- Start command: `gunicorn wallpaper_estimator.wsgi:application --bind 0.0.0.0:$PORT`
- 必須環境変数: `SECRET_KEY`, `DEBUG=False`, `DATABASE_URL`
- 管理ユーザー自動作成: `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD`
- 必要に応じて設定: `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`
- PostgreSQL を使う場合: `DATABASE_URL`
- PDF AI読取を使う場合: `OPENAI_API_KEY`, `OPENAI_PDF_ANALYSIS_MODEL`

Render の Blueprint を使う場合は `render.yaml` を読み込ませ、作成後に発行されたホスト名を `ALLOWED_HOSTS` と `CSRF_TRUSTED_ORIGINS` に設定します。
