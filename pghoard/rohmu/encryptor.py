"""
rohmu - content encryption

Copyright (c) 2016 Ohmu Ltd
See LICENSE for details
"""

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hashes import SHA1, SHA256
from cryptography.hazmat.primitives.hmac import HMAC
from cryptography.hazmat.primitives import serialization
import io
import os
import struct

FILEMAGIC = b"pghoa1"
IO_BLOCK_SIZE = 2 ** 20


class EncryptorError(Exception):
    """ EncryptorError """


class Encryptor(object):
    def __init__(self, rsa_public_key_pem):
        if not isinstance(rsa_public_key_pem, bytes):
            rsa_public_key_pem = rsa_public_key_pem.encode("ascii")
        self.rsa_public_key = serialization.load_pem_public_key(rsa_public_key_pem, backend=default_backend())
        self.cipher = None
        self.authenticator = None

    def update(self, data):
        ret = b""
        if self.cipher is None:
            key = os.urandom(16)
            nonce = os.urandom(16)
            auth_key = os.urandom(32)
            self.cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend()).encryptor()
            self.authenticator = HMAC(auth_key, SHA256(), backend=default_backend())
            pad = padding.OAEP(mgf=padding.MGF1(algorithm=SHA1()),
                               algorithm=SHA1(),
                               label=None)
            cipherkey = self.rsa_public_key.encrypt(key + nonce + auth_key, pad)
            ret = FILEMAGIC + struct.pack(">H", len(cipherkey)) + cipherkey
        cur = self.cipher.update(data)
        self.authenticator.update(cur)
        ret += cur
        return ret

    def finalize(self):
        if self.cipher is None:
            return b""  # empty plaintext input yields empty encrypted output

        ret = self.cipher.finalize()
        self.authenticator.update(ret)
        ret += self.authenticator.finalize()
        self.cipher = None
        self.authenticator = None
        return ret


class Decryptor(object):
    def __init__(self, rsa_private_key_pem):
        if not isinstance(rsa_private_key_pem, bytes):
            rsa_private_key_pem = rsa_private_key_pem.encode("ascii")
        self.rsa_private_key = serialization.load_pem_private_key(
            data=rsa_private_key_pem,
            password=None,
            backend=default_backend())
        self.cipher = None
        self.authenticator = None
        self.buf = b""

    def update(self, data):
        self.buf += data
        if self.cipher is None:
            if len(self.buf) < 8:
                return b""
            if self.buf[0:6] != FILEMAGIC:
                raise EncryptorError("Invalid magic bytes")
            cipherkeylen = struct.unpack(">H", self.buf[6:8])[0]
            if len(self.buf) < 8 + cipherkeylen:
                return b""
            pad = padding.OAEP(mgf=padding.MGF1(algorithm=SHA1()),
                               algorithm=SHA1(),
                               label=None)
            try:
                plainkey = self.rsa_private_key.decrypt(self.buf[8:8 + cipherkeylen], pad)
            except AssertionError:
                raise EncryptorError("Decrypting key data failed")
            if len(plainkey) != 64:
                raise EncryptorError("Integrity check failed")
            key = plainkey[0:16]
            nonce = plainkey[16:32]
            auth_key = plainkey[32:64]

            self.cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend()).decryptor()
            self.authenticator = HMAC(auth_key, SHA256(), backend=default_backend())
            self.buf = self.buf[8 + cipherkeylen:]

        if len(self.buf) < 32:
            return b""

        self.authenticator.update(self.buf[:-32])
        result = self.cipher.update(self.buf[:-32])
        self.buf = self.buf[-32:]

        return result

    def finalize(self):
        if self.cipher is None:
            return b""  # empty encrypted input yields empty plaintext output
        elif self.buf != self.authenticator.finalize():
            raise EncryptorError("Integrity check failed")
        result = self.cipher.finalize()
        self.buf = b""
        self.cipher = None
        self.authenticator = None
        return result


