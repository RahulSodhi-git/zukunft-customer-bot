import base64
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from zukunft_worker.crypto import CredentialDecryptor


class CredentialDecryptorTests(unittest.TestCase):
    def test_decrypts_oaep_sha256_payload(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ciphertext = private_key.public_key().encrypt(
            json.dumps({"apiKey": "key", "apiSecret": "secret"}).encode(),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private.pem"
            path.write_bytes(private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            result = CredentialDecryptor(path).decrypt(base64.b64encode(ciphertext).decode())
        self.assertEqual(result, {"apiKey": "key", "apiSecret": "secret"})


if __name__ == "__main__":
    unittest.main()

