"""The QEMU microphone port keeps its own errors out of the way of the real one."""

import asyncio
import os
import socket
import unittest

from mind.mic_port import MicrophonePort


class MicrophonePortStop(unittest.IsolatedAsyncioTestCase):
    def port(self):
        ours, host = socket.socketpair()
        port = MicrophonePort.__new__(MicrophonePort)
        port.fd = os.dup(ours.fileno())
        ours.close()
        os.set_blocking(port.fd, False)
        port.audio = bytearray()
        self.addCleanup(port.close)
        return port, host

    async def test_start_and_stop_commands_frame_a_recording(self):
        port, host = self.port()
        async with port.recording() as stream:
            host.sendall(b"\x01\x02" * 4)
            self.assertEqual(await asyncio.wait_for(stream.readexactly(8), 1), b"\x01\x02" * 4)
        self.assertEqual(host.recv(64), b"start\nstop\n")
        host.close()

    async def test_a_vanished_host_does_not_mask_the_stream_error(self):
        port, host = self.port()
        with self.assertRaises(asyncio.IncompleteReadError):
            async with port.recording() as stream:
                host.close()
                await asyncio.wait_for(stream.readexactly(8), 1)


if __name__ == "__main__":
    unittest.main()
