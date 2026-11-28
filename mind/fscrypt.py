"""Linux x86_64 fscrypt v2 operations for boot-local working files."""

import ctypes
import errno
import fcntl
import os
import struct


def _command(direction, number, size):
    return (direction << 30) | (size << 16) | (ord("f") << 8) | number


# Linux 6.18 include/uapi/linux/fscrypt.h. The policy ioctl encodings still
# advertise the v1 struct (SET) and the size and version fields (GET_EX).
_SET = _command(2, 19, 12)
_GET_EX = _command(3, 22, 9)
_ADD_HEADER = 80
_ADD = _command(3, 23, _ADD_HEADER)
_REMOVE = _command(3, 24, 64)
_GET_STATUS = _command(3, 26, 128)
_RAW_SIZE = 64
_KEY_ID = slice(8, 24)
_POLICY_HEADER = bytes((2, 1, 4, 3, 0, 0, 0, 0))  # v2, AES-256-XTS/CTS, PAD_32


def _key_argument(size, identifier=None):
    arg = bytearray(size)
    struct.pack_into("<I", arg, 0, 2)  # FSCRYPT_KEY_SPEC_TYPE_IDENTIFIER
    if identifier is not None:
        if len(identifier) != 16:
            raise ValueError("Invalid fscrypt key identifier")
        arg[_KEY_ID] = identifier
    return arg


def add_key(mount_fd):
    """Generate and enroll raw bytes in one buffer. Return only the key identifier."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getrandom.restype = ctypes.c_ssize_t
    libc.getrandom.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint)
    libc.ioctl.restype = ctypes.c_int
    libc.ioctl.argtypes = (ctypes.c_int, ctypes.c_ulong)
    arg = _key_argument(_ADD_HEADER + _RAW_SIZE)
    struct.pack_into("<I", arg, 40, _RAW_SIZE)
    view = (ctypes.c_char * len(arg)).from_buffer(arg)
    raw = ctypes.addressof(view) + _ADD_HEADER
    try:
        count = libc.getrandom(raw, _RAW_SIZE, 1)  # GRND_NONBLOCK, fall back to RAM if entropy is not ready
        if count != _RAW_SIZE:
            code = ctypes.get_errno() if count == -1 else errno.EIO
            raise OSError(code, os.strerror(code))
        if libc.ioctl(mount_fd, _ADD, ctypes.byref(view)) == -1:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return bytes(arg[_KEY_ID])
    finally:
        ctypes.memset(raw, 0, _RAW_SIZE)


def set_policy(directory_fd, identifier):
    if len(identifier) != 16:
        raise ValueError("Invalid fscrypt key identifier")
    fcntl.ioctl(directory_fd, _SET, _POLICY_HEADER + identifier)


def policy_key(directory_fd):
    arg = bytearray(32)
    struct.pack_into("<Q", arg, 0, 24)
    try:
        fcntl.ioctl(directory_fd, _GET_EX, arg)
    except OSError as error:
        if error.errno == errno.ENODATA:
            return None
        raise
    if struct.unpack_from("<Q", arg)[0] != 24 or arg[8:16] != _POLICY_HEADER:
        raise ValueError("Unexpected fscrypt directory policy")
    return bytes(arg[16:32])


def key_status(mount_fd, identifier):
    arg = _key_argument(128, identifier)
    fcntl.ioctl(mount_fd, _GET_STATUS, arg)
    status = struct.unpack_from("<I", arg, 64)[0]
    return {1: "absent", 2: "present", 3: "busy"}.get(status, "unknown")


def remove_key(mount_fd, identifier):
    arg = _key_argument(64, identifier)
    fcntl.ioctl(mount_fd, _REMOVE, arg)
