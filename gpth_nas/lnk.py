"""
Minimal Windows .lnk shortcut writer (MS-SHLLINK).

Generated from Linux so files at /volume1/.../Albums/<name>/*.lnk, browsed
over SMB from a Windows client, resolve to the target media file.

We emit only what's needed:
  1. LinkHeader (76 bytes)
  2. STRING_DATA: RelativePath (Unicode), then WorkingDir (Unicode).

Relative paths are used so the .lnk works regardless of how the SMB share is
named on the Windows side.

References: MS-SHLLINK §2.1 LinkHeader, §2.4 STRING_DATA.
"""
import struct
from pathlib import Path


_LINK_CLSID = bytes.fromhex('0114021400000000C000000000000046')

# LinkFlags bits we set
_HAS_RELATIVE_PATH  = 0x00000008
_HAS_WORKING_DIR    = 0x00000010
_IS_UNICODE         = 0x00000080

_FILE_ATTRIBUTE_NORMAL = 0x00000080
_SW_SHOWNORMAL = 0x00000001


def _utf16_lestr(s: str) -> bytes:
    """STRING_DATA: uint16-LE char count + UTF-16LE bytes (no terminator)."""
    encoded = s.encode('utf-16-le')
    count = len(encoded) // 2
    return struct.pack('<H', count) + encoded


def _build_link_header() -> bytes:
    out = bytearray()
    out += struct.pack('<I', 0x0000004C)                # HeaderSize
    out += _LINK_CLSID                                  # LinkCLSID (16)
    flags = _HAS_RELATIVE_PATH | _HAS_WORKING_DIR | _IS_UNICODE
    out += struct.pack('<I', flags)                     # LinkFlags
    out += struct.pack('<I', _FILE_ATTRIBUTE_NORMAL)    # FileAttributes
    out += struct.pack('<Q', 0)                         # CreationTime
    out += struct.pack('<Q', 0)                         # AccessTime
    out += struct.pack('<Q', 0)                         # WriteTime
    out += struct.pack('<I', 0)                         # FileSize
    out += struct.pack('<i', 0)                         # IconIndex
    out += struct.pack('<I', _SW_SHOWNORMAL)            # ShowCommand
    out += struct.pack('<H', 0)                         # HotKey
    out += struct.pack('<H', 0)                         # Reserved1
    out += struct.pack('<I', 0)                         # Reserved2
    out += struct.pack('<I', 0)                         # Reserved3
    assert len(out) == 76, f"LinkHeader size {len(out)} != 76"
    return bytes(out)


def _relative_windows_path(shortcut: Path, target: Path) -> str:
    """Compute a relative path from shortcut.parent to target, using backslashes."""
    sc_parts = shortcut.parent.resolve().parts
    tg_parts = target.resolve().parts

    i = 0
    while i < len(sc_parts) and i < len(tg_parts) and sc_parts[i] == tg_parts[i]:
        i += 1

    up_count = len(sc_parts) - i
    down = tg_parts[i:]
    rel_parts = ['..'] * up_count + list(down)
    return '\\'.join(rel_parts) if rel_parts else '.'


def write_lnk(shortcut_path: Path, target_path: Path) -> None:
    """Write a relative-path .lnk at shortcut_path pointing to target_path."""
    rel = _relative_windows_path(shortcut_path, target_path)
    parts = rel.split('\\')
    working_dir = '\\'.join(parts[:-1]) if len(parts) > 1 else '.'

    body = bytearray()
    body += _build_link_header()
    body += _utf16_lestr(rel)
    body += _utf16_lestr(working_dir)

    shortcut_path.write_bytes(bytes(body))
