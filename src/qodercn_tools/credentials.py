"""Load QoderCN gateway credentials.

Two sources:
  1. An explicit plaintext auth file (JSON) — see `storedCredentialFile` shape.
  2. The QoderCN IDE/CLI login cache — an AES-128-CBC blob whose key and IV are
     both the first 16 bytes of the machine id, PKCS#7 padded.

Only the four fields the signature/headers need are required: cosy_key,
encrypt_user_info, user_id, machine_id. access_token is optional (quota only).
"""

from __future__ import annotations

import base64
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


@dataclass
class Credential:
    cosy_key: str
    encrypt_user_info: str
    user_id: str
    machine_id: str
    access_token: str = ""
    source: str = ""

    def validate(self) -> None:
        missing = [
            name
            for name, val in (
                ("cosy_key", self.cosy_key),
                ("encrypt_user_info", self.encrypt_user_info),
                ("user_id", self.user_id),
                ("machine_id", self.machine_id),
            )
            if not val
        ]
        if missing:
            raise CredentialError(f"credential missing required fields: {', '.join(missing)}")


class CredentialError(RuntimeError):
    pass


# --- machine OS header --------------------------------------------------------

def machine_os_header() -> str:
    """Return "<arch>_<os>" the way the Go client derives Cosy-Machineos."""
    machine = platform.machine().lower()
    system = platform.system().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine
    )
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system, system)
    return f"{arch}_{os_name}"


# --- explicit auth file -------------------------------------------------------

def _load_auth_file(path: Path) -> Credential:
    data = json.loads(path.read_text(encoding="utf-8"))
    auth = data.get("auth") or {}
    cred = Credential(
        cosy_key=auth.get("cosy_key", ""),
        encrypt_user_info=auth.get("encrypt_user_info", ""),
        user_id=auth.get("user_id", ""),
        machine_id=auth.get("machine_id", ""),
        access_token=auth.get("access_token", ""),
        source=data.get("source", "auth-file"),
    )
    cred.validate()
    return cred


# --- encrypted IDE/CLI cache --------------------------------------------------

_USER_BLOB_NAMES = ("cache/user", ".auth/user")
_MACHINE_ID_NAMES = ("cache/id", ".auth/machine_id", ".auth/id", "cli/.auth/id")


def _candidate_cache_dirs() -> list[Path]:
    env_dir = os.environ.get("LINGMA_CACHE_DIR")
    if env_dir:
        return [Path(env_dir).expanduser()]

    home = Path.home()
    dirs: list[Path] = [
        home / ".qoder-cn",
        home / ".qodercn",
        home / ".qoder",
        home / ".lingma",
        home / ".config" / "QoderCN",
        home / ".config" / "Qoder",
        home / ".config" / "Lingma",
        # macOS application support
        home / "Library" / "Application Support" / "QoderCN" / "SharedClientCache",
        home / "Library" / "Application Support" / "Qoder" / "SharedClientCache",
        home / "Library" / "Application Support" / "Lingma" / "SharedClientCache",
    ]
    # VS Code globalStorage for the Tongyi Lingma extension
    for base in (
        home / ".config" / "Code" / "User" / "globalStorage",
        home / "Library" / "Application Support" / "Code" / "User" / "globalStorage",
    ):
        dirs.append(base / "alibaba-cloud.tongyi-lingma")
    return dirs


def _first_existing(cache_dir: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = cache_dir / name
        if candidate.is_file():
            return candidate
    return None


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise CredentialError("empty plaintext after decrypt")
    pad = data[-1]
    if pad < 1 or pad > 16 or pad > len(data):
        raise CredentialError("invalid PKCS#7 padding")
    return data[:-pad]


def _decrypt_cache_user(blob: bytes, machine_id: str) -> bytes:
    if len(machine_id) < 16:
        raise CredentialError("machine id too short to derive AES key")
    key = machine_id[:16].encode("utf-8")  # key and IV are both the first 16 bytes
    raw = base64.b64decode(blob)
    cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    decryptor = cipher.decryptor()
    plaintext = decryptor.update(raw) + decryptor.finalize()
    return _pkcs7_unpad(plaintext)


def _load_cache_credential(cache_dir: Path) -> Credential | None:
    user_file = _first_existing(cache_dir, _USER_BLOB_NAMES)
    id_file = _first_existing(cache_dir, _MACHINE_ID_NAMES)
    if user_file is None or id_file is None:
        return None

    machine_id = id_file.read_text(encoding="utf-8").strip()
    if not machine_id:
        return None

    plaintext = _decrypt_cache_user(user_file.read_bytes(), machine_id)
    payload = json.loads(plaintext)
    cred = Credential(
        cosy_key=payload.get("key", ""),
        encrypt_user_info=payload.get("encrypt_user_info", ""),
        user_id=payload.get("uid", ""),
        machine_id=machine_id,
        access_token=payload.get("access_token", ""),
        source=f"cache:{cache_dir}",
    )
    cred.validate()
    return cred


def load_credential(auth_file: str | None = None) -> Credential:
    """Load a credential from an explicit auth file or the IDE/CLI cache."""
    if auth_file:
        return _load_auth_file(Path(auth_file).expanduser())

    last_error: Exception | None = None
    for cache_dir in _candidate_cache_dirs():
        if not cache_dir.is_dir():
            continue
        try:
            cred = _load_cache_credential(cache_dir)
        except Exception as exc:  # keep scanning other dirs on a decrypt/parse miss
            last_error = exc
            continue
        if cred is not None:
            return cred

    if last_error is not None:
        raise CredentialError(f"no usable credential found (last error: {last_error})")
    raise CredentialError(
        "no credential found: set an explicit --auth-file, or LINGMA_CACHE_DIR to the QoderCN cache directory"
    )
