#!/usr/bin/env python3
"""Replace selected arm64 Mach-O function RVAs with a tiny return stub."""

from __future__ import annotations

import argparse
import pathlib
import struct


MH_MAGIC_64 = 0xFEEDFACF
LC_SEGMENT_64 = 0x19
ARM64_RET = b"\xc0\x03\x5f\xd6"
ARM64_MOV_W0_FALSE = b"\x00\x00\x80\x52"
ARM64_MOV_W0_TRUE = b"\x20\x00\x80\x52"


def arm64_branch(source_rva: int, target_rva: int) -> bytes:
    delta = target_rva - source_rva
    if delta % 4:
        raise ValueError("arm64 branch target must be 4-byte aligned")
    immediate = delta // 4
    if not -(1 << 25) <= immediate < (1 << 25):
        raise ValueError("arm64 branch target is outside the signed 26-bit range")
    return struct.pack("<I", 0x14000000 | (immediate & 0x03FFFFFF))


def rva_to_file_offset(data: bytearray, rva: int, patch_size: int = 4) -> int:
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
            if relative + patch_size > filesize:
                raise ValueError(f"RVA 0x{rva:x} is not file-backed")
            return fileoff + relative

    raise ValueError(f"RVA 0x{rva:x} does not belong to any segment")


def patch(
    binary: pathlib.Path,
    rvas: list[int],
    replacement: bytes | None,
    branch_target: int | None = None,
) -> None:
    data = bytearray(binary.read_bytes())
    for rva in rvas:
        current_replacement = (
            arm64_branch(rva, branch_target)
            if branch_target is not None
            else replacement
        )
        if current_replacement is None:
            raise ValueError("no arm64 replacement was selected")
        offset = rva_to_file_offset(data, rva, len(current_replacement))
        original = bytes(data[offset : offset + len(current_replacement)])
        data[offset : offset + len(current_replacement)] = current_replacement
        print(
            f"patched {binary.name} RVA 0x{rva:x}: "
            f"{original.hex()} -> {current_replacement.hex()}"
        )
    binary.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--return-true",
        action="store_true",
        help="replace each function with `mov w0, #1; ret` instead of `ret`",
    )
    parser.add_argument(
        "--return-false",
        action="store_true",
        help="replace each function with `mov w0, #0; ret` instead of `ret`",
    )
    parser.add_argument(
        "--branch-to",
        type=lambda value: int(value, 0),
        help="replace each RVA with an unconditional branch to this RVA",
    )
    parser.add_argument("binary", type=pathlib.Path)
    parser.add_argument("rvas", nargs="+", type=lambda value: int(value, 0))
    args = parser.parse_args()
    selected_modes = sum(
        (
            bool(args.return_true),
            bool(args.return_false),
            args.branch_to is not None,
        )
    )
    if selected_modes > 1:
        parser.error("return and branch modes are mutually exclusive")
    if args.branch_to is not None:
        replacement = None
    elif args.return_true:
        replacement = ARM64_MOV_W0_TRUE + ARM64_RET
    elif args.return_false:
        replacement = ARM64_MOV_W0_FALSE + ARM64_RET
    else:
        replacement = ARM64_RET
    patch(args.binary, args.rvas, replacement, args.branch_to)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
