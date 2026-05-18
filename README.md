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

PDFからの自動読取は将来拡張用として、現時点ではPDF添付と手入力のMVPにしています。

## デプロイ

Render などの Python/Django 対応ホスティングで動かせます。

- Build command: `./build.sh`
- Start command: `gunicorn wallpaper_estimator.wsgi:application`
- 必須環境変数: `SECRET_KEY`, `DEBUG=False`, `DATABASE_URL`
- 管理ユーザー自動作成: `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD`
- 必要に応じて設定: `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`
- PostgreSQL を使う場合: `DATABASE_URL`

Render の Blueprint を使う場合は `render.yaml` を読み込ませ、作成後に発行されたホスト名を `ALLOWED_HOSTS` と `CSRF_TRUSTED_ORIGINS` に設定します。
