"""Debug report: ELF/DWARF parsing on a synthetic binary, dump decoding/redaction, ADB collection."""
from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.aciq_thermostat import debug_report as dbg
from custom_components.aciq_thermostat.deployer import Deployer

from .fake_adb import FakeThermostat


@pytest.fixture
def thermo():
    t = FakeThermostat()
    with (
        patch("custom_components.aciq_thermostat.deployer.AdbDeviceTcpAsync", t.factory),
        patch("custom_components.aciq_thermostat.deployer.asyncio.sleep", AsyncMock()),
    ):
        yield t


def _uleb(v: int) -> bytes:
    out = bytearray()
    while True:
        b, v = v & 0x7F, v >> 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _dwarf() -> tuple[bytes, bytes, bytes]:
    """DWARF 4: struct dev_state_t { uint8_t addr; uint8_t sn[4]; struct { uint8_t TsMinSet; uint16_t f; } s; }
    typedef'd, and `dev_state_t g_self_state;`."""
    strs = b"\0dev_state_t\0"                       # strp offset 1
    # abbrev codes: 1 CU, 2 base, 3 struct(strp name), 4 member(loc data1), 5 array, 6 subrange,
    # 7 typedef, 8 variable, 9 anonymous struct, 10 member with exprloc location, 11 const
    A = bytearray()
    def ab(code: int, tag: int, children: bool, specs: list[tuple[int, int]]) -> None:
        A.extend(_uleb(code) + _uleb(tag) + bytes([children]))
        for at, form in specs:
            A.extend(_uleb(at) + _uleb(form))
        A.extend(b"\0\0")
    ab(1, 0x11, True, [(0x03, 0x08)])
    ab(2, 0x24, False, [(0x03, 0x08), (0x0B, 0x0B)])
    ab(3, 0x13, True, [(0x03, 0x0E), (0x0B, 0x0B)])
    ab(4, 0x0D, False, [(0x03, 0x08), (0x49, 0x13), (0x38, 0x0B)])
    ab(5, 0x01, True, [(0x49, 0x13)])
    ab(6, 0x21, False, [(0x2F, 0x0B)])
    ab(7, 0x16, False, [(0x03, 0x08), (0x49, 0x13)])
    ab(8, 0x34, False, [(0x03, 0x08), (0x49, 0x13), (0x02, 0x18), (0x3F, 0x19)])
    ab(9, 0x13, True, [(0x0B, 0x0B)])
    ab(10, 0x0D, False, [(0x03, 0x08), (0x49, 0x13), (0x38, 0x18)])
    ab(11, 0x26, False, [(0x49, 0x13)])
    A.append(0)
    body = bytearray()
    off: dict[str, int] = {}
    fix: list[tuple[int, str]] = []
    HDR = 11
    def die(name: str | None, code: int, *parts: bytes | tuple[str]) -> None:
        if name:
            off[name] = HDR + len(body)
        body.extend(_uleb(code))
        for p in parts:
            if isinstance(p, tuple):
                fix.append((len(body), p[0]))
                body.extend(b"\0\0\0\0")
            else:
                body.extend(p)
    die(None, 1, b"t.c\0")
    die("u8", 2, b"uint8_t\0", b"\x01")
    die("u16", 2, b"uint16_t\0", b"\x02")
    die("cu8", 11, ("u8",))
    die("arr", 5, ("u8",)); die(None, 6, b"\x03"); body.append(0)
    die("inner", 9, b"\x03")
    die(None, 4, b"TsMinSet\0", ("cu8",), b"\x00")
    die(None, 10, b"f\0", ("u16",), b"\x02\x23\x01")      # DW_OP_plus_uconst 1
    body.append(0)
    die("st", 3, struct.pack("<I", 1), b"\x08")
    die(None, 4, b"addr\0", ("u8",), b"\x00")
    die(None, 4, b"sn\0", ("arr",), b"\x01")
    die(None, 4, b"s\0", ("inner",), b"\x05")
    body.append(0)
    die("td", 7, b"dev_state_t\0", ("st",))
    die(None, 8, b"g_self_state\0", ("td",), b"\x05\x03\x00\x10\x00\x00")
    body.append(0)                                  # end of CU children
    for pos, name in fix:
        body[pos:pos + 4] = struct.pack("<I", off[name])
    unit = struct.pack("<HIB", 4, 0, 4) + bytes(body)
    return struct.pack("<I", len(unit)) + unit, bytes(A), strs


