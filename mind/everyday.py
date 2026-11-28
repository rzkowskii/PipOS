"""The registry of everyday code skills (docs/PIP-FUNCTIONS.md 5.2).

The mind asks `may_match(text)` before starting a speculative model read, and
calls `handle(text, ctx)` at step 5 of its routing, after its own commands.
Each skill module is imported lazily, on first use, in a fixed order; a module
that is missing (another part not built into this image yet) is skipped, so
the rest still answer. A handler that fails is logged and passed over: the
utterance then goes on to the next handler, and in the end to the model.

Handlers never call the model, and their lines are not archived unless the
Reply says keep=True.
"""

import importlib
import inspect
import logging

ORDER = ("clock_set", "readback", "calc", "units", "lists", "play", "body_talk", "device", "removable_media", "keep", "doctor",
         "action_log", "diary", "ponder", "voice_engines", "care_routes", "library", "night_speech")

log = logging.getLogger("being.everyday")


class Registry:
    def __init__(self, names=ORDER, package=__package__ or "mind"):
        self.names = tuple(names)
        self.package = package
        self._modules = None
        self.missing = ()

    def modules(self):
        if self._modules is None:
            found, missing = [], []
            for name in self.names:
                qualified = f"{self.package}.{name}" if self.package else name
                try:
                    module = importlib.import_module(qualified)
                except ModuleNotFoundError as error:
                    # Only the module itself being absent is expected; a
                    # module that imports something absent is a fault.
                    if error.name not in (qualified, name):
                        log.warning("everyday skill %s failed to import: %s", name, error)
                    missing.append(name)
                    continue
                except Exception as error:  # a broken skill must not take the others down
                    log.warning("everyday skill %s failed to import: %s", name, error)
                    missing.append(name)
                    continue
                if not callable(getattr(module, "handle", None)):
                    missing.append(name)
                    continue
                found.append(module)
            self._modules = tuple(found)
            self.missing = tuple(missing)
        return self._modules

    def may_match(self, text):
        for module in self.modules():
            check = getattr(module, "may_match", None)
            if check is None:
                continue
            try:
                if check(text):
                    return True
            except Exception as error:
                log.warning("everyday skill %s may_match failed: %s", module.__name__, error)
        return False

    async def handle(self, text, ctx):
        for module in self.modules():
            try:
                reply = module.handle(text, ctx)
                if inspect.isawaitable(reply):
                    reply = await reply
            except Exception as error:
                log.warning("everyday skill %s failed: %s", module.__name__, error)
                continue
            if reply is not None:
                return reply
        return None


_DEFAULT = Registry()


def preload():
    """Import every skill now (at start-up, not on the first utterance)."""
    return _DEFAULT.modules()


def may_match(text):
    return _DEFAULT.may_match(text)


async def handle(text, ctx):
    return await _DEFAULT.handle(text, ctx)
