"""Подпись запроса AWS Signature Version 4 на стандартной библиотеке (без boto3).

Postbox принимает API, совместимый с AWS SES, с подписью SigV4: регион ru-central1, сервис ses
(https://yandex.cloud/ru/docs/postbox/operations/send-email). Порядок подписи —
https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html.
Подписываются все переданные заголовки; путь без query-строки (Postbox она не нужна).
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def canonical_request(method: str, path: str, headers: dict[str, str], payload_hash: str) -> tuple[str, str]:
    """(канонический запрос, список подписанных заголовков)."""
    names = sorted(name.lower() for name in headers)
    values = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    canonical_headers = "".join(f"{name}:{values[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_path = quote(path or "/", safe="/-_.~")
    request = "\n".join([method.upper(), canonical_path, "", canonical_headers, signed_headers, payload_hash])
    return request, signed_headers


def sign(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    key_id: str,
    secret_key: str,
    region: str,
    service: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Заголовки запроса с добавленными Host (если нет), X-Amz-Date и Authorization."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    parts = urlsplit(url)
    signed = dict(headers)
    if not any(name.lower() == "host" for name in signed):
        signed["Host"] = parts.netloc
    signed["X-Amz-Date"] = amz_date
    payload_hash = hashlib.sha256(body).hexdigest()
    request, signed_headers = canonical_request(method, parts.path, signed, payload_hash)
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(request.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, date, region, service), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    signed["Authorization"] = (
        f"{ALGORITHM} Credential={key_id}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    return signed