def _elf(dwarf: bool = True, symbols: bool = True, e_type: int = 2) -> bytes:
    """ELF32 ARM with .symtab/.strtab (g_self_state, a function, an unrelated object) and DWARF."""
    strtab = b"\0g_self_state\0main\0other_thing\0"
    sym = bytes(16)
    sym += struct.pack("<IIIBBH", 1, 0x1000, 8, 0x11, 0, 1)        # OBJECT
    sym += struct.pack("<IIIBBH", 14, 0x2000, 4, 0x12, 0, 1)       # FUNC
    sym += struct.pack("<IIIBBH", 19, 0x3000, 4, 0x11, 0, 1)       # OBJECT, name not of interest
    sections: list[tuple[str, int, bytes, int]] = [("", 0, b"", 0)]
    if symbols:
        sections += [(".symtab", 2, sym, 3), (".strtab", 3, strtab, 0)]
    if dwarf:
        info, abbrev, strs = _dwarf()
        sections += [(".debug_info", 1, info, 0), (".debug_abbrev", 1, abbrev, 0), (".debug_str", 1, strs, 0)]
    shstr = b"\0" + b"".join(n.encode() + b"\0" for n, *_ in sections[1:]) + b".shstrtab\0"
    sections.append((".shstrtab", 3, shstr, 0))
    data = bytearray(52)
    offsets = []
    for _, _, content, _ in sections:
        offsets.append(len(data))
        data += content
    shoff = len(data)
    name_off = 0
    for i, (name, typ, content, link) in enumerate(sections):
        nm = 0 if not name else shstr.index(name.encode() + b"\0")
        data += struct.pack("<IIIIIIIIII", nm, typ, 0, 0, offsets[i], len(content), link, 0, 0, 0)
    hdr = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    hdr += struct.pack("<HHIIIIIHHHHHH", e_type, 40, 1, 0, 0, shoff, 0, 52, 0, 0, 40, len(sections),
                       len(sections) - 1)
    data[:52] = hdr
    return bytes(data)


def test_parse_elf_and_layout() -> None:
    data = _elf()
    e = dbg.parse_elf(data)
    assert (e["class"], e["type"], e["machine"], e["stripped"]) == ("ELF32", "EXEC", "ARM", False)
    assert e["symbols"] == {"g_self_state": {"addr": "0x1000", "size": 8}}
    assert e["debug_sections"] == [".debug_abbrev", ".debug_info", ".debug_str"]
    lay = dbg.struct_layout(data)
    assert lay["type"] == "dev_state_t" and lay["size"] == 8
    assert [(m["offset"], m["name"], m["type"], m["size"]) for m in lay["members"]] == [
        (0, "addr", "uint8_t", 1), (1, "sn", "uint8_t[4]", 4), (5, "s.TsMinSet", "uint8_t", 1),
        (6, "s.f", "uint16_t", 2)]
    assert dbg.struct_layout(data, "missing") is None
    assert dbg.struct_layout(_elf(dwarf=False)) is None
    assert dbg.parse_elf(_elf(symbols=False, e_type=3))["stripped"] is True


def test_dump_decoding_and_redaction() -> None:
    lay = dbg.struct_layout(_elf())
    dump = bytes([7, 0x41, 0x42, 0x43, 0x44, 60, 0x34, 0x12])
    assert dbg.decode_dump(dump, lay) == {"addr": 7, "sn": "(redacted)", "s.TsMinSet": 60, "s.f": 0x1234}
    assert dbg.redact_dump(dump, lay) == bytes([7, 0, 0, 0, 0, 60, 0x34, 0x12])
    assert dbg.redact_dump(dump, None) == b""


def test_mask_ips_and_bad_elf() -> None:
    assert dbg.mask_ips("\x1b[1;32m-a 10.0.0.2 -a ::ffff:10.0.0.9 0.0.0.0:8080 127.0.0.1\x1b[0m") == \
        "-a x.x.x.x -a x.x.x.x 0.0.0.0:8080 127.0.0.1"
    with pytest.raises(dbg.ElfError):
        dbg.parse_elf(b"MZ" + bytes(60))
    with pytest.raises(dbg.ElfError):
        dbg.parse_elf(b"\x7fELF\x01\x02" + bytes(60))


async def test_debug_report_over_adb(thermo: FakeThermostat) -> None:
    thermo.dev_app_bytes = _elf()
    thermo.dev_app_mem = bytes([7, 1, 2, 3, 4, 60, 0, 1])
    r = await Deployer("h", 5555).debug_report()
    assert set(r["shell"]) == {k for k, _ in dbg.DEBUG_COMMANDS}
    assert r["shell"]["kernel"] == "output of uname from x.x.x.x"
    assert r["dev_app"]["type"] == "EXEC" and r["state_layout"]["size"] == 8
    assert r["state_dump"]["fields"]["s.TsMinSet"] == 60 and r["state_dump"]["hex"] == "0700000000" + "3c0001"
    assert r["adb_nat"] == {"behind_nat": False, "addresses_allowed": 1}
    assert any("dd if=/proc/1666/mem bs=1 skip=4096 count=8" in c for c in thermo.commands)


async def test_debug_report_without_debug_info(thermo: FakeThermostat) -> None:
    r = await Deployer("h", 5555).debug_report()                      # copy fails
    assert "copy failed" in r["dev_app"]["error"]
    thermo.dev_app_bytes = _elf(dwarf=False)
    r = await Deployer("h", 5555).debug_report()
    assert r["state_layout"] is None and "skipped" in r["state_dump"]
    thermo.dev_app_bytes = b"not an elf"
    r = await Deployer("h", 5555).debug_report()
    assert "parse failed" in r["dev_app"]["error"]
