import base64, hashlib
from garmin_gateway import security


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
