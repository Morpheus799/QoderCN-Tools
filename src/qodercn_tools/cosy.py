"""COSY request signing — a faithful port of the Go client's headers() method.

Authorization = "Bearer COSY.<payloadB64>.<md5hex>" where
  payloadB64 = base64.std({cosyVersion,ideVersion,info,requestId,version})
  md5hex     = md5( payloadB64 \\n cosyKey \\n date \\n body \\n normalizePath(path) )
  date       = str(int(time.time()))  (Unix seconds)
The signature is self-consistent: the gateway re-signs the body/path it receives,
so JSON escaping differences never break validation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

from .credentials import Credential, machine_os_header

DEFAULT_COSY_VERSION = "1.1.28"


def compact_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def new_uuid() -> str:
    return str(uuid.uuid4())


def normalize_path(path: str) -> str:
    prefix = "/algo"
    return path[len(prefix):] if path.startswith(prefix) else path


def build_headers(cred: Credential, path: str, body: str, cosy_version: str = DEFAULT_COSY_VERSION) -> dict[str, str]:
    date = str(int(time.time()))

    auth_payload = {
        "cosyVersion": cosy_version,
        "ideVersion": "",
        "info": cred.encrypt_user_info,
        "requestId": new_uuid(),
        "version": "v1",
    }
    payload_b64 = base64.b64encode(compact_json(auth_payload).encode("utf-8")).decode("ascii")

    preimage = "\n".join([payload_b64, cred.cosy_key, date, body, normalize_path(path)])
    signature = hashlib.md5(preimage.encode("utf-8")).hexdigest()

    return {
        "Authorization": f"Bearer COSY.{payload_b64}.{signature}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Appcode": "cosy",
        "Cosy-Date": date,
        "Cosy-Key": cred.cosy_key,
        "Cosy-Machineid": cred.machine_id,
        "Cosy-User": cred.user_id,
        "Cosy-Clientip": "198.18.0.1",
        "Cosy-Clienttype": "5",
        "Cosy-Machineos": machine_os_header(),
        "Cosy-Machinetoken": "",
        "Cosy-Machinetype": "5",
        "Cosy-Version": cosy_version,
        "Cosy-Business-Product": "cli",
        "Cosy-Business-Type": "agent",
        "Cosy-Scene": "assistant",
        "Login-Version": "v2",
        "User-Agent": "qodercn-tools/remote",
        "Cache-Control": "no-cache",
    }
