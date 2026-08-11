#!/usr/bin/env python3
"""Inject one LC_LOAD_DYLIB command into a thin arm64 Mach-O executable."""

from __future__ import annotations

import argparse
import pathlib
import struct


MH_MAGIC_64 = 0xFEEDFACF
LC_SEGMENT_64 = 0x19
LC_LOAD_DYLIB = 0xC


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def read_c_string(data: bytearray, start: int, end: int) -> str:
    zero = data.find(b"\0", start, end)
    if zero < 0:
        zero = end
    return data[start:zero].decode("utf-8", errors="replace")


def inject(binary: pathlib.Path, install_name: str) -> bool:
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
    first_section_offset = len(data)
    for _ in range(ncmds):
        if cursor + 8 > commands_end:
            raise ValueError("truncated Mach-O load command table")
        cmd, cmdsize = struct.unpack_from("<II", data, cursor)
        if cmdsize < 8 or cursor + cmdsize > commands_end:
            raise ValueError("invalid Mach-O load command size")

        if cmd == LC_LOAD_DYLIB:
            name_offset = struct.unpack_from("<I", data, cursor + 8)[0]
            existing = read_c_string(data, cursor + name_offset, cursor + cmdsize)
            if existing == install_name:
                return False

        if cmd == LC_SEGMENT_64:
            nsects = struct.unpack_from("<I", data, cursor + 64)[0]
            section = cursor + 72
            for _ in range(nsects):
                if section + 80 > cursor + cmdsize:
                    raise ValueError("truncated Mach-O section table")
                section_offset = struct.unpack_from("<I", data, section + 48)[0]
                if section_offset:
                    first_section_offset = min(first_section_offset, section_offset)
                section += 80
        cursor += cmdsize

    name = install_name.encode("utf-8") + b"\0"
    cmdsize = align(24 + len(name), 8)
    new_commands_end = commands_end + cmdsize
    if new_commands_end > first_section_offset:
        raise ValueError(
            f"not enough Mach-O header padding: need {cmdsize} bytes, "
            f"have {first_section_offset - commands_end}"
        )
    if any(data[commands_end:new_commands_end]):
        raise ValueError("Mach-O header padding is not empty")

    command = struct.pack("<IIIIII", LC_LOAD_DYLIB, cmdsize, 24, 0, 0, 0)
    command += name
    command += b"\0" * (cmdsize - len(command))
    data[commands_end:new_commands_end] = command
    struct.pack_into("<II", data, 16, ncmds + 1, sizeofcmds + cmdsize)
    binary.write_bytes(data)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("install_name")
    args = parser.parse_args()
    changed = inject(args.binary, args.install_name)
    print("injected" if changed else "already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
