"""Bridge host microphone PCM into QEMU on demand."""

import asyncio
import signal
import sys


async def record(writer, device):
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-i", f":{device}", "-ac", "1", "-ar", "16000",
            "-f", "s16le", "pipe:1", stdout=asyncio.subprocess.PIPE,
        )
    )
    try:
        process = await asyncio.shield(spawn)
        while audio := await process.stdout.read(4096):
            writer.write(audio)
            await writer.drain()
        if await process.wait():
            print("Microphone capture exited. Check macOS microphone access and the selected input.", file=sys.stderr)
    finally:
        process = await spawn
        if process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.communicate(), 1)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
            except ProcessLookupError:
                await process.wait()


async def stop_recording(task):
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def serve(path, device):
    for _ in range(150):
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
            break
        except OSError:
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError("could not connect to QEMU's microphone port")

    recording = None
    try:
        while line := await reader.readline():
            if line == b"start\n":
                if recording is None or recording.done():
                    recording = asyncio.create_task(record(writer, device))
            elif line == b"stop\n":
                await stop_recording(recording)
                recording = None
    finally:
        await stop_recording(recording)
        writer.close()
        await writer.wait_closed()


async def run(command, path, device):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    qemu = await asyncio.create_subprocess_exec(*command)
    exited = asyncio.create_task(qemu.wait())
    bridge = asyncio.create_task(serve(path, device))
    try:
        done, _ = await asyncio.wait((exited, bridge), return_when=asyncio.FIRST_COMPLETED)
        if exited in done:
            return exited.result()
        await bridge
        return await asyncio.wait_for(asyncio.shield(exited), 5)
    finally:
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        if qemu.returncode is None:
            try:
                qemu.terminate()
                await asyncio.wait_for(asyncio.shield(exited), 5)
            except asyncio.TimeoutError:
                qemu.kill()
                await exited
            except ProcessLookupError:
                await exited
