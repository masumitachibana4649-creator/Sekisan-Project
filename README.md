# 壁紙積算システム

PDF図面から壁紙積算に必要な部屋情報を読み取り、壁紙面積、ロール本数、概算金額を計算するDjango製Webアプリです。

<h2><span style="color:red">本番環境</span></h2>

<h3><span style="color:red">URL</span></h3>

<span style="color:red">https://new-project-2-yj6p.onrender.com/</span>

<h3><span style="color:red">確認用アカウント</span></h3>

<span style="color:red">ID:testuser</span><br>
<span style="color:red">Pass:sekisanuser</span>

## 主な機能

- ユーザー登録、ログイン、ログアウト
- ログインユーザーごとの案件管理
- PDF図面アップロード
- OpenAI APIによるPDF図面のAI読取
- 平面図、天井伏図、展開図、床面積表、居室区画面積表、仕上表、建具表のページ指定
- 表ページや平面図テキストからの部屋候補検出
- 部屋別の周長、天井高、開口部面積、天井面積、面別壁紙の編集
- AIで抽出できなかった部屋の追加表示
- 手動での部屋追加
- 部屋ごとの集計対象外設定
- 壁紙別合算方式と部屋別積上方式の見積切り替え
- 編集内容の同一案件反映、または別案件としての保存
- PDF再解析
- 図面PDF表示
- 積算明細CSVダウンロード
- 管理画面での案件、部屋、壁紙マスタ、初期値設定の管理
- Supabase Storageを使ったPDF保存

## 起動方法

```bash
python3 manage.py migrate
python3 manage.py runserver 127.0.0.1:8000
```

ブラウザで `http://127.0.0.1:8000/` を開きます。

管理ユーザーを作成する場合:

```bash
python3 manage.py createsuperuser
```

## 計算式

面別入力がある場合は、各面の面積から開口部面積を控除して施工面積を計算します。面別入力がない場合は、周長、天井高、開口部面積、天井面積から計算します。

```text
壁面積 = 周長 × 天井高 - 開口部面積
施工面積 = 壁面積 + 天井面積
必要面積 = 施工面積 × (1 + ロス率 / 100)
ロール面積 = ロール幅 × ロール長さ
ロール本数 = 必要面積 ÷ ロール面積 を切り上げ
概算金額 = ロール本数 × 1ロール単価
```

見積方式は次の2種類です。

- 壁紙別合算方式: 壁紙ごとに必要面積を合算してからロール本数を切り上げます。
- 部屋別積上方式: 部屋ごと、壁紙ごとにロール本数を切り上げてから合算します。

## PDF AI読取

PDF自動読取を有効にする場合は、OpenAI APIキーを環境変数に設定します。

```bash
OPENAI_API_KEY=sk-...
OPENAI_PDF_ANALYSIS_MODEL=gpt-5.5
```

AIはPDF図面から、部屋名、周長、天井高、開口部面積、天井面積、1面から4面までの壁面情報を抽出します。ロス率込みの必要面積、ロール本数、概算金額はDjango側の計算式で算出します。

テキスト抽出だけで表ページを判定できないPDFでは、設定によりAI画像解析で表ページ検出を補助できます。

```bash
OPENAI_VISUAL_TABLE_PAGE_DETECTION=true
OPENAI_TABLE_PAGE_DETECTION_MAX_PAGES=60
```

## PDF保存

Supabase Storageの環境変数が設定されている場合、アップロードPDFはSupabase Storageへ保存されます。未設定の場合はDjangoのローカルファイルストレージへ保存されます。

```bash
SUPABASE_URL=https://example.supabase.co
SUPABASE_SECRET_KEY=sb_secret_xxxxxxxxx
SUPABASE_BUCKET=pdfs
SIGNED_URL_EXPIRES_IN=600
SUPABASE_STORAGE_TIMEOUT_SECONDS=30
PDF_MAX_UPLOAD_SIZE=10485760
```

## デプロイ

RenderなどのPython/Django対応ホスティングで動かせます。

- Build command: `./build.sh`
- Start command: `gunicorn wallpaper_estimator.wsgi:application --bind 0.0.0.0:$PORT --timeout 180`
- 必須環境変数: `SECRET_KEY`, `DEBUG=False`, `DATABASE_URL`
- 管理ユーザー自動作成: `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD`
- 必要に応じて設定: `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`
- PostgreSQLを使う場合: `DATABASE_URL`
- PDF AI読取を使う場合: `OPENAI_API_KEY`, `OPENAI_PDF_ANALYSIS_MODEL`
- AI画像解析で表ページ検出を使う場合: `OPENAI_VISUAL_TABLE_PAGE_DETECTION`, `OPENAI_TABLE_PAGE_DETECTION_MAX_PAGES`
- Supabase Storageを使う場合: `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_BUCKET`

RenderのBlueprintを使う場合は `render.yaml` を読み込ませ、作成後に発行されたホスト名を `ALLOWED_HOSTS` と `CSRF_TRUSTED_ORIGINS` に設定します。
