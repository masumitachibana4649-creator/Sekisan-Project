import json
from pathlib import Path
from urllib import error, request

from django.conf import settings


class SupabaseStorageError(RuntimeError):
    pass


def is_configured():
    return bool(settings.SUPABASE_URL and settings.SUPABASE_SECRET_KEY and settings.SUPABASE_BUCKET)


def upload_pdf(file_obj, object_path):
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
    _require_config()
    expires_in = expires_in or settings.SUPABASE_SIGNED_URL_EXPIRES_IN
    url = _storage_url(f"object/sign/{settings.SUPABASE_BUCKET}/{object_path}")
    payload = json.dumps({"expiresIn": int(expires_in)}).encode("utf-8")
    headers = _json_headers()
    response = _send(url, method="POST", headers=headers, data=payload)
    signed_url = response.get("signedURL") or response.get("signedUrl")
    if not signed_url:
        raise SupabaseStorageError("署名付きURLを発行できませんでした。")
    if signed_url.startswith("http"):
        return signed_url
    if signed_url.startswith("/storage/v1/"):
        return f"{settings.SUPABASE_URL.rstrip('/')}{signed_url}"
    return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/{signed_url.lstrip('/')}"


def download_pdf(object_path):
    _require_config()
    url = _storage_url(f"object/{settings.SUPABASE_BUCKET}/{object_path}")
    response = _send(url, method="GET", headers=_auth_headers(), expect_json=False)
    return response


def delete_pdf(object_path):
    _require_config()
    url = _storage_url(f"object/{settings.SUPABASE_BUCKET}")
    payload = json.dumps({"prefixes": [object_path]}).encode("utf-8")
    _send(url, method="DELETE", headers=_json_headers(), data=payload)


def _require_config():
    if not is_configured():
        raise SupabaseStorageError("Supabase Storageの環境変数が設定されていません。")


def _storage_url(path):
    return f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/{path.lstrip('/')}"


def _auth_headers():
    key = settings.SUPABASE_SECRET_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


def _json_headers():
    headers = _auth_headers()
    headers["Content-Type"] = "application/json"
    return headers


def _send(url, method, headers, data=None, expect_json=True):
    req = request.Request(url, method=method, headers=headers, data=data)
    try:
        with request.urlopen(req, timeout=settings.SUPABASE_STORAGE_TIMEOUT_SECONDS) as response:
            body = response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SupabaseStorageError(f"Supabase Storage APIエラー: {exc.code} {detail}") from exc
    except error.URLError as exc:
        raise SupabaseStorageError(f"Supabase Storageへ接続できませんでした: {exc.reason}") from exc

    if not expect_json:
        return body
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise SupabaseStorageError("Supabase Storage APIの応答を解析できませんでした。") from exc


def safe_filename(filename):
    return Path(filename or "drawing.pdf").name or "drawing.pdf"
