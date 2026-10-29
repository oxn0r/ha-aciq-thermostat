"""Debug report helpers: what's needed to support a new firmware build, from one report.

Pure functions (no I/O): parse dev_app's ELF symbols and DWARF struct layout, decode a memory
dump of g_self_state against that layout, mask addresses in shell output. The ADB side is
Deployer.debug_report(). Nothing here sends anything to the thermostat.
"""

from __future__ import annotations

import re
import struct
from typing import Any

STATE_SYMBOL = "g_self_state"              # dev_state_t: equipment data, settings, sub-states
# globals worth listing for mapping work (equipment, outdoor unit, defrost, sub-states)
SYMBOL_HINT = re.compile(r"state|defrost|frost|outdoor|odu|comp|equip|sensor|fault", re.I)
# struct members never dumped (serial number and anything network-like)
PRIVATE_MEMBER = re.compile(r"(^|[._])(sn|serial|mac|ssid|passw\w*|key|token|ip|addr_ip)($|[._\[])", re.I)
# read-only shell commands; output is IP-masked and capped. Order = report order.
DEBUG_COMMANDS: tuple[tuple[str, str], ...] = (
    ("os_release", "cat /etc/openwrt_release"),
    ("kernel", "uname -a"),
    ("uptime_s", "cut -d' ' -f1 /proc/uptime"),
    ("memory", "free | head -2"),
    ("binaries", "ls -l /usr/bin/dev_app /usr/bin/msmart /usr/bin/meiju; "
                 "sha256sum /usr/bin/dev_app /usr/bin/msmart /usr/bin/meiju"),
    ("processes", "ps | grep -E 'dev_app|msmart|meiju|thermostat_api|logread' | grep -v grep"),
    ("ipc_queues", "cat /proc/sysvipc/msg"),
    ("listeners", "netstat -ltn"),
    ("log_files", "for f in /tmp/.lastlog.old /tmp/.lastlog; do ls -l $f; head -1 $f | cut -c1-23; "
                  "tail -1 $f | cut -c1-23; wc -l < $f; done"),
    # which functions / source lines log, and how often (no values): the map for a new build
    ("log_sources", "cat /tmp/.lastlog.old /tmp/.lastlog 2>/dev/null | grep -oE "
                    "'(dev_app|meiju): [A-Za-z0-9_]+\\]( <I>\\([^)]*\\))?' | sort | uniq -c | sort -rn | head -150"),
)
DEBUG_OUTPUT_MAX = 12000                    # characters per command
DEV_APP_PATH = "/usr/bin/dev_app"

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_MAPPED = re.compile(r"::ffff:[0-9.]+", re.I)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def mask_ips(text: str) -> str:
    """Replace IP addresses (keeping loopback / any, which say how a port is bound) and drop
    terminal colour codes."""
    def sub(m: re.Match[str]) -> str:
        ip = m.group(0)
        return ip if ip.startswith("127.") or ip == "0.0.0.0" else "x.x.x.x"
    return _IPV4.sub(sub, _IPV6_MAPPED.sub("x.x.x.x", _ANSI.sub("", text)))


# ------------------------------------------------------------------ ELF

class ElfError(ValueError):
    pass


def _elf_header(data: bytes) -> tuple[str, int, int, int, int, int, int, int]:
    if data[:4] != b"\x7fELF":
        raise ElfError("not an ELF file")
    if data[5] != 1:
        raise ElfError("big-endian ELF not supported")
    cls = data[4]
    if cls == 1:
        e_type, e_machine = struct.unpack_from("<HH", data, 16)
        shoff, = struct.unpack_from("<I", data, 32)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 46)
        return "ELF32", e_type, e_machine, shoff, shentsize, shnum, shstrndx, cls
    if cls == 2:
        e_type, e_machine = struct.unpack_from("<HH", data, 16)
        shoff, = struct.unpack_from("<Q", data, 40)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 58)
        return "ELF64", e_type, e_machine, shoff, shentsize, shnum, shstrndx, cls
    raise ElfError("unknown ELF class")


