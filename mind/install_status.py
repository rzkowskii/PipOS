"""Fixed installation status lines. A local screen supplies the optional sink."""

from contextvars import ContextVar

TOPIC = "voice.install_status"
FRESH_FOR = 3.0
LINES = {
    "prepare": "Preparing the installation.",
    "write": "Writing the installation to disk.",
    "verify": "Verifying the installation.",
    "commit": "Finishing the installation.",
    "complete": "Installation complete.",
    "stopped": "Installation stopped. Check the screen.",
}
sink = ContextVar("install_status_sink", default=None)


def report(stage):
    publish = sink.get()
    if publish is not None and stage in LINES:
        publish(stage)
