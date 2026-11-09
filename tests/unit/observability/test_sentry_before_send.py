"""What an event keeps on its way to Sentry: the route, not the path; no
headers, query, extra or integration context; text scrubbed of credential
shapes and, on a failed turn, of the user's own secrets."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from src.observability import sentry


def _event(**overrides):
    event = {
        "transaction": "/api/v1/wsfiles/g/{grant}/{path:path}",
        "transaction_info": {"source": "route"},
        "request": {
            "method": "GET",
            "url": "https://api.example/api/v1/wsfiles/g/GRANT-SECRET/a.csv",
            "headers": {"Authorization": "Bearer abc"},
            "query_string": "token=abc",
        },
        "exception": {"values": [{"type": "RuntimeError", "value": "failed with sk-abcdefghijklmnop"}]},
        "breadcrumbs": {"values": [{
            "message": "GET https://vendor.example",
            "data": {"method": "GET", "url": "https://vendor.example", "http.query": "apikey=xyz", "status_code": 500},
        }]},
        "extra": {"sys.argv": ["uvicorn"], "note": "user text"},
        "contexts": {"runtime": {"name": "CPython"}, "os": {"name": "Linux"}, "trace": {}, "custom": {"t": "x"}},
    }
    event.update(overrides)
    return event


def _send(event, exc=None, vault=None):
    token = sentry._event_vault.set(vault)
    try:
        hint = {"exc_info": (type(exc), exc, None)} if exc else {}
        return sentry._before_send(event, hint)
    finally:
        sentry._event_vault.reset(token)


def test_a_request_keeps_its_method_and_route_template_only():
    out = _send(_event())
    assert out["request"] == {"method": "GET", "url": "/api/v1/wsfiles/g/{grant}/{path:path}"}
    assert "GRANT-SECRET" not in str(out)


def test_a_request_with_no_matched_route_sends_no_path():
    out = _send(_event(transaction="unknown", transaction_info={"source": "url"}))
    assert out["request"] == {"method": "GET"}


def test_credential_shapes_are_scrubbed_from_exception_text():
    out = _send(_event())
    assert "sk-abcdefghijklmnop" not in out["exception"]["values"][0]["value"]


def test_a_failed_turns_event_is_scrubbed_of_the_users_secrets():
    event = _event(exception={"values": [{"value": "token was my-own-vault-value-1234"}]})
    out = _send(event, vault={"MY_TOKEN": "my-own-vault-value-1234"})
    assert "my-own-vault-value-1234" not in out["exception"]["values"][0]["value"]


def test_an_unreadable_vault_withholds_the_text():
    out = _send(_event(), vault=sentry._WITHHELD)
    assert out["exception"]["values"][0]["value"].startswith("[withheld")


def test_a_breadcrumb_keeps_only_method_url_status_and_reason():
    crumb = _send(_event())["breadcrumbs"]["values"][0]
    assert set(crumb["data"]) == {"method", "url", "status_code"}


def test_extra_goes_and_contexts_keep_runtime_os_and_trace():
    out = _send(_event())
    assert "extra" not in out
    assert set(out["contexts"]) == {"runtime", "os", "trace"}


@pytest.mark.parametrize(
    ("exc", "dropped"),
    [
        (asyncio.CancelledError(), True),
        (HTTPException(status_code=404), True),
        (HTTPException(status_code=429), True),
        (HTTPException(status_code=500), False),
        (KeyError("x"), False),
    ],
)
def test_refusals_and_cancellations_are_dropped_but_a_rewritten_5xx_is_kept(exc, dropped):
    assert (_send(_event(), exc=exc) is None) is dropped


def test_a_finalize_failure_goes_without_its_text():
    out = _send(_event(), vault=sentry._NO_TEXT)
    assert out["exception"]["values"][0]["value"] == "[withheld: it can quote the user's data]"