def _sections(data: bytes) -> tuple[dict[str, tuple[int, int, int]], int, int, int]:
    """name -> (offset, size, link index) plus e_type, e_machine, class."""
    _, e_type, e_machine, shoff, shentsize, shnum, shstrndx, cls = _elf_header(data)
    raw = []
    for i in range(shnum):
        base = shoff + i * shentsize
        if cls == 1:
            name, _typ, _flags, _addr, off, size, link = struct.unpack_from("<IIIIIII", data, base)
        else:
            name, _typ, _flags, _addr, off, size, link = struct.unpack_from("<IIQQQQI", data, base)
        raw.append((name, off, size, link))
    str_off = raw[shstrndx][1] if shstrndx < len(raw) else 0
    out: dict[str, tuple[int, int, int]] = {}
    names: list[str] = []
    for name, off, size, link in raw:
        end = data.find(b"\0", str_off + name)
        n = data[str_off + name:end].decode(errors="replace")
        names.append(n)
        out[n] = (off, size, link)
    return out, e_type, e_machine, cls


def parse_elf(data: bytes) -> dict[str, Any]:
    """ELF facts for the report: type (EXEC = fixed addresses), debug sections, interesting
    global objects (name, address, size)."""
    secs, e_type, e_machine, cls = _sections(data)
    info: dict[str, Any] = {
        "class": "ELF32" if cls == 1 else "ELF64",
        "type": {2: "EXEC", 3: "DYN"}.get(e_type, str(e_type)),
        "machine": {40: "ARM", 183: "AArch64", 3: "x86", 62: "x86-64"}.get(e_machine, str(e_machine)),
        "debug_sections": sorted(n for n in secs if n.startswith(".debug")),
        "stripped": ".symtab" not in secs,
        "symbols": {},
    }
    if ".symtab" in secs and ".strtab" in secs:
        off, size, _ = secs[".symtab"]
        str_off = secs[".strtab"][0]
        ent = 16 if cls == 1 else 24
        for i in range(size // ent):
            b = off + i * ent
            if cls == 1:
                st_name, value, sz, st_info = struct.unpack_from("<IIIB", data, b)
            else:
                st_name, st_info, _o, _sh, value, sz = struct.unpack_from("<IBBHQQ", data, b)
            if st_info & 0xF != 1 or not sz:           # STT_OBJECT with a size
                continue
            end = data.find(b"\0", str_off + st_name)
            name = data[str_off + st_name:end].decode(errors="replace")
            if name == STATE_SYMBOL or SYMBOL_HINT.search(name):
                info["symbols"][name] = {"addr": f"0x{value:x}", "size": sz}
    return info


# ------------------------------------------------------------------ DWARF (2-4, 32-bit)

def _uleb(b: bytes, i: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << shift
        shift += 7
        if not x & 0x80:
            return v, i


def _sleb(b: bytes, i: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << shift
        shift += 7
        if not x & 0x80:
            if x & 0x40:
                v -= 1 << shift
            return v, i


_TAG_VARIABLE, _TAG_MEMBER, _TAG_STRUCT, _TAG_TYPEDEF = 0x34, 0x0D, 0x13, 0x16
_TAG_ARRAY, _TAG_SUBRANGE, _TAG_BASE, _TAG_POINTER, _TAG_UNION = 0x01, 0x21, 0x24, 0x0F, 0x17
_TAG_CONST, _TAG_VOLATILE, _TAG_ENUM = 0x26, 0x35, 0x04
_AT_NAME, _AT_TYPE, _AT_BYTE_SIZE, _AT_LOC, _AT_MEMBER_LOC = 0x03, 0x49, 0x0B, 0x02, 0x38
_AT_UPPER, _AT_COUNT = 0x2F, 0x37


class _Die:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: int, attrs: dict[int, Any]) -> None:
        self.tag, self.attrs = tag, attrs
        self.children: list[_Die] = []


def _parse_dwarf(info: bytes, abbrev: bytes, strs: bytes, addr_size_default: int) -> dict[int, _Die]:
    dies: dict[int, _Die] = {}
    pos = 0
    while pos + 11 <= len(info):
        cu_off = pos
        unit_len, = struct.unpack_from("<I", info, pos)
        if unit_len >= 0xFFFFFFF0:
            raise ElfError("64-bit DWARF not supported")
        end = pos + 4 + unit_len
        version, = struct.unpack_from("<H", info, pos + 4)
        if version > 4:
            raise ElfError(f"DWARF {version} not supported")
        abbr_off, = struct.unpack_from("<I", info, pos + 6)
        addr_size = info[pos + 10] or addr_size_default
        i = pos + 11
        # abbreviation table for this CU
        table: dict[int, tuple[int, bool, list[tuple[int, int]]]] = {}
        a = abbr_off
        while True:
            code, a = _uleb(abbrev, a)
            if not code:
                break
            tag, a = _uleb(abbrev, a)
            has_children = bool(abbrev[a])
            a += 1
            specs = []
            while True:
                at, a = _uleb(abbrev, a)
                form, a = _uleb(abbrev, a)
                if not at and not form:
                    break
                specs.append((at, form))
            table[code] = (tag, has_children, specs)
        stack: list[_Die] = []
        while i < end:
            die_off = i
            code, i = _uleb(info, i)
            if not code:
                if stack:
                    stack.pop()
                continue
            tag, has_children, specs = table[code]
            attrs: dict[int, Any] = {}
            for at, form in specs:
                val, i = _read_form(info, i, form, cu_off, addr_size, strs)
                attrs[at] = val
            die = _Die(tag, attrs)
            dies[die_off] = die
            if stack:
                stack[-1].children.append(die)
            if has_children:
                stack.append(die)
        pos = end
    return dies


def _read_form(b: bytes, i: int, form: int, cu_off: int, addr_size: int, strs: bytes) -> tuple[Any, int]:
    if form == 0x16:                                   # indirect
        form, i = _uleb(b, i)
    if form == 0x01:                                   # addr
        return int.from_bytes(b[i:i + addr_size], "little"), i + addr_size
    if form in (0x0B, 0x11, 0x0C):                     # data1, ref1, flag
        v = b[i]
        return (v + cu_off if form == 0x11 else v), i + 1
    if form in (0x05, 0x12):                           # data2, ref2
        v, = struct.unpack_from("<H", b, i)
        return (v + cu_off if form == 0x12 else v), i + 2
    if form in (0x06, 0x13, 0x17, 0x10):               # data4, ref4, sec_offset, ref_addr
        v, = struct.unpack_from("<I", b, i)
        return (v + cu_off if form == 0x13 else v), i + 4
    if form in (0x07, 0x14, 0x20):                     # data8, ref8, ref_sig8
        v, = struct.unpack_from("<Q", b, i)
        return (v + cu_off if form == 0x14 else v), i + 8
    if form == 0x0D:                                   # sdata
        return _sleb(b, i)
    if form in (0x0F, 0x15):                           # udata, ref_udata
        v, i = _uleb(b, i)
        return (v + cu_off if form == 0x15 else v), i
    if form == 0x08:                                   # string
        end = b.index(b"\0", i)
        return b[i:end].decode(errors="replace"), end + 1
    if form == 0x0E:                                   # strp
        off, = struct.unpack_from("<I", b, i)
        end = strs.index(b"\0", off)
        return strs[off:end].decode(errors="replace"), i + 4
    if form in (0x09, 0x18):                           # block, exprloc
        n, i = _uleb(b, i)
        return b[i:i + n], i + n
    if form == 0x0A:                                   # block1
        n = b[i]
        return b[i + 1:i + 1 + n], i + 1 + n
    if form == 0x03:
        n, = struct.unpack_from("<H", b, i)
        return b[i + 2:i + 2 + n], i + 2 + n
    if form == 0x04:
        n, = struct.unpack_from("<I", b, i)
        return b[i + 4:i + 4 + n], i + 4 + n
    if form == 0x19:                                   # flag_present
        return True, i
    raise ElfError(f"unsupported DWARF form 0x{form:x}")


def _resolve(dies: dict[int, _Die], die: _Die | None) -> _Die | None:
    while die is not None and die.tag in (_TAG_TYPEDEF, _TAG_CONST, _TAG_VOLATILE):
        die = dies.get(die.attrs.get(_AT_TYPE, -1))
    return die


def _type_name(dies: dict[int, _Die], die: _Die | None) -> str:
    dims = ""
    while die is not None:
        if die.tag == _TAG_ARRAY:
            for c in die.children:
                if c.tag == _TAG_SUBRANGE:
                    n = c.attrs.get(_AT_COUNT)
                    if n is None and isinstance(c.attrs.get(_AT_UPPER), int):
                        n = c.attrs[_AT_UPPER] + 1
                    dims += f"[{n if n is not None else ''}]"
            die = dies.get(die.attrs.get(_AT_TYPE, -1))
            continue
        if die.tag == _TAG_POINTER:
            return "*" + dims
        if _AT_NAME in die.attrs:
            return str(die.attrs[_AT_NAME]) + dims
        die = dies.get(die.attrs.get(_AT_TYPE, -1))
    return "void" + dims


def _byte_size(dies: dict[int, _Die], die: _Die | None) -> int | None:
    die_r = _resolve(dies, die)
    if die_r is None:
        return None
    if _AT_BYTE_SIZE in die_r.attrs:
        return int(die_r.attrs[_AT_BYTE_SIZE])
    if die_r.tag == _TAG_ARRAY:
        n = 1
        for c in die_r.children:
            if c.tag == _TAG_SUBRANGE:
                cnt = c.attrs.get(_AT_COUNT)
                if cnt is None and isinstance(c.attrs.get(_AT_UPPER), int):
                    cnt = c.attrs[_AT_UPPER] + 1
                n *= int(cnt or 0)
        el = _byte_size(dies, dies.get(die_r.attrs.get(_AT_TYPE, -1)))
        return n * el if el is not None else None
    return None


def _member_offset(v: Any) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, (bytes, bytearray)) and v and v[0] == 0x23:   # DW_OP_plus_uconst
        return _uleb(bytes(v), 1)[0]
    return 0


def struct_layout(data: bytes, variable: str = STATE_SYMBOL, depth: int = 2) -> dict[str, Any] | None:
    """Flattened member layout of a global variable's struct type, from DWARF (None if the
    binary has no DWARF or no such variable)."""
    secs, *_rest = _sections(data)
    if ".debug_info" not in secs or ".debug_abbrev" not in secs:
        return None

    def get(n: str) -> bytes:
        return data[secs[n][0]:secs[n][0] + secs[n][1]] if n in secs else b""

    dies = _parse_dwarf(get(".debug_info"), get(".debug_abbrev"), get(".debug_str"), 4)
    var = next((d for d in dies.values() if d.tag == _TAG_VARIABLE and d.attrs.get(_AT_NAME) == variable
                and _AT_LOC in d.attrs), None)
    if var is None:
        return None
    typ = dies.get(var.attrs.get(_AT_TYPE, -1))
    st = _resolve(dies, typ)
    if st is None or st.tag not in (_TAG_STRUCT, _TAG_UNION):
        return None
    members: list[dict[str, Any]] = []

    def walk(s: _Die, base: int, prefix: str, level: int) -> None:
        for m in s.children:
            if m.tag != _TAG_MEMBER:
                continue
            off = base + _member_offset(m.attrs.get(_AT_MEMBER_LOC, 0))
            mt = dies.get(m.attrs.get(_AT_TYPE, -1))
            name = prefix + str(m.attrs.get(_AT_NAME, "?"))
            sub = _resolve(dies, mt)
            if sub is not None and sub.tag in (_TAG_STRUCT, _TAG_UNION) and level < depth:
                walk(sub, off, name + ".", level + 1)
                continue
            members.append({"offset": off, "name": name, "type": _type_name(dies, mt),
                            "size": _byte_size(dies, mt)})

    walk(st, 0, "", 0)
    return {"variable": variable, "type": _type_name(dies, typ), "size": _byte_size(dies, typ),
            "members": members}


def decode_dump(dump: bytes, layout: dict[str, Any]) -> dict[str, Any]:
    """Member values from a memory dump (little-endian ints, arrays as hex); private members
    (serial, network) are left out."""
    out: dict[str, Any] = {}
    for m in layout["members"]:
        off, size, name = m["offset"], m["size"], m["name"]
        if PRIVATE_MEMBER.search(name):
            out[name] = "(redacted)"
            continue
        if not size or off + size > len(dump):
            continue
        raw = dump[off:off + size]
        out[name] = int.from_bytes(raw, "little") if size in (1, 2, 4) and "[" not in m["type"] else raw.hex()
    return out


def redact_dump(dump: bytes, layout: dict[str, Any] | None) -> bytes:
    """Zero private members in the raw dump (all of it when the layout is unknown)."""
    if layout is None:
        return b""
    b = bytearray(dump)
    for m in layout["members"]:
        if PRIVATE_MEMBER.search(m["name"]) and m["size"]:
            b[m["offset"]:m["offset"] + m["size"]] = bytes(min(m["size"], max(0, len(b) - m["offset"])))
    return bytes(b)
