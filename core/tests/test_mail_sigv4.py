import hashlib
import unittest
from datetime import datetime, timezone

from . import _paths  # noqa: F401
from platform_core.mail.sigv4 import canonical_request, sign

# Пример из набора тестов подписи AWS: aws-c-auth, tests/aws-signing-test-suite/v4/post-x-www-form-urlencoded
# (https://github.com/awslabs/aws-c-auth). Ключи — учебные ключи из документации AWS, не настоящие.
KEY_ID = "AKIDEXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
NOW = datetime(2015, 8, 30, 12, 36, tzinfo=timezone.utc)
BODY = b"Param1=value1"
BODY_HASH = "9095672bbd1f56dfc5b65f3e153adc8731a4a654192329106275f4c7b24d0b6e"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Host": "example.amazonaws.com",
    "Content-Length": "13",
    "X-Amz-Content-Sha256": BODY_HASH,
}
EXPECTED_CANONICAL = (
    "POST\n/\n\n"
    "content-length:13\n"
    "content-type:application/x-www-form-urlencoded\n"
    "host:example.amazonaws.com\n"
    f"x-amz-content-sha256:{BODY_HASH}\n"
    "x-amz-date:20150830T123600Z\n\n"
    "content-length;content-type;host;x-amz-content-sha256;x-amz-date\n"
    f"{BODY_HASH}"
)
EXPECTED_SIGNATURE = "d3875051da38690788ef43de4db0d8f280229d82040bfac253562e56c3f20e0b"


class SigV4Test(unittest.TestCase):
    def test_body_hash(self):
        self.assertEqual(hashlib.sha256(BODY).hexdigest(), BODY_HASH)

    def test_canonical_request_matches_aws_suite(self):
        headers = dict(HEADERS, **{"X-Amz-Date": "20150830T123600Z"})
        request, signed = canonical_request("POST", "/", headers, BODY_HASH)
        self.assertEqual(request, EXPECTED_CANONICAL)
        self.assertEqual(signed, "content-length;content-type;host;x-amz-content-sha256;x-amz-date")

    def test_signature_matches_aws_suite(self):
        headers = sign("POST", "https://example.amazonaws.com/", HEADERS, BODY, KEY_ID, SECRET, "us-east-1", "service", now=NOW)
        self.assertEqual(headers["X-Amz-Date"], "20150830T123600Z")
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=content-length;content-type;host;x-amz-content-sha256;x-amz-date, "
            f"Signature={EXPECTED_SIGNATURE}",
        )

    def test_postbox_scope_and_host(self):
        headers = sign(
            "POST", "https://postbox.cloud.yandex.net/v2/email/outbound-emails",
            {"Content-Type": "application/json"}, b"{}", "key-id", "secret", "ru-central1", "ses", now=NOW,
        )
        self.assertEqual(headers["Host"], "postbox.cloud.yandex.net")
        self.assertIn("Credential=key-id/20150830/ru-central1/ses/aws4_request", headers["Authorization"])
        self.assertIn("SignedHeaders=content-type;host;x-amz-date,", headers["Authorization"])
        self.assertNotIn("secret", headers["Authorization"])

    def test_header_values_trimmed(self):
        request, _ = canonical_request("post", "/v2/x", {"X-Test": "  a   b  "}, "h")
        self.assertIn("\nx-test:a b\n", request)
        self.assertTrue(request.startswith("POST\n/v2/x\n\n"))


if __name__ == "__main__":
    unittest.main()
