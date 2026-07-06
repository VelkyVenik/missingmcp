import base64, hashlib
from missingmcp import security


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def test_pkce_s256_ok():
    v = "verifier-123"
    assert security.verify_pkce(v, _challenge(v), "S256")


def test_pkce_wrong_verifier_fails():
    assert not security.verify_pkce("nope", _challenge("verifier-123"), "S256")


def test_pkce_plain_rejected():
    assert not security.verify_pkce("v", "v", "plain")


def test_csp_has_no_form_action():
    # An OAuth authorization server must 302 the login/MFA form POST to the
    # client's registered redirect_uri (a different origin). `form-action 'self'`
    # makes browsers block that cross-origin redirect, breaking the auth-code
    # callback. Redirect safety is enforced by validate_redirect_uri() instead.
    csp = security.security_headers()["Content-Security-Policy"]
    assert "form-action" not in csp
    assert "default-src 'self'" in csp


def test_redirect_uri_allowlist():
    allowed = ["https://claude.ai/cb"]
    assert security.validate_redirect_uri("https://claude.ai/cb", allowed)
    assert not security.validate_redirect_uri("https://evil.com/cb", allowed)


def test_session_id_validation():
    assert security.validate_session_id("abc-123_.A")
    assert not security.validate_session_id("bad id space")
    assert not security.validate_session_id("")


def test_rate_limiter_blocks_over_limit():
    clock = [0.0]
    rl = security.RateLimiter(clock=lambda: clock[0])
    assert rl.check("ip", limit=2, window=60)
    assert rl.check("ip", limit=2, window=60)
    assert not rl.check("ip", limit=2, window=60)
    clock[0] = 61
    assert rl.check("ip", limit=2, window=60)  # window slid


def test_rate_limiter_gc_drops_idle_keys():
    clock = [0.0]
    rl = security.RateLimiter(clock=lambda: clock[0])
    rl.check("a", limit=5, window=60)
    rl.check("b", limit=5, window=60)
    assert len(rl._hits) == 2
    clock[0] = 100.0
    rl.check("b", limit=5, window=60)   # refresh b
    rl.gc(max_idle=50)                  # a idle 100s > 50, b just used
    assert "a" not in rl._hits and "b" in rl._hits


def test_csrf_one_time():
    clock = [0.0]
    cs = security.CsrfStore(ttl=600, clock=lambda: clock[0])
    tok = cs.issue()
    assert cs.consume(tok)
    assert not cs.consume(tok)         # one-time
    assert not cs.consume("forged")


def test_csrf_expires():
    clock = [0.0]
    cs = security.CsrfStore(ttl=10, clock=lambda: clock[0])
    tok = cs.issue()
    clock[0] = 11
    assert not cs.consume(tok)


class _FakeStreamRequest:
    def __init__(self, chunks):
        self._chunks = chunks

    async def stream(self):
        for c in self._chunks:
            yield c


async def test_read_body_limited_under_limit_returns_body():
    req = _FakeStreamRequest([b"hello", b"world"])
    assert await security.read_body_limited(req, max_bytes=100) == b"helloworld"


async def test_read_body_limited_over_limit_returns_none():
    req = _FakeStreamRequest([b"a" * 60, b"b" * 60])  # 120 bytes > 100
    assert await security.read_body_limited(req, max_bytes=100) is None
