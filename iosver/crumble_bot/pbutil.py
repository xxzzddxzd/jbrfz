"""Minimal protobuf wire helpers (encode + light decode)."""
from __future__ import annotations

import struct
from typing import Iterable, List, Tuple


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        out.append(bits | (0x80 if value else 0))
        if not value:
            break
    return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def encode_varint_field(field: int, value: int) -> bytes:
    if value == 0:
        return b""
    return tag(field, 0) + _varint(value)


def encode_sint32_field(field: int, value: int) -> bytes:
    # zigzag
    zz = (value << 1) ^ (value >> 31)
    return encode_varint_field(field, zz & 0xFFFFFFFF)


def encode_int32_field(field: int, value: int) -> bytes:
    return encode_varint_field(field, value & 0xFFFFFFFF)


def encode_int64_field(field: int, value: int) -> bytes:
    return encode_varint_field(field, value)


def encode_bool_field(field: int, value: bool) -> bytes:
    return encode_varint_field(field, 1 if value else 0)


def encode_bytes_field(field: int, data: bytes) -> bytes:
    if not data:
        return b""
    return tag(field, 2) + _varint(len(data)) + data


def encode_string_field(field: int, value: str) -> bytes:
    if not value:
        return b""
    raw = value.encode("utf-8")
    return tag(field, 2) + _varint(len(raw)) + raw


def encode_double_field(field: int, value: float) -> bytes:
    if value == 0.0:
        return b""
    return tag(field, 1) + struct.pack("<d", float(value))


def encode_message_field(field: int, message: bytes) -> bytes:
    return encode_bytes_field(field, message)


def encode_repeated_messages(field: int, messages: Iterable[bytes]) -> bytes:
    return b"".join(encode_message_field(field, m) for m in messages)


def encode_packed_int32_field(field: int, values: Iterable[int]) -> bytes:
    """Encode a repeated ``int32`` field using protobuf packed encoding."""
    payload = b"".join(_varint(int(value) & 0xFFFFFFFF) for value in values)
    return encode_bytes_field(field, payload)


def decode_packed_varints(data: bytes) -> List[int]:
    """Decode the payload of a packed protobuf varint field."""
    values: List[int] = []
    index = 0
    while index < len(data):
        value = 0
        shift = 0
        while True:
            if index >= len(data):
                raise ValueError("truncated packed varint")
            byte = data[index]
            index += 1
            value |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                break
            shift += 7
            if shift >= 70:
                raise ValueError("packed varint is too long")
        values.append(value)
    return values


def decode_fields(buf: bytes) -> List[Tuple[int, int, object]]:
    """Return list of (field_number, wire_type, value)."""
    i = 0
    n = len(buf)
    out: List[Tuple[int, int, object]] = []

    def read_varint() -> int:
        nonlocal i
        val = 0
        shift = 0
        while True:
            if i >= n:
                raise ValueError("truncated varint")
            b = buf[i]
            i += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                return val
            shift += 7

    while i < n:
        key = read_varint()
        fn, wt = key >> 3, key & 7
        if wt == 0:
            out.append((fn, wt, read_varint()))
        elif wt == 2:
            ln = read_varint()
            data = buf[i : i + ln]
            i += ln
            out.append((fn, wt, data))
        elif wt == 1:
            data = buf[i : i + 8]
            i += 8
            out.append((fn, wt, data))
        elif wt == 5:
            data = buf[i : i + 4]
            i += 4
            out.append((fn, wt, data))
        else:
            raise ValueError(f"unsupported wire type {wt}")
    return out
