#!/usr/bin/env python3
"""Install signed arm64 hook entry bridges in CookieRunCrumble 1.1.101.

Each target entry point loads its replacement pointer from a writable
UnityFramework data slot. Original prologues are preserved in pre-signed
trampolines stored in unused padding at the end of __TEXT. The injected dylib
fills the slots during image initialization; no executable page is modified at
runtime and AppSealing worker bodies remain intact.
"""

from __future__ import annotations

import argparse
import pathlib
import struct

from patch_arm64_rvas import rva_to_file_offset


DATA_SLOT_RVA = 0x0F2FD000
TRAMPOLINE_BASE_RVA = 0x0DF44200
TARGET_STUB_SIZE = 12
TRAMPOLINE_STRIDE = 24

# Keep this order synchronized with SignedStaticHookIndex in Tweak.mm.
# expected_first rejects accidental application to another game build.
HOOKS = (
    ("LoadingFlag.Set", 0x03BDC854, "ff0301d1f65701a9f44f02a9"),
    ("AfterResponse(RpcException)", 0x03A63ED8, "ff8301d1f85f02a9f65703a9"),
    ("AfterError", 0x03A6435C, "ff4302d1f65706a9f44f07a9"),
    ("Guide.HandleOnGuideUIClick", 0x0425287C, "ffc300d1f44f01a9fd7b02a9"),
    ("OvenAutoDrawService.ctor", 0x042334F4, "ffc301d1fc6f01a9fa6702a9"),
    ("CookieGacha.OnPageOpen", 0x040B16C0, "ff4301d1f85f01a9f65702a9"),
    ("PetGacha.OnPageOpen", 0x040E66E4, "f657bda9f44f01a9fd7b02a9"),
    ("Inventory.OnPopupOpen", 0x0415BA04, "f657bda9f44f01a9fd7b02a9"),
    ("InventoryItemInfo.OnPopupOpen", 0x041520F8, "ff4301d1f85f01a9f65702a9"),
    ("Roulette.RefreshView", 0x04AC6BC4, "ff8306d1fc6f14a9fa6715a9"),
    ("SugarRune.OnPopupOpen", 0x03F32DBC, "eb2bbc6de923016df44f02a9"),
    ("StellarLink.OnPageOpen", 0x042FB688, "ff0305d1fc6f0fa9f85f10a9"),
    ("CrumbleDungeon.OnPageOpen", 0x0402DD3C, "fc6fbaa9fa6701a9f85f02a9"),
    ("DailyDungeon.OnPageOpen", 0x04047990, "f44fbea9fd7b01a9fd430091"),
    ("ArenaLobby.OnPageOpen", 0x04009C38, "f44fbea9fd7b01a9fd430091"),
    ("ArenaPreparation.OnPageOpen", 0x0401A3B0, "f44fbea9fd7b01a9fd430091"),
    ("Requirement.Register", 0x03E497D4, "ff8301d1fa6701a9f85f02a9"),
    ("Requirement.CalculateCurrentValue", 0x03E4DDB4, "ff0302d1f85f04a9f65705a9"),
    ("Guide.HandleOnProgressUpdated", 0x04253698, "fc6fbaa9fa6701a9f85f02a9"),
    ("Guide.UpdateUI", 0x042521E4, "ff8305d1fc6f10a9fa6711a9"),
)


def encode_adrp_x16(source_rva: int, target_rva: int) -> bytes:
    page_delta = (target_rva & ~0xFFF) - (source_rva & ~0xFFF)
    immediate = page_delta // 0x1000
    if not -(1 << 20) <= immediate < (1 << 20):
        raise ValueError("arm64 ADRP target is out of range")
    encoded = immediate & 0x1FFFFF
    immlo = encoded & 0x3
    immhi = encoded >> 2
    return struct.pack("<I", 0x90000010 | (immlo << 29) | (immhi << 5))


def encode_ldr_x16(slot_rva: int) -> bytes:
    page_offset = slot_rva & 0xFFF
    if page_offset % 8 or page_offset > 0x7FF8:
        raise ValueError("arm64 LDR slot offset is not encodable")
    immediate = page_offset // 8
    return struct.pack("<I", 0xF9400000 | (immediate << 10) | (16 << 5) | 16)


def encode_add_x16(target_rva: int) -> bytes:
    page_offset = target_rva & 0xFFF
    return struct.pack("<I", 0x91000000 | (page_offset << 10) | (16 << 5) | 16)


def patch(binary: pathlib.Path) -> None:
    data = bytearray(binary.read_bytes())
    slot_size = len(HOOKS) * 8
    slot_offset = rva_to_file_offset(data, DATA_SLOT_RVA, slot_size)
    if any(data[slot_offset : slot_offset + slot_size]):
        raise ValueError(
            f"static hook data slots at RVA 0x{DATA_SLOT_RVA:x} are not empty"
        )

    trampoline_size = len(HOOKS) * TRAMPOLINE_STRIDE
    trampoline_offset = rva_to_file_offset(
        data, TRAMPOLINE_BASE_RVA, trampoline_size
    )
    if any(data[trampoline_offset : trampoline_offset + trampoline_size]):
        raise ValueError(
            f"static trampoline area at RVA 0x{TRAMPOLINE_BASE_RVA:x} "
            "is not empty"
        )

    for index, (name, target, expected_hex) in enumerate(HOOKS):
        target_offset = rva_to_file_offset(data, target, TARGET_STUB_SIZE)
        expected = bytes.fromhex(expected_hex)
        original = bytes(data[target_offset : target_offset + TARGET_STUB_SIZE])
        if original != expected:
            raise ValueError(
                f"{name} RVA 0x{target:x}: expected {expected.hex()}, "
                f"found {original.hex()}"
            )

        slot = DATA_SLOT_RVA + index * 8
        target_stub = (
            encode_adrp_x16(target, slot)
            + encode_ldr_x16(slot)
            + struct.pack("<I", 0xD61F0200)  # br x16
        )
        trampoline = TRAMPOLINE_BASE_RVA + index * TRAMPOLINE_STRIDE
        resume = target + TARGET_STUB_SIZE
        trampoline_bytes = (
            original
            + encode_adrp_x16(trampoline + TARGET_STUB_SIZE, resume)
            + encode_add_x16(resume)
            + struct.pack("<I", 0xD61F0200)  # br x16
        )
        current_trampoline_offset = rva_to_file_offset(
            data, trampoline, len(trampoline_bytes)
        )
        data[
            current_trampoline_offset : current_trampoline_offset
            + len(trampoline_bytes)
        ] = trampoline_bytes
        data[target_offset : target_offset + len(target_stub)] = target_stub
        print(
            f"static hook {index:02d} {name}: target=0x{target:x} "
            f"trampoline=0x{trampoline:x} "
            f"slot=0x{slot:x}"
        )

    binary.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=pathlib.Path)
    args = parser.parse_args()
    patch(args.binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
