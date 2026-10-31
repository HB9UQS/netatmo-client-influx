"""Token handling of NetatmoAPI, with the Netatmo HTTP API replaced by canned responses."""

import json
import time
from unittest import mock

import pytest

import netatmo_api as na


class FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        pass


OK = FakeResponse(200, {"body": {"devices": []}, "status": "ok"})
EXPIRED = FakeResponse(403, {"error": {"code": 3, "message": "Access token expired"}})


def token(n: int) -> FakeResponse:
    return FakeResponse(200, {"access_token": f"acc{n}", "refresh_token": f"ref{n}", "expires_in": 10800})


def endpoint(url: str) -> str:
    return url.rsplit("/", 1)[-1]


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"access_token": "acc0", "refresh_token": "ref0", "expires_at": time.time() + 3600}))
    return str(path)


def run(token_file: str, responses: list) -> tuple[na.NetatmoAPI, list]:
    calls = []
    replies = iter(responses)

    def post(url, **kwargs):
        calls.append(endpoint(url))
        return next(replies)

    with mock.patch.object(na.requests, "post", post):
        api = na.NetatmoAPI("id", "secret", None, token_file)
        api.get_stations_data()
    return api, calls


def test_valid_token_is_used_without_refresh(token_file):
    _, calls = run(token_file, [OK])
    assert calls == ["getstationsdata"]


def test_expired_token_is_refreshed_before_the_call(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"access_token": "old", "refresh_token": "ref0", "expires_at": time.time() - 5}))
    _api, calls = run(str(path), [token(1), OK])
    assert calls == ["token", "getstationsdata"]
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "acc1"
    assert saved["expires_at"] > time.time()


def test_rejected_token_is_refreshed_and_the_call_retried(token_file):
    api, calls = run(token_file, [EXPIRED, token(2), OK])
    assert calls == ["getstationsdata", "token", "getstationsdata"]
    assert api.auth.access_token == "acc2"


def test_second_rejection_raises_instead_of_looping(token_file):
    with pytest.raises(na.NetatmoAuthError):
        run(token_file, [EXPIRED, token(3), EXPIRED])
