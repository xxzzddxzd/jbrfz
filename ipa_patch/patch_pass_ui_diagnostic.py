#!/usr/bin/env python3
"""Apply or restore the 1.0.101 season-pass UI diagnostic patch.

The patch only changes the reward-list rendering flag.  Premium reward clicks
are disabled at the presenter so this diagnostic build cannot submit a special
reward claim for an account without a server-side entitlement.
"""

from __future__ import annotations

import argparse
import pathlib

from patch_arm64_rvas import rva_to_file_offset


PATCHES = (
    (
        0x0421B4B0,
        bytes.fromhex("04c14039"),  # ldrb w4, [x8, #0x30]
        bytes.fromhex("24008052"),  # mov w4, #1
        "render premium reward cells as available",
    ),
    (
        0x0421E11C,
        bytes.fromhex("f657bda9"),  # HandleSpecialRewardClicked prologue
        bytes.fromhex("c0035fd6"),  # ret
        "block premium reward click/RPC",
    ),
)


def patch(binary: pathlib.Path, restore: bool) -> None:
    data = bytearray(binary.read_bytes())
    changed = False

    for rva, original, diagnostic, description in PATCHES:
        expected, replacement = (
            (diagnostic, original) if restore else (original, diagnostic)
        )
        offset = rva_to_file_offset(data, rva)
        current = bytes(data[offset : offset + len(expected)])
        if current == replacement:
            print(f"unchanged {binary.name} RVA 0x{rva:x}: {description}")
            continue
        if current != expected:
            raise ValueError(
                f"unexpected bytes at RVA 0x{rva:x}: "
                f"expected {expected.hex()}, got {current.hex()}"
            )
        data[offset : offset + len(replacement)] = replacement
        changed = True
        print(
            f"patched {binary.name} RVA 0x{rva:x}: "
            f"{expected.hex()} -> {replacement.hex()} ({description})"
        )

    if changed:
        binary.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument(
        "--restore",
        action="store_true",
        help="restore the two original 1.0.101 instructions",
    )
    args = parser.parse_args()
    patch(args.binary, args.restore)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
