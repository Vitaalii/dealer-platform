import asyncio
import contextlib
import io
import json
import logging
import unittest
from unittest import mock

from . import _paths  # noqa: F401
from platform_core.config import MailSettings
from platform_core.mail import __main__ as mail_cli
from platform_core.mail import MailPermanentError, MailTemporaryError, build_mailer, normalize_recipients
from platform_core.mail.log_mailer import LogMailer
from platform_core.mail.postbox import MAX_ATTEMPTS, PostboxMailer, TransportError

SECRET = "postbox-secret-must-not-leak"
TEXT = "Текст письма с телефоном +70000000000"
SETTINGS = MailSettings(
    provider="postbox",
    from_address="no-reply@notify.example.org",
    from_name="Платформа дилеров",
    postbox_key_id="key-id",
    postbox_secret_key=SECRET,
)


class FakeTransport:
    """Вместо HTTP: отдаёт ответы по очереди и запоминает запросы."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, url, headers, body):
        self.calls.append((url, headers, json.loads(body)))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return status, json.dumps(payload).encode()


class FakeTime:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def ok(message_id="msg-1"):
    return 200, {"MessageId": message_id}


def make(*responses, settings=SETTINGS):
    transport, fake_time = FakeTransport(*responses), FakeTime()
    mailer = PostboxMailer(settings, transport=transport, sleep=fake_time.sleep, clock=fake_time.clock, jitter=lambda: 0.0)
    return mailer, transport, fake_time


def run(coro):
    return asyncio.run(coro)


class RecipientsTest(unittest.TestCase):
    def test_trim_lower_dedupe(self):
        self.assertEqual(normalize_recipients([" A@Example.org ", "a@example.org", "b@example.org"]), ["a@example.org", "b@example.org"])

    def test_empty(self):
        with self.assertRaisesRegex(MailPermanentError, "no recipients"):
            normalize_recipients([])

    def test_invalid_reports_position_not_address(self):
        with self.assertRaises(MailPermanentError) as cm:
            normalize_recipients(["a@example.org", "secret-person"])
        self.assertEqual(str(cm.exception), "invalid recipient #2")

    def test_limit_200(self):
        self.assertEqual(len(normalize_recipients([f"u{i}@example.org" for i in range(200)])), 200)
        with self.assertRaisesRegex(MailPermanentError, "too many recipients: 201 > 200"):
            normalize_recipients([f"u{i}@example.org" for i in range(201)])


class PostboxSendTest(unittest.TestCase):
    def test_one_recipient_body_and_signature(self):
        mailer, transport, _ = make(ok())
        result = run(mailer.send(["user@example.org"], "Тема", TEXT))
        self.assertEqual((result.message_id, result.recipients), ("msg-1", 1))
        url, headers, body = transport.calls[0]
        self.assertEqual(url, "https://postbox.cloud.yandex.net/v2/email/outbound-emails")
        self.assertEqual(body["Destination"], {"ToAddresses": ["user@example.org"]})
        self.assertEqual(body["Content"]["Simple"]["Subject"], {"Data": "Тема", "Charset": "UTF-8"})
        self.assertEqual(body["Content"]["Simple"]["Body"], {"Text": {"Data": TEXT, "Charset": "UTF-8"}})
        self.assertTrue(body["FromEmailAddress"].endswith(" <no-reply@notify.example.org>"))
        self.assertTrue(body["FromEmailAddress"].startswith("=?utf-8?"))  # имя кириллицей — по RFC 2047
        self.assertIn("/ru-central1/ses/aws4_request", headers["Authorization"])
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_several_recipients_one_request(self):
        mailer, transport, _ = make(ok())
        result = run(mailer.send(["a@example.org", "B@example.org", "a@example.org"], "Тема", TEXT, html="<p>x</p>"))
        self.assertEqual(result.recipients, 2)
        self.assertEqual(len(transport.calls), 1)
        body = transport.calls[0][2]
        self.assertEqual(body["Destination"]["ToAddresses"], ["a@example.org", "b@example.org"])
        self.assertEqual(body["Content"]["Simple"]["Body"]["Html"], {"Data": "<p>x</p>", "Charset": "UTF-8"})

    def test_sender_without_name(self):
        settings = MailSettings(provider="postbox", from_address="no-reply@notify.example.org", postbox_key_id="k", postbox_secret_key="s")
        mailer, transport, _ = make(ok(), settings=settings)
        run(mailer.send(["a@example.org"], "Тема", TEXT))
        self.assertEqual(transport.calls[0][2]["FromEmailAddress"], "no-reply@notify.example.org")

    def test_invalid_recipients_no_request(self):
        mailer, transport, _ = make()
        with self.assertRaises(MailPermanentError):
            run(mailer.send([], "Тема", TEXT))
        self.assertEqual(transport.calls, [])

    def test_retry_on_429_and_503_then_success(self):
        mailer, transport, fake_time = make(
            (429, {"Code": "TooManyRequestsException", "message": "quota"}),
            (503, {"Code": "ServiceUnavailable"}),
            ok("msg-3"),
        )
        self.assertEqual(run(mailer.send(["a@example.org"], "Тема", TEXT)).message_id, "msg-3")
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(fake_time.sleeps, [1.0, 2.0])  # паузы 1 и 2 с; квота 1/с уже соблюдена паузой

    def test_retry_on_network_error(self):
        mailer, transport, _ = make(TransportError("ClientConnectionError"), ok())
        self.assertEqual(run(mailer.send(["a@example.org"], "Тема", TEXT)).message_id, "msg-1")
        self.assertEqual(len(transport.calls), 2)

    def test_temporary_error_after_last_attempt(self):
        mailer, transport, _ = make(*[(500, {"Code": "InternalError"})] * MAX_ATTEMPTS)
        with self.assertRaises(MailTemporaryError) as cm:
            run(mailer.send(["a@example.org"], "Тема", TEXT))
        self.assertEqual(len(transport.calls), MAX_ATTEMPTS)
        self.assertIn("code=InternalError", str(cm.exception))

    def test_permanent_errors_not_retried(self):
        for code in ("MessageRejected", "MailFromDomainNotVerifiedException", "BadRequestException"):
            with self.subTest(code=code):
                mailer, transport, _ = make((400, {"Code": code, "message": "failed: a@example.org"}))
                with self.assertRaises(MailPermanentError) as cm:
                    run(mailer.send(["a@example.org"], "Тема", TEXT))
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(str(cm.exception), f"postbox status=400 code={code}")

    def test_auth_error_permanent(self):
        mailer, transport, _ = make((403, {"Code": "SignatureDoesNotMatch"}))
        with self.assertRaises(MailPermanentError):
            run(mailer.send(["a@example.org"], "Тема", TEXT))
        self.assertEqual(len(transport.calls), 1)

    def test_rate_limit_between_sends(self):
        mailer, _, fake_time = make(ok(), ok())
        run(self._two_sends(mailer))
        self.assertEqual(fake_time.sleeps, [1.0])

    def test_rate_limit_from_settings(self):
        settings = MailSettings(provider="postbox", from_address="n@example.org", postbox_key_id="k", postbox_secret_key="s", max_per_second=4)
        mailer, _, fake_time = make(ok(), ok(), settings=settings)
        run(self._two_sends(mailer))
        self.assertEqual(fake_time.sleeps, [0.25])

    async def _two_sends(self, mailer):
        await mailer.send(["a@example.org"], "1", TEXT)
        await mailer.send(["a@example.org"], "2", TEXT)

    def test_no_secret_addresses_or_text_in_logs_and_errors(self):
        mailer, _, _ = make(
            (429, {"Code": "TooManyRequestsException"}),
            ok(),
        )
        with self.assertLogs("platform.mail", level=logging.INFO) as logs:
            run(mailer.send(["private.person@example.org"], "Тема", TEXT))
        mailer2, _, _ = make((400, {"Code": "MessageRejected", "message": "private.person@example.org"}))
        with self.assertRaises(MailPermanentError) as cm, self.assertLogs("platform.mail", level=logging.INFO) as logs2:
            run(mailer2.send(["private.person@example.org"], "Тема", TEXT))
        output = " ".join(logs.output + logs2.output) + str(cm.exception)
        for leak in (SECRET, "private.person", TEXT, "+70000000000"):
            self.assertNotIn(leak, output)
        self.assertNotIn(SECRET, repr(mailer.settings))

    def test_strange_error_code_not_copied(self):
        mailer, _, _ = make((400, {"Code": "a@example.org <script>"}))
        with self.assertRaises(MailPermanentError) as cm:
            run(mailer.send(["a@example.org"], "Тема", TEXT))
        self.assertEqual(str(cm.exception), "postbox status=400 code=unknown")


class BuildMailerTest(unittest.TestCase):
    def test_postbox(self):
        self.assertIsInstance(build_mailer(SETTINGS, transport=FakeTransport()), PostboxMailer)

    def test_log_sends_nothing_and_logs_no_addresses(self):
        mailer = build_mailer(MailSettings(provider="log", from_address="n@example.org"))
        self.assertIsInstance(mailer, LogMailer)
        with self.assertLogs("platform.mail", level=logging.INFO) as logs:
            result = run(mailer.send(["private.person@example.org", "b@example.org"], "Тема", TEXT))
        self.assertEqual(result.recipients, 2)
        self.assertTrue(result.message_id.startswith("log-"))
        output = " ".join(logs.output)
        self.assertIn("recipients=2", output)
        self.assertNotIn("private.person", output)
        self.assertNotIn(TEXT, output)

    def test_unknown(self):
        with self.assertRaises(ValueError):
            build_mailer(MailSettings(provider="smtp", from_address="n@example.org"))


class CliTest(unittest.TestCase):
    ENV = {
        "PLATFORM_ENV": "test", "PLATFORM_DB_HOST": "h", "PLATFORM_DB_NAME": "n", "PLATFORM_DB_USER": "u",
        "PLATFORM_DB_PASSWORD": "p", "MAIL_PROVIDER": "log", "MAIL_FROM": "no-reply@notify.example.org",
    }

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", self.ENV, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), self.assertLogs("platform.mail", level=logging.INFO):
            code = mail_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_sent_line(self):
        code, out, _ = self.cli("--to", "a@example.org", "--to", "b@example.org")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^SENT message_id=log-\S+ recipients=2\n$")

    def test_error_line_and_code(self):
        with mock.patch.object(mail_cli, "build_mailer", lambda _: _Failing()):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict("os.environ", self.ENV, clear=True), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = mail_cli.main(["--to", "a@example.org"])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "MAIL ERROR: MailPermanentError: postbox status=400 code=MessageRejected\n")


class _Failing(LogMailer):
    def __init__(self):
        pass

    async def send(self, to, subject, text, html=None):
        raise MailPermanentError("postbox status=400 code=MessageRejected")


if __name__ == "__main__":
    unittest.main()
