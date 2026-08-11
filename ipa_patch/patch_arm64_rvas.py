#!/usr/bin/env python3
"""Replace selected arm64 Mach-O RVAs with a single RET instruction."""

from __future__ import annotations

import argparse
import pathlib
import struct


MH_MAGIC_64 = 0xFEEDFACF
LC_SEGMENT_64 = 0x19
ARM64_RET = b"\xc0\x03\x5f\xd6"


def rva_to_file_offset(data: bytearray, rva: int) -> int:
    if len(data) < 32:
        raise ValueError("file is too small to be a Mach-O")

    magic, _, _, _, ncmds, sizeofcmds, _, _ = struct.unpack_from(
        "<IiiIIIII", data, 0
    )
    if magic != MH_MAGIC_64:
        raise ValueError("only a thin little-endian 64-bit Mach-O is supported")

    cursor = 32
    commands_end = cursor + sizeofcmds
    segments: list[tuple[int, int, int, int]] = []
    for _ in range(ncmds):
        if cursor + 8 > commands_end:
            raise ValueError("truncated Mach-O load command table")
        cmd, cmdsize = struct.unpack_from("<II", data, cursor)
        if cmdsize < 8 or cursor + cmdsize > commands_end:
            raise ValueError("invalid Mach-O load command size")

        if cmd == LC_SEGMENT_64:
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from(
                "<QQQQ", data, cursor + 24
            )
            segments.append((vmaddr, vmsize, fileoff, filesize))
        cursor += cmdsize

    image_base = min(vmaddr for vmaddr, _, _, filesize in segments if filesize)
    virtual_address = image_base + rva
    for vmaddr, vmsize, fileoff, filesize in segments:
        if vmaddr <= virtual_address < vmaddr + vmsize:
            relative = virtual_address - vmaddr
            if relative + len(ARM64_RET) > filesize:
                raise ValueError(f"RVA 0x{rva:x} is not file-backed")
            return fileoff + relative

    raise ValueError(f"RVA 0x{rva:x} does not belong to any segment")


def patch(binary: pathlib.Path, rvas: list[int]) -> None:
    data = bytearray(binary.read_bytes())
    for rva in rvas:
        offset = rva_to_file_offset(data, rva)
        original = bytes(data[offset : offset + len(ARM64_RET)])
        data[offset : offset + len(ARM64_RET)] = ARM64_RET
        print(f"patched {binary.name} RVA 0x{rva:x}: {original.hex()} -> ret")
    binary.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("rvas", nargs="+", type=lambda value: int(value, 0))
    args = parser.parse_args()
    patch(args.binary, args.rvas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
