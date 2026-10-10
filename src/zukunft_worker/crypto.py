from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class CredentialDecryptor:
    def __init__(self, private_key_path: Path):
        self._key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)

    def decrypt(self, ciphertext: str) -> dict[str, str]:
        plaintext = self._key.decrypt(
            base64.b64decode(ciphertext),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        value = json.loads(plaintext.decode("utf8"))
        if not isinstance(value, dict):
            raise ValueError("Credential payload must be an object")
        return {str(key): str(item) for key, item in value.items()}

