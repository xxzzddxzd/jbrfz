"""Minimal unary gRPC client over HTTP/2 (httpx)."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, Mapping, Optional
from urllib.parse import urlparse

import httpx


@dataclass
class GrpcError(Exception):
    status: int
    message: str
    details: bytes = b""

    def __str__(self) -> str:
        return f"grpc-status={self.status} message={self.message!r}"


@dataclass
class GrpcResponse:
    message: bytes
    headers: Dict[str, str]
    trailers: Dict[str, str]


class GrpcClient:
    def __init__(
        self,
        endpoint: str,
        *,
        default_metadata: Optional[Mapping[str, str]] = None,
        timeout: float = 30.0,
    ) -> None:
        endpoint = endpoint.rstrip("/")
        parsed = urlparse(endpoint if "://" in endpoint else "https://" + endpoint)
        self.base = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            self.base += f":{parsed.port}"
        elif parsed.scheme == "https":
            self.base += ":443"
        self.default_metadata = dict(default_metadata or {})
        self.timeout = timeout
        self._client = httpx.Client(http2=True, timeout=timeout, verify=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GrpcClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @staticmethod
    def _frame(message: bytes, compressed: bool = False) -> bytes:
        return struct.pack("!BI", 1 if compressed else 0, len(message)) + message

    @staticmethod
    def _unframe(payload: bytes) -> bytes:
        if len(payload) < 5:
            return payload
        # may contain multiple frames; take first
        flag, length = struct.unpack("!BI", payload[:5])
        data = payload[5 : 5 + length]
        if flag & 1:
            raise GrpcError(13, "compressed response not supported")
        return data

    def unary(
        self,
        service_method: str,
        message: bytes,
        *,
        metadata: Optional[Mapping[str, str]] = None,
    ) -> GrpcResponse:
        path = service_method if service_method.startswith("/") else "/" + service_method
        headers = {
            "content-type": "application/grpc",
            "te": "trailers",
            "user-agent": "crumble-bot/0.1 grpc-python",
            "grpc-accept-encoding": "identity",
        }
        merged = dict(self.default_metadata)
        if metadata:
            merged.update(metadata)
        # gRPC metadata: ascii headers; -bin would need base64
        for k, v in merged.items():
            if v is None:
                continue
            headers[k.lower()] = str(v)

        url = self.base + path
        resp = self._client.post(url, content=self._frame(message), headers=headers)
        # httpx exposes trailers poorly; grpc-status may be in headers on some stacks
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        trailers = {}
        for key in ("grpc-status", "grpc-message", "crumble-error-code"):
            if key in hdrs:
                trailers[key] = hdrs[key]

        status = int(trailers.get("grpc-status", "0" if resp.status_code == 200 else "2"))
        body = self._unframe(resp.content or b"")
        if status != 0:
            raise GrpcError(status, trailers.get("grpc-message", resp.reason_phrase), body)
        if resp.status_code != 200 and status == 0:
            # some proxies
            raise GrpcError(14, f"http {resp.status_code}: {resp.text[:200]}")
        return GrpcResponse(message=body, headers=hdrs, trailers=trailers)
