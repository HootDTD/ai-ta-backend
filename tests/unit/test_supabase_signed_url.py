"""Signed-URL minting on the Supabase Storage client.

The sign endpoint returns a path fragment; the client must root it at
/storage/v1 so the result is directly usable in a browser tab (where no
Authorization header can ride along — expiry is Supabase-enforced).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import vendors.supabase_storage as storage_mod
from vendors.supabase_storage import SupabaseStorageClient

pytestmark = pytest.mark.unit


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    return SupabaseStorageClient()


def _response(payload):
    return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)


def test_create_signed_url_roots_fragment_and_posts_expiry(client, monkeypatch):
    calls = []

    def _post(url, *, headers, json, timeout):
        calls.append({"url": url, "json": json})
        return _response({"signedURL": "/object/sign/uploads/c7/w1.pdf?token=abc"})

    monkeypatch.setattr(storage_mod.requests, "post", _post)

    url = client.create_signed_url(bucket="uploads", object_key="c7/w1.pdf", expires_in=300)

    assert url == "http://localhost:54321/storage/v1/object/sign/uploads/c7/w1.pdf?token=abc"
    assert calls == [
        {
            "url": "http://localhost:54321/storage/v1/object/sign/uploads/c7/w1.pdf",
            "json": {"expiresIn": 300},
        }
    ]


def test_create_signed_url_handles_fragment_without_leading_slash(client, monkeypatch):
    monkeypatch.setattr(
        storage_mod.requests,
        "post",
        lambda *a, **k: _response({"signedURL": "object/sign/uploads/k.pdf?token=t"}),
    )
    url = client.create_signed_url(bucket="uploads", object_key="k.pdf")
    assert url == "http://localhost:54321/storage/v1/object/sign/uploads/k.pdf?token=t"


def test_create_signed_url_raises_on_missing_fragment(client, monkeypatch):
    monkeypatch.setattr(storage_mod.requests, "post", lambda *a, **k: _response({}))
    with pytest.raises(RuntimeError, match="no signedURL"):
        client.create_signed_url(bucket="uploads", object_key="k.pdf")


def test_create_signed_url_validates_inputs(client):
    with pytest.raises(ValueError, match="bucket"):
        client.create_signed_url(bucket=" ", object_key="k.pdf")
    with pytest.raises(ValueError, match="object_key"):
        client.create_signed_url(bucket="uploads", object_key="")
