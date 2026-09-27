"""Email alerts end-to-end against a real local SMTP server, plus the port-465 (SSL) path."""

import smtplib
import socketserver
import threading

import pytest

from trader.alerts import AlertError, send_alert, send_email, smtp_connect
from trader.config import AlertsConfig, AppConfig, Secrets


class _SMTPHandler(socketserver.StreamRequestHandler):
    """Just enough SMTP (RFC 5321) to accept one message: EHLO/MAIL/RCPT/DATA/QUIT."""

    def handle(self):
        srv = self.server
        self.wfile.write(b"220 test ESMTP\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode().strip()
            verb = cmd.split(" ")[0].upper()
            if verb in ("EHLO", "HELO"):
                self.wfile.write(b"250-test\r\n250 OK\r\n")
            elif verb == "MAIL":
                srv.mail_from = cmd
                self.wfile.write(b"250 OK\r\n")
            elif verb == "RCPT":
                srv.rcpt.append(cmd)
                self.wfile.write(b"250 OK\r\n")
            elif verb == "DATA":
                self.wfile.write(b"354 go\r\n")
                body = []
                while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                    body.append(chunk.decode())
                srv.messages.append("".join(body))
                self.wfile.write(b"250 queued\r\n")
            elif verb == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"502 not implemented\r\n")


@pytest.fixture
def smtp_server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    srv.daemon_threads = True
    srv.messages, srv.rcpt, srv.mail_from = [], [], None
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _secrets(port, **kw):
    base = dict(smtp_host="127.0.0.1", smtp_port=port, smtp_starttls=False,
                alert_email_from="bot@example.com", alert_email_to="me@example.com")
    base.update(kw)
    return Secrets(**base)


def test_send_email_delivers_message(smtp_server):
    port = smtp_server.server_address[1]
    send_email(_secrets(port), "[trader] circuit breaker", "New entries blocked.")
    assert len(smtp_server.messages) == 1
    msg = smtp_server.messages[0]
    assert "Subject: [trader] circuit breaker" in msg
    assert "From: bot@example.com" in msg and "To: me@example.com" in msg
    assert "New entries blocked." in msg
    assert "me@example.com" in smtp_server.rcpt[0]


def test_send_alert_respects_channel(smtp_server):
    port = smtp_server.server_address[1]
    cfg = AppConfig(alerts=AlertsConfig(channel="email"))
    assert send_alert(cfg, _secrets(port), "s", "b") is True
    off = AppConfig(alerts=AlertsConfig(channel="none"))
    assert send_alert(off, _secrets(port), "s", "b") is False
    assert len(smtp_server.messages) == 1


def test_unconfigured_email_raises_clearly():
    with pytest.raises(AlertError, match="not configured"):
        send_email(Secrets(), "s", "b")


def test_port_465_uses_implicit_tls(monkeypatch):
    used = {}

    class FakeSSL:
        def __init__(self, host, port, timeout):
            used["ssl"] = (host, port)

        def ehlo(self):
            pass

        def starttls(self):  # must NOT be called on 465
            used["starttls"] = True

        def login(self, u, p):
            used["login"] = (u, p)

        def close(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    smtp_connect(_secrets(465, smtp_starttls=True, smtp_user="u", smtp_password="pw"))
    assert used == {"ssl": ("127.0.0.1", 465), "login": ("u", "pw")}


def test_connection_failure_is_an_exception_not_a_hang():
    with pytest.raises(OSError):
        smtp_connect(_secrets(1, smtp_host="127.0.0.1"), timeout=2)  # nothing listens on port 1
