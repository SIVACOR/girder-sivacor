import os
import json
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MASTER_KEY_HEX = os.environ.get(
    "MASTER_KEY_HEX", "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
)
_master_key = bytes.fromhex(MASTER_KEY_HEX)
MASTER_AES = AESGCM(_master_key)


def encrypt_job_secrets(env_secrets_list):
    """
    Encrypts a list of secrets using Envelope Encryption.
    """
    job_key = AESGCM.generate_key(bit_length=256)
    job_aes = AESGCM(job_key)

    plaintext_data = json.dumps(env_secrets_list).encode("utf-8")
    nonce_data = os.urandom(12)  # GCM needs a 12-byte nonce
    encrypted_secrets = job_aes.encrypt(nonce_data, plaintext_data, None)

    nonce_key = os.urandom(12)
    wrapped_job_key = MASTER_AES.encrypt(nonce_key, job_key, None)

    return {
        "encrypted_secrets": base64.b64encode(nonce_data + encrypted_secrets).decode(
            "utf-8"
        ),
        "wrapped_job_key": base64.b64encode(nonce_key + wrapped_job_key).decode(
            "utf-8"
        ),
    }
