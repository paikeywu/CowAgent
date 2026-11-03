# encoding:utf-8
"""A refused login must be a 401, and the body must still say why.

``/auth/login`` used to answer a wrong password with ``HTTP 200`` and a
body that said ``{"status": "error", "message": "Wrong password"}``.  A
monitor that reads only the status code read that as a successful sign-in.
The status code is what the machine reads and the body is what the person
reads, so a refused credential has to be right in both.

The body is deliberately unchanged: the console's login form (channel/web/
static/js/core/auth.js) decides by ``data.status``, and the desktop
client's ``LoginGate.tsx`` shows the same message whether the request
failed or the response was an error.  So the reason still reaches the
user, and only the status code changes.

The password never appears in this file.  The wrong-password arm uses a
fake value; the correct-password arm reads the real one from the
environment (``COW_TEST_WEB_PASSWORD``) and skips when it is absent.
"""

import json
import os

import pytest
import web

import config
from channel.web.api.auth import AuthLoginHandler
from channel.web.api.pages import HealthHandler


#: A value that cannot be the configured password, and says so.
WRONG_PASSWORD = "definitely-wrong-test-password-zzz"

#: Where the correct password comes from.  Never a literal.
PASSWORD_ENV = "COW_TEST_WEB_PASSWORD"


@pytest.fixture
def console(tmp_path, monkeypatch):
    """A console whose password is enabled, on a throwaway config file."""
    original = config.config
    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path))
    path = tmp_path / "config.json"
    seed = {
        "model": "gpt-4o-mini",
        "enable_thinking": False,
        "agent_max_steps": 20,
        "web_password": "",
        "subagent": {"enabled": False, "max_concurrent": 7},
    }
    path.write_text(json.dumps(seed), encoding="utf-8")
    config.load_config()
    app = web.application(
        ("/auth/login", "AuthLoginHandler", "/api/health", "HealthHandler"),
        {"AuthLoginHandler": AuthLoginHandler, "HealthHandler": HealthHandler},
        autoreload=False,
    )

    def post_login(body, headers=None):
        return app.request(
            "/auth/login", method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
            data=body,
        )

    try:
        yield app, post_login, path, seed
    finally:
        config.config = original


def _enable_password(path, seed, password):
    path.write_text(
        json.dumps({**seed, "web_password": password}), encoding="utf-8")
    config.load_config()


# --- 2.1.1  a wrong password is a 401, and the body still says why ---------


def test_a_wrong_password_is_a_401_and_the_body_still_says_why(console):
    """2.1.1  the status code says "rejected"; the body says "wrong password"."""
    _, post_login, path, seed = console
    _enable_password(path, seed, "the-real-password-for-this-test")

    response = post_login(json.dumps({"password": WRONG_PASSWORD}))

    assert response.status.startswith("401")
    body = json.loads(response.data)
    assert body["status"] == "error"
    assert body["message"] == "Wrong password"


def test_the_refusal_carries_no_token_and_no_cookie(console):
    """A refused login must not hand out a session."""
    _, post_login, path, seed = console
    _enable_password(path, seed, "the-real-password-for-this-test")

    response = post_login(json.dumps({"password": WRONG_PASSWORD}))

    body = json.loads(response.data)
    assert "token" not in body
    assert "Set-Cookie" not in response.headers


# --- 2.1.2  an empty or missing password is a 401 too ----------------------


@pytest.mark.parametrize("body", [
    "{}",
    '{"password": ""}',
    '{"password": null}',
])
def test_a_missing_or_empty_password_is_a_401(console, body):
    """2.1.2  no credential offered is a refused credential."""
    _, post_login, path, seed = console
    _enable_password(path, seed, "the-real-password-for-this-test")

    response = post_login(body)

    assert response.status.startswith("401")
    assert json.loads(response.data)["status"] == "error"


def test_a_body_that_is_not_json_is_a_401(console):
    """A malformed body carries no credential either."""
    _, post_login, path, seed = console
    _enable_password(path, seed, "the-real-password-for-this-test")

    response = post_login("this is not json")

    assert response.status.startswith("401")
    assert json.loads(response.data)["message"] == "Invalid request"


# --- 2.1.3  the correct password still signs in ---------------------------


def test_the_correct_password_still_signs_in(console):
    """2.1.3  the good path must not be broken by the refusal."""
    _, post_login, path, seed = console
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        pytest.skip("%s is not set; the real password never enters this file"
                    % PASSWORD_ENV)
    _enable_password(path, seed, password)

    response = post_login(json.dumps({"password": password}))

    assert response.status.startswith("200")
    body = json.loads(response.data)
    assert body["status"] == "success"
    assert body.get("token")


def test_a_correct_password_from_the_environment_yields_a_usable_token(console):
    """2.1.5  the token a correct login returns is accepted afterwards."""
    _, post_login, path, seed = console
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        pytest.skip("%s is not set; the real password never enters this file"
                    % PASSWORD_ENV)
    _enable_password(path, seed, password)

    response = post_login(json.dumps({"password": password}))
    token = json.loads(response.data)["token"]
    assert "." in token and token.split(".", 1)[0]


# --- 2.1.4  the liveness probe stays open ---------------------------------


def test_the_liveness_probe_stays_open(console):
    """2.1.4  /api/health is unauthenticated BY DESIGN; do not gate it."""
    app, _, path, seed = console
    _enable_password(path, seed, "the-real-password-for-this-test")

    response = app.request("/api/health")

    assert response.status.startswith("200")
    assert json.loads(response.data)["status"] == "ok"
