#!/usr/bin/env python3
"""Redirect one existing Mach-O dylib load path without resizing commands."""

from __future__ import annotations

import argparse
import pathlib
import struct


MH_MAGIC_64 = 0xFEEDFACF
DYLIB_LOAD_COMMANDS = {
    0xC,  # LC_LOAD_DYLIB
    0x18 | 0x80000000,  # LC_LOAD_WEAK_DYLIB
    0x1F | 0x80000000,  # LC_REEXPORT_DYLIB
    0x20 | 0x80000000,  # LC_LAZY_LOAD_DYLIB
    0x23 | 0x80000000,  # LC_LOAD_UPWARD_DYLIB
}


def redirect(binary: pathlib.Path, old_path: str, new_path: str) -> None:
    data = bytearray(binary.read_bytes())
    if len(data) < 32:
        raise ValueError("file is too small to be a Mach-O")

    magic, _, _, _, ncmds, sizeofcmds, _, _ = struct.unpack_from(
        "<IiiIIIII", data, 0
    )
    if magic != MH_MAGIC_64:
        raise ValueError("only a thin little-endian 64-bit Mach-O is supported")

    cursor = 32
    commands_end = cursor + sizeofcmds
    matches = 0
    for _ in range(ncmds):
        if cursor + 8 > commands_end:
            raise ValueError("truncated Mach-O load command table")
        cmd, cmdsize = struct.unpack_from("<II", data, cursor)
        if cmdsize < 8 or cursor + cmdsize > commands_end:
            raise ValueError("invalid Mach-O load command size")

        if cmd in DYLIB_LOAD_COMMANDS:
            name_offset = struct.unpack_from("<I", data, cursor + 8)[0]
            name_start = cursor + name_offset
            name_end_limit = cursor + cmdsize
            if not cursor <= name_start < name_end_limit:
                raise ValueError("invalid dylib name offset")
            nul = data.find(0, name_start, name_end_limit)
            if nul < 0:
                raise ValueError("unterminated dylib load path")
            current = data[name_start:nul].decode("utf-8")
            if current == old_path:
                encoded = new_path.encode("utf-8") + b"\0"
                capacity = name_end_limit - name_start
                if len(encoded) > capacity:
                    raise ValueError(
                        f"replacement path requires {len(encoded)} bytes; "
                        f"load command only has {capacity}"
                    )
                data[name_start:name_end_limit] = encoded.ljust(capacity, b"\0")
                matches += 1
        cursor += cmdsize

    if matches != 1:
        raise ValueError(f"expected one matching load command, found {matches}")

    binary.write_bytes(data)
    print(f"redirected {binary.name}: {old_path} -> {new_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("old_path")
    parser.add_argument("new_path")
    args = parser.parse_args()
    redirect(args.binary, args.old_path, args.new_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
