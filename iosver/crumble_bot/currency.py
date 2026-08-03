"""Currency ids and lightweight protobuf response parsers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import pbutil as pb

DIAMOND_CURRENCY_DATA_ID = 1464007916


@dataclass(frozen=True)
class CurrencyPayment:
    data_id: int
    amount: int


def parse_signup_currency_balance(
    body: bytes,
    data_id: int = DIAMOND_CURRENCY_DATA_ID,
) -> Optional[int]:
    """Read one currency from SignUpResponse.crumble.inventory.currencies."""
    crumble = _message_field(body, 3)
    inventory = _message_field(crumble, 3) if crumble is not None else None
    if inventory is None:
        return None

    for field_number, wire_type, value in pb.decode_fields(inventory):
        if field_number != 1 or wire_type != 2:
            continue
        fields = pb.decode_fields(bytes(value))
        currency_id = _varint_value(fields, 1)
        if currency_id == data_id:
            return _varint_value(fields, 2, default=0)
    return None


def parse_currency_payments(body: bytes) -> list[CurrencyPayment]:
    """Read currency payments from a game mutation response."""
    payments: list[CurrencyPayment] = []
    for field_number, wire_type, value in pb.decode_fields(body):
        if field_number != 2 or wire_type != 2:
            continue
        payment = _message_field(bytes(value), 1)
        if payment is None:
            continue
        fields = pb.decode_fields(payment)
        data_id = _varint_value(fields, 1)
        amount = _varint_value(fields, 2, default=0)
        if data_id is not None:
            payments.append(CurrencyPayment(data_id=data_id, amount=amount or 0))
    return payments


def _message_field(body: bytes, target: int) -> Optional[bytes]:
    for field_number, wire_type, value in pb.decode_fields(body):
        if field_number == target and wire_type == 2:
            return bytes(value)
    return None


def _varint_value(fields, target: int, default: Optional[int] = None) -> Optional[int]:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 0:
            return int(value)
    return default
