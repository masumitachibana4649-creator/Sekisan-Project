import json
from pathlib import Path
from urllib import error, request

from django.conf import settings


class SupabaseStorageError(RuntimeError):
    """Supabase Storage連携で発生したエラーを表す例外。"""

    pass


def is_configured():
    """Supabase Storage連携に必要な設定がそろっているかを返す。

    Returns:
        必要な設定がすべて存在する場合はTrue、それ以外はFalse。
    """
    return bool(settings.SUPABASE_URL and settings.SUPABASE_SECRET_KEY and settings.SUPABASE_BUCKET)


def upload_pdf(file_obj, object_path):
    """PDFファイルをSupabase Storageへアップロードする。

    Args:
        file_obj: アップロード対象のPDFファイルオブジェクト。
        object_path: Supabase Storage上の保存先パス。
    """
    _require_config()
    file_obj.seek(0)
    data = file_obj.read()
    file_obj.seek(0)

    url = _storage_url(f"object/{settings.SUPABASE_BUCKET}/{object_path}")
    headers = _auth_headers()
    headers.update(
        {
            "Content-Type": "application/pdf",
            "x-upsert": "false",
        }
    )
    _send(url, method="POST", headers=headers, data=data)


def create_signed_url(object_path, expires_in=None):
    """Supabase Storage上のPDFにアクセスする署名付きURLを発行する。

    Args:
        object_path: Supabase Storage上のPDFファイルパス。
        expires_in: 署名付きURLの有効期限秒数。未指定時は設定値を使用する。

    Returns:
        PDFへアクセスするための署名付きURL。
    """
    _require_config()
    expires_in = expires_in or settings.SUPABASE_SIGNED_URL_EXPIRES_IN
    url = _storage_url(f"object/sign/{settings.SUPABASE_BUCKET}/{object_path}")
    payload = json.dumps({"expiresIn": int(expires_in)}).encode("utf-8")
    headers = _json_headers()
    response = _send(url, method="POST", headers=headers, data=payload)
    signed_url = response.get("signedURL") or response.get("signedUrl")
    if not signed_url:
        raise SupabaseStorageError("署名付きURLを発行できませんでした。")

    # Supabaseの応答形式に差があるため、絶対URLと相対パスの両方を正規化する
    if signed_url.startswith("http"):
        return signed_url
    if signed_url.startswith("/storage/v1/"):
        return f"{settings.SUPABASE_URL.rstrip('/')}{signed_url}"
    return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/{signed_url.lstrip('/')}"


def download_pdf(object_path):
    """Supabase StorageからPDFファイルのバイナリを取得する。

    Args:
        object_path: Supabase Storage上のPDFファイルパス。

    Returns:
        取得したPDFファイルのバイナリ。
    """
    _require_config()
    url = _storage_url(f"object/{settings.SUPABASE_BUCKET}/{object_path}")
    response = _send(url, method="GET", headers=_auth_headers(), expect_json=False)
    return response


def delete_pdf(object_path):
    """Supabase Storage上のPDFファイルを削除する。

    Args:
        object_path: 削除対象のSupabase Storage上のPDFファイルパス。
    """
    _require_config()
    url = _storage_url(f"object/{settings.SUPABASE_BUCKET}")
    payload = json.dumps({"prefixes": [object_path]}).encode("utf-8")
    _send(url, method="DELETE", headers=_json_headers(), data=payload)


def _require_config():
    """Supabase Storage連携に必要な設定がなければ例外を送出する。"""
    if not is_configured():
        raise SupabaseStorageError("Supabase Storageの環境変数が設定されていません。")


def _storage_url(path):
    """Supabase Storage APIのURLを組み立てる。

    Args:
        path: Storage APIの`/storage/v1/`以降のパス。

    Returns:
        Supabase Storage APIの絶対URL。
    """
    return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/{path.lstrip('/')}"


def _auth_headers():
    """Supabase Storage APIの認証ヘッダーを返す。

    Returns:
        APIキーとBearerトークンを含むヘッダー。
    """
    key = settings.SUPABASE_SECRET_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


def _json_headers():
    """JSONリクエスト用のSupabase Storage APIヘッダーを返す。

    Returns:
        認証情報とJSONのContent-Typeを含むヘッダー。
    """
    headers = _auth_headers()
    headers["Content-Type"] = "application/json"
    return headers


def _send(url, method, headers, data=None, expect_json=True):
    """Supabase Storage APIへリクエストを送信し、応答を返す。

    Args:
        url: リクエスト先のURL。
        method: HTTPメソッド。
        headers: リクエストヘッダー。
        data: 送信するリクエストボディ。
        expect_json: 応答をJSONとして解析する場合はTrue。

    Returns:
        JSON応答の辞書、または`expect_json`がFalseの場合は応答バイナリ。
    """
    req = request.Request(url, method=method, headers=headers, data=data)
    try:
        with request.urlopen(req, timeout=settings.SUPABASE_STORAGE_TIMEOUT_SECONDS) as response:
            body = response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SupabaseStorageError(f"Supabase Storage APIエラー: {exc.code} {detail}") from exc
    except error.URLError as exc:
        raise SupabaseStorageError(f"Supabase Storageへ接続できませんでした: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise SupabaseStorageError(f"Supabase Storageへの通信でエラーが発生しました: {exc}") from exc

    if not expect_json:
        return body
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SupabaseStorageError("Supabase Storage APIの応答を解析できませんでした。") from exc


def safe_filename(filename):
    """パス要素を除いた安全なファイル名を返す。

    Args:
        filename: 利用者から渡されたファイル名。

    Returns:
        パス要素を除去したファイル名。空の場合はデフォルト名。
    """
    return Path(filename or "drawing.pdf").name or "drawing.pdf"
