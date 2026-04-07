from __future__ import annotations

"""Minimal Supabase Storage client for server-side teacher upload assets."""

import os
from urllib.parse import quote

import requests


class SupabaseStorageClient:
    """Upload and download binary objects via the Supabase Storage REST API."""

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
        self._base_url = (base_url or os.getenv("SUPABASE_URL") or "").rstrip("/")
        self._api_key = (
            api_key
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_API_KEY")
            or os.getenv("SUPABASE_ANON_KEY")
            or ""
        ).strip()
        if not self._base_url:
            raise RuntimeError("SUPABASE_URL is required for Supabase Storage access.")
        if not self._api_key:
            raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_API_KEY) is required for Supabase Storage access.")

    def upload_bytes(
        self,
        *,
        bucket: str,
        object_key: str,
        data: bytes,
        content_type: str,
        upsert: bool = False,
        timeout: int = 120,
    ) -> None:
        resp = requests.post(
            self._object_url(bucket=bucket, object_key=object_key),
            headers={
                **self._headers(),
                "Content-Type": content_type,
                "x-upsert": "true" if upsert else "false",
            },
            data=data,
            timeout=timeout,
        )
        resp.raise_for_status()

    def download_bytes(
        self,
        *,
        bucket: str,
        object_key: str,
        timeout: int = 120,
    ) -> bytes:
        resp = requests.get(
            self._object_url(bucket=bucket, object_key=object_key),
            headers=self._headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.content

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._api_key,
            "Authorization": f"Bearer {self._api_key}",
        }

    def _object_url(self, *, bucket: str, object_key: str) -> str:
        bucket_norm = (bucket or "").strip()
        if not bucket_norm:
            raise ValueError("bucket is required")
        key_norm = (object_key or "").lstrip("/")
        if not key_norm:
            raise ValueError("object_key is required")
        return f"{self._base_url}/storage/v1/object/{bucket_norm}/{quote(key_norm, safe='/')}"


__all__ = ["SupabaseStorageClient"]