class DecryptorFile(io.BufferedIOBase):
    def __init__(self, source_fp, rsa_private_key_pem):
        super().__init__()
        self.buffer = b""
        self.buffer_offset = 0
        self.decryptor = None
        self.key = rsa_private_key_pem
        self.offset = 0
        self.state = "OPEN"
        self.source_fp = source_fp

    def _check_not_closed(self):
        if self.state == "CLOSED":
            raise ValueError("I/O operation on closed file")

    def _read_all(self):
        if self.state == "EOF":
            retval = self.buffer[self.buffer_offset:]
            self.buffer_offset = 0
            self.buffer = b""
            return retval
        blocks = []
        if self.buffer_offset > 0:
            blocks.append(self.buffer)
            self.buffer = b""
            self.buffer_offset = 0
        while True:
            data = self.source_fp.read(IO_BLOCK_SIZE)
            if not data:
                self.state = "EOF"
                data = self.decryptor.finalize()
                if data:
                    blocks.append(data)
                    self.offset += len(data)
                break
            data = self.decryptor.update(data)
            if data:
                self.offset += len(data)
                blocks.append(data)
        return b"".join(blocks)

    def _read_block(self, size):
        readylen = len(self.buffer) - self.buffer_offset
        if size <= readylen:
            retval = self.buffer[self.buffer_offset:self.buffer_offset + size]
            self.buffer_offset += size
            self.offset += len(retval)
            return retval
        if self.state == "EOF":
            retval = self.buffer[self.buffer_offset:]
            self.buffer_offset = 0
            self.buffer = b""
            return retval
        blocks = []
        if self.buffer_offset:
            blocks = [self.buffer[self.buffer_offset:]]
        else:
            blocks = [self.buffer]
        while readylen < size:
            data = self.source_fp.read(IO_BLOCK_SIZE)
            if not data:
                self.state = "EOF"
                data = self.decryptor.finalize()
                if data:
                    blocks.append(data)
                    readylen += len(data)
                break
            data = self.decryptor.update(data)
            if data:
                blocks.append(data)
                readylen += len(data)
        self.buffer = b"".join(blocks)
        self.buffer_offset = 0
        if size < readylen:
            retval = self.buffer[:size]
            self.buffer_offset = size
        else:
            retval = self.buffer
            self.buffer = b""
            self.buffer_offset = 0
        self.offset += len(retval)
        return retval

    def close(self):
        """Close stream"""
        if self.state == "CLOSED":
            return
        self.decryptor = None
        self.source_fp = None
        self.state = "CLOSED"

    @property
    def closed(self):
        """True if this stream is closed"""
        return self.state == "CLOSED"

    def fileno(self):
        self._check_not_closed()
        return self.source_fp.fileno()

    def flush(self):
        self._check_not_closed()

    def peek(self, size=-1):  # pylint: disable=unused-argument
        self._check_not_closed()
        if len(self.buffer):
            return self.buffer
        data = self.read(size)
        self.buffer += data
        return data

    def read(self, size=-1):
        """Read up to size decrypted bytes"""
        self._check_not_closed()
        if not self.decryptor:
            self.decryptor = Decryptor(self.key)
        if self.state == "EOF" or size == 0:
            return b""
        elif size < 0:
            return self._read_all()
        else:
            return self._read_block(size)

    def read1(self, size=-1):
        return self.read(size)

    def readable(self):
        """True if this stream supports reading"""
        self._check_not_closed()
        return self.state in ["OPEN", "EOF"]

    def seek(self, offset, whence=0):
        self._check_not_closed()
        if whence == 0:
            if offset < 0:
                raise ValueError("negative seek position")
            if self.offset == offset:
                return self.offset
            elif self.offset < offset:
                self.read(offset - self.offset)
                return self.offset
            elif self.offset > offset:
                # simulate backward seek by restarting from the beginning
                self.buffer = b""
                self.buffer_offset = 0
                self.source_fp.seek(0)
                self.offset = 0
                self.decryptor = None
                self.state = "OPEN"
                self.read(offset)
                return self.offset
            else:
                self.read(self.offset - offset)
                return self.offset
        elif whence == 1:
            if offset != 0:
                raise io.UnsupportedOperation("can't do nonzero cur-relative seeks")
            return self.offset
        elif whence == 2:
            if offset != 0:
                raise io.UnsupportedOperation("can't do nonzero end-relative seeks")
            self.read()
            return self.offset
        else:
            raise ValueError("Invalid whence value")

    def seekable(self):
        """True if this stream supports random access"""
        self._check_not_closed()
        return self.source_fp.seekable()

    def tell(self):
        self._check_not_closed()
        return self.offset

    def truncate(self):
        self._check_not_closed()
        raise io.UnsupportedOperation("Truncate not supported")

    def writable(self):
        """True if this stream supports writing"""
        self._check_not_closed()
        return False
