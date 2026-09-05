"""
Hardware fingerprint collection utilities.

Security notes:
- Raw hardware identifiers are never written to disk or logged.
- Only the SHA-256 hash of the combined identifiers is returned.
- Keep this file minimal and avoid importing Django or other heavy modules.
"""
from __future__ import annotations

import hashlib
import platform
import shlex
import subprocess
from typing import Tuple


def _run_powershell(cmd: str) -> str:
    """Run a PowerShell command and return the trimmed output or empty string.

    Uses `powershell` executable which is present on modern Windows systems.
    We run in non-interactive, no-profile mode to reduce startup overhead.
    """
    try:
        full_cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            cmd,
        ]
        out = subprocess.check_output(full_cmd, stderr=subprocess.DEVNULL)
        text = out.decode(errors='ignore')
        # Return the first non-empty trimmed line
        for ln in text.splitlines():
            ln = ln.strip()
            if ln:
                return ln
        return ""
    except Exception:
        return ""


_CACHED_FINGERPRINT_HASH: str | None = None


def _get_cache_file():
    try:
        from .paths import get_config_dir
        return get_config_dir() / ".hw_fingerprint.cache"
    except Exception:
        return None


def _collect_windows_uuid_and_baseboard() -> Tuple[str, str]:
    """Collect Machine UUID and motherboard serial in a single PowerShell process."""
    cmd = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID; "
        "(Get-CimInstance -ClassName Win32_BaseBoard).SerialNumber"
    )
    try:
        full_cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            cmd,
        ]
        out = subprocess.check_output(full_cmd, stderr=subprocess.DEVNULL)
        lines = [ln.strip() for ln in out.decode(errors='ignore').splitlines() if ln.strip()]
        uuid = lines[0] if len(lines) > 0 else ""
        mb = lines[1] if len(lines) > 1 else ""
        return uuid, mb
    except Exception:
        return "", ""


def get_fingerprint_hash() -> str:
    """Return SHA-256 hash of the stable machine identifiers.

    Uses an in-memory and on-disk cache to avoid launching PowerShell
    on every Django startup. Falls back to executing the PowerShell query
    if the cache is missing or corrupt.
    """
    global _CACHED_FINGERPRINT_HASH

    if _CACHED_FINGERPRINT_HASH and len(_CACHED_FINGERPRINT_HASH) == 64:
        return _CACHED_FINGERPRINT_HASH

    cache_file = _get_cache_file()
    if cache_file and cache_file.is_file():
        try:
            cached = cache_file.read_text(encoding="utf-8").strip()
            if len(cached) == 64 and all(c in "0123456789abcdefABCDEF" for c in cached):
                _CACHED_FINGERPRINT_HASH = cached.lower()
                return _CACHED_FINGERPRINT_HASH
        except Exception:
            pass

    if platform.system().lower() == "windows":
        uuid_val, mb_val = _collect_windows_uuid_and_baseboard()
    else:
        # Non-windows fallback: use platform.node() as UUID and empty baseboard
        uuid_val = platform.node() or ""
        mb_val = ""

    combined = "|".join([uuid_val, mb_val])
    digest = hashlib.sha256(combined.encode('utf-8')).hexdigest()

    _CACHED_FINGERPRINT_HASH = digest

    if cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(digest, encoding="utf-8")
        except Exception:
            pass

    # Short-lived cleanup
    uuid_val = mb_val = combined = None

    return digest

