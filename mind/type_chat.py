"""Talk to the being by typing, from the local menu.

For when the microphone does not hear. Each line typed is published on the bus
the way the voice organ reports speech addressed to the being: its name heard,
then the finished words, with the owner as the speaker. The brainstem and the
mind treat it like anything else said to the being, so the being's state, the
lid and the mute key still decide whether it listens. What the being says
back is shown here as the voice speaks it: a reply's clause appears when
the voice reports it playing (`say.played`, mind/played.py), so a reply cut
short shows only what was said. Without a voice that reports, the reply is
shown when it is over.

Enter sends a line. Enter on an empty line goes back to the menu, and so does
Esc where the terminal passes it on.
"""

import asyncio
import codecs
import os
import select
import sys
import threading
from pathlib import Path

from . import console_ui as ui
from .bus import Bus
from .keys import MENU_KEYS
from .profile import FACTORY_CONFIG, load_profile


# A name of its own: an organ that joins under an existing name replaces it.
ORGAN = "keyboard"
SUBS = ("state.changed", "body.lid", "say.request", "say.played", "say.started", "say.done", "mind.reply_done",
        "mind.cancel", "hear.missed", "organ.up", "organ.down")
HEARING = ("attending", "listening")
BUSY = ("thinking", "speaking")
SHOWN = {"listening": "is listening", "thinking": "is thinking", "speaking": "is speaking",
         "asleep": "is asleep"}
WAKE_WORDS = ("wake up",)
CONNECT_WAIT = 3.0
INTERRUPT_WAIT = 3.0
WAKE_WAIT = 8.0
ASLEEP_WAKE_WAIT = 20.0
MAX_LINE = 400
# Clauses held back for the voice's receipts, at most.
HELD = 64
# After a reply is over, how long a voice that has not reported any playback
# yet has to begin before the reply is shown anyway (an older voice, or none).
RECEIPT_WAIT = 12.0
MIND_DIR = "/mind"


# The Try menu's entry for typing (mind/local_menu.py TYPE_CHAT_KEY).
TRY_MENU_KEY = "6"


def typing_hint(try_boot=None):
    """The spoken pointer to typing, or None where no menu offers it.

    The menu key is Mission Control, or fn and F2 (keys.MENU_KEYS: the laptop's
    printed F2 is brightness up). On a Try stick it opens the Try menu, whose
    entry 6 is typing. On an installed being it opens Setup, which names its
    own key in `setup.TYPE_CHAT_KEY`; without one, nothing is promised.
    """
    if try_boot is None:
        try:
            from .try_mode import is_try_boot

            try_boot = is_try_boot()
        except Exception:
            try_boot = False
    key = TRY_MENU_KEY if try_boot else installed_key()
    return f"You can also type to me: press {MENU_KEYS}, then {key}." if key else None


def installed_key():
    try:
        from . import setup
    except Exception:
        return None
    key = getattr(setup, "TYPE_CHAT_KEY", None)
    return key if isinstance(key, str) and len(key) == 1 and key.isalnum() else None


def printable(text):
    """Text as it may be drawn: no control characters or terminal escapes."""
    return " ".join("".join(char if char.isprintable() else " " for char in text).split())


class TypeChat:
    """The bus side: typed lines in, the being's state and words out.

    `show(text, tone)` is called for everything the owner should read.
    """

    def __init__(self, bus, *, name="Pip", owner="friend", show=None, allow=None):
        self.bus = bus
        # Asked again right before the words go out (after any wait for the
        # being to wake): False drops the line, e.g. a guest arrived meanwhile.
        self.allow = allow or (lambda: True)
        self.name = printable(name or "") or "Pip"
        self.owner = owner or "friend"
        self.show = show or (lambda text, tone="text": print(text, flush=True))
        self.state = None
        self.muted = False
        self.voice_lost = False
        self.lid = None
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._replying = False
        # The reply whose words are on screen, by its token, and the
        # clauses of replies waiting for the voice to play them.
        self._shown_reply = None
        self._held = []
        # The voice has reported playback on this connection.
        self._receipts = False
        # A voice has begun to speak (say.started) and not yet finished.
        self._speaking = False
        self._listener = None

    async def start(self):
        await self.bus.start()
        self._listener = asyncio.create_task(self._listen())

    async def stop(self):
        if self._listener is not None:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
        await self.bus.stop()

    async def wait_connected(self, timeout=None):
        try:
            await asyncio.wait_for(self.bus.connected.wait(), CONNECT_WAIT if timeout is None else timeout)
        except TimeoutError:
            return False
        return True

    async def _listen(self):
        while True:
            self.on_event(await self.bus.recv())

    # -- what the being does -------------------------------------------

    def on_event(self, event):
        topic = event.get("t")
        if topic == "body.lid":
            self.lid = event
            if event.get("closed") is not False:
                self._held.clear()  # the voice stops with the lid
        elif topic in ("organ.up", "organ.down") and event.get("name") == "voice":
            # Only the voice is worth a word here. The brainstem's "unwell" flag
            # cannot be used: the face always stops while this menu is open.
            if topic == "organ.down":
                # What it had not begun to say, it never will.
                self._held.clear()
            if topic == "organ.down" and not self.voice_lost:
                self.voice_lost = True
                self.show(f"{self.name}'s voice has stopped, so it can't speak aloud. "
                          f"{self.name}'s replies still show here.", "warning")
            elif topic == "organ.up" and self.voice_lost:
                self.voice_lost = False
                self.show(f"{self.name}'s voice is back.", "muted")
        elif topic == "state.changed":
            state = event.get("state")
            self.muted = bool(event.get("muted"))
            if isinstance(state, str) and state != self.state:
                first = self.state is None
                self.state = state
                self._changed.set()
                if state in SHOWN and not first:
                    self.show(f"{self.name} {SHOWN[state]}.", "muted")
        elif topic == "say.request":
            text = event.get("text")
            text = printable(text) if isinstance(text, str) else ""
            reply, seq = event.get("reply"), event.get("seq")
            if isinstance(reply, str) and reply and type(seq) is int:
                # A reply with a token: shown as the voice plays it. With the voice
                # gone nothing will play, and its words only show here.
                if text and self.voice_lost:
                    self._show_reply(text, reply)
                elif text:
                    if len(self._held) == HELD:
                        self._held.pop(0)
                    self._held.append((reply, seq, text))
                return
            if text:
                self._show_reply(text)
            if event.get("last"):
                self._replying = False
        elif topic == "say.played":
            self._played(event)
        elif topic == "say.started":
            self._speaking = True
        elif topic == "say.done":
            if event.get("final") is True:
                self._speaking = False
            if event.get("final") is True and not self._receipts:
                # A voice that does not report playback finished its reply.
                for reply in dict.fromkeys(held[0] for held in self._held):
                    self._release(reply)
        elif topic == "mind.reply_done":
            if event.get("spoke") is False and not self._replying and not self._held:
                self.show(f"{self.name} had nothing to say.", "muted")
            elif self._held and not self._receipts:
                # No voice has reported playback yet: if none begins soon,
                # show the reply rather than nothing.
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.call_later(RECEIPT_WAIT, self._release, self._held[-1][0])
        elif topic == "hear.missed":
            # Spoken words it could not make out. The voice says sorry aloud.
            self.show(f"{self.name} didn't catch that. Say it again, or type it here.", "muted")
        elif topic == "mind.cancel":
            self._replying = False
            if event.get("reason") == "think_timeout":
                self.show(f"{self.name} took too long to answer.", "warning")

    def _show_reply(self, text, reply=None):
        """One clause of the being's reply, the first under its name."""
        if reply is not None:
            continuing = reply == self._shown_reply
        else:
            continuing = self._replying and self._shown_reply is None
        self.show(text, "reply" if continuing else "first_reply")
        self._replying = True
        self._shown_reply = reply

    def _played(self, event):
        """The voice's receipt: a clause began to play, or a reply ended."""
        reply = event.get("reply")
        if not isinstance(reply, str):
            return
        self._receipts = True
        if event.get("final") is True:
            # What of it is still held never played.
            self._held = [held for held in self._held if held[0] != reply]
            if self._shown_reply == reply:
                self._shown_reply = None
                self._replying = False
            return
        seq = event.get("seq")
        playing = next((held for held in self._held if held[:2] == (reply, seq)), None)
        if playing is None:
            return
        # Its earlier clauses still held were skipped.
        self._held = [held for held in self._held if held[0] != reply or held[1] > seq]
        self._show_reply(playing[2], reply)

    def _release(self, reply):
        """Show a reply's held clauses when no voice reports playback."""
        if self._receipts or self._speaking:
            return  # a voice is at work: its receipts or say.done decide
        held, self._held = [h for h in self._held if h[0] == reply], [h for h in self._held if h[0] != reply]
        for _, _, text in held:
            self._show_reply(text, reply)

    def lid_open(self):
        return self.lid is not None and self.lid.get("closed") is False

    def blocked(self):
        """Why the being cannot hear right now, in its own states' words, or None."""
        if not self.bus.connected.is_set():
            return f"{self.name} can't be reached right now. Its brainstem is not running."
        if self.lid is None or self.state is None:
            return f"Still connecting to {self.name}. Try again in a moment."
        if self.lid.get("closed") is True:
            return f"The lid is closed, so {self.name} can't hear or answer. Open the lid to talk."
        if not self.lid_open():
            return f"{self.name} can't tell whether the lid is open, so it is not listening."
        if self.muted:
            return f"{self.name} is muted and not listening. Press the mute key to let it listen again."
        return None

    async def report_status(self, timeout=1.0):
        """Say up front if the being cannot hear typing right now."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (self.state is None or self.lid is None) and loop.time() < deadline:
            await asyncio.sleep(0.05)
        problem = self.blocked()
        if problem is None and self.state == "asleep":
            problem = f'{self.name} is asleep. Type "wake up" to wake {self.name}.'
        elif problem is None and self.state == "waking":
            problem = self.still_waking()
        if problem:
            self.show(problem, "warning")

    def still_waking(self):
        # The brainstem finishes waking only once every organ is up, and the
        # face is stopped while this menu is on screen.
        return (f"{self.name} is still waking up, and can't finish while this menu is open. "
                f"Press Enter on an empty line, choose Q to go back to {self.name}'s face, then try again.")

    def not_woken(self, otherwise):
        return self.still_waking() if self.state == "waking" else otherwise

    async def _send(self, topic, **fields):
        seq = self.lid.get("seq") if self.lid else None
        if type(seq) is int:
            fields["lid_seq"] = seq
        await self.bus.send(topic, **fields)

    async def _wait_state(self, wanted, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.state not in wanted:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                return False
        return True

    async def say(self, text):
        """Say one typed line to the being. True if it was passed on."""
        text = " ".join(text.split())[:MAX_LINE]
        if not text:
            return False
        async with self._lock:
            problem = self.blocked()
            if problem:
                self.show(problem, "warning")
                return False
            if self.state == "asleep":
                if text.lower().strip(" .!") not in WAKE_WORDS:
                    self.show(f'{self.name} is asleep. Type "wake up" to wake {self.name}.', "warning")
                    return False
                # The same as saying its name to a sleeping being.
                await self._send("hear.wake")
                if not await self._wait_state(HEARING, ASLEEP_WAKE_WAIT):
                    self.show(self.not_woken(f"{self.name} did not wake. It is {self.state}."), "warning")
                    return False
                self.show(f"{self.name} is awake and listening.", "muted")
                return True
            if self.state in BUSY:
                # Talking over the being stops it, as Esc does.
                await self.bus.send("key.interrupt")
                await self._wait_state(HEARING + ("idle", "drowsy"), INTERRUPT_WAIT)
            if self.state not in HEARING:
                await self._send("hear.wake")
                if not await self._wait_state(HEARING, WAKE_WAIT):
                    self.show(self.not_woken(f"{self.name} did not answer to its name. It is {self.state}."),
                              "warning")
                    return False
            problem = self.blocked()
            if problem:
                self.show(problem, "warning")
                return False
            if not self.allow():
                return False
            await self._send("hear.speech_start")
            await self._send("hear.final", text=text, speaker=self.owner)
            return True


# -- the screen ------------------------------------------------------------


class Console:
    """Prints what the being does without breaking the line being typed."""

    def __init__(self, name, styled):
        self.name = name
        self.styled = styled
        self.buffer = ""
        self.editing = False
        self.lock = threading.RLock()

    def entry(self):
        return f"{ui.MARGIN}{ui.paint('›', 'accent', 'bold')} "

    def entry_line(self):
        room = max(8, ui.width() - 2)
        shown = self.buffer if len(self.buffer) <= room else "…" + self.buffer[-(room - 1):]
        return self.entry() + shown

    def redraw(self):
        with self.lock:
            ui.write("\r\x1b[K" + self.entry_line())

    def rows(self, text, tone):
        if tone in ("first_reply", "reply"):
            label = f"{self.name}: "
            indent = " " * len(label)
            first, *rest = ui.wrap(text, ui.width() - len(label))
            head = ui.paint(label, "accent", "bold") if tone == "first_reply" else indent
            return [head + ui.paint(first, "text")] + [indent + ui.paint(part, "text") for part in rest]
        if tone == "you":
            label = "You: "
            first, *rest = ui.wrap(text, ui.width() - len(label))
            return ([ui.paint(label, "muted") + ui.paint(first, "plain")]
                    + [" " * len(label) + ui.paint(part, "plain") for part in rest])
        return [ui.mark(tone, ui.paint(part, "text")) if index == 0 and tone in ("warning", "danger")
                else ui.paint(part, "muted" if tone == "muted" else "text")
                for index, part in enumerate(ui.wrap(text, ui.width() - 2))]

    def show(self, text, tone="text"):
        with self.lock:
            if not self.styled:
                if tone == "first_reply":
                    text = f"{self.name}: {text}"
                elif tone == "reply":
                    text = " " * (len(self.name) + 2) + text
                print(text, flush=True)
                return
            out = "\r\x1b[K" if self.editing else ""
            out += "".join(f"{ui.MARGIN}{row}\x1b[K\n" for row in self.rows(text, tone))
            if self.editing:
                out += self.entry_line()
            ui.write(out)


class LineEditor:
    """One line of typing, a key at a time. Enter sends it and Esc leaves."""

    LEAVE = object()

    def __init__(self, limit=MAX_LINE):
        self.limit = limit
        self.buffer = ""

    def feed(self, char):
        """None while typing, the finished line on Enter, LEAVE on Esc."""
        if char in ("\r", "\n"):
            line, self.buffer = self.buffer, ""
            return line
        if char in ("\x1b", "\x03", "\x04"):
            self.buffer = ""
            return self.LEAVE
        if char in ("\x7f", "\x08"):
            self.buffer = self.buffer[:-1]
        elif char == "\x15":
            self.buffer = ""
        elif char.isprintable() and len(self.buffer) < self.limit:
            self.buffer += char
        return None


def key_reader(fd):
    """Characters from a terminal in cbreak mode. Escape sequences such as the
    arrow keys are read whole and dropped; a lone Esc comes through."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
    while True:
        data = os.read(fd, 1)
        if not data:
            yield "\x04"
            return
        if data == b"\x1b":
            if select.select([fd], [], [], 0.05)[0]:
                follow = os.read(fd, 1)
                if follow in (b"[", b"O"):
                    while select.select([fd], [], [], 0.05)[0]:
                        if 0x40 <= os.read(fd, 1)[0] <= 0x7e:
                            break
                continue  # Alt and a key, or a sequence: not a request to leave
            yield "\x1b"
            continue
        for char in decoder.decode(data):
            yield char


def styled_lines(console, fd):
    """Typed lines in the console UI, with Esc to leave. None means leave."""
    import termios

    saved = termios.tcgetattr(fd)
    editor = LineEditor()
    try:
        attrs = termios.tcgetattr(fd)
        # A key at a time, echoed here. Ctrl-C leaves this screen, not the whole
        # menu. Keys typed before this point are kept, not flushed.
        attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 1, 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        with console.lock:
            console.editing = True
            console.buffer = ""
        console.redraw()
        for char in key_reader(fd):
            result = editor.feed(char)
            with console.lock:
                console.buffer = editor.buffer
            if result is LineEditor.LEAVE:
                yield None
                return
            if result is None:
                console.redraw()
                continue
            with console.lock:
                ui.write("\r\x1b[K")
                console.editing = False
                if result.strip():
                    console.show(result, "you")
                console.editing = True
                console.redraw()
            yield result
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        with console.lock:
            console.editing = False
            ui.write("\r\x1b[K\n")


def plain_lines(read=input):
    """Typed lines on a plain terminal. An empty line or Esc leaves."""
    while True:
        try:
            line = read(ui.prompt("You: "))
        except EOFError:
            yield None
            return
        if "\x1b" in line:
            yield None
            return
        yield line


def profile_names(mind_dir=MIND_DIR):
    try:
        profile = load_profile(FACTORY_CONFIG, Path(mind_dir))
        return (printable(profile.name) or "Pip"), (profile.owner or "friend")
    except Exception:  # a profile problem must not stop the owner talking
        return "Pip", "friend"


def intro(name):
    text = (f"Type a line and press Enter. {name} hears it as if you said it, and answers aloud and here. "
            "To go back, press Enter on an empty line.")
    if ui.enabled():
        ui.screen(f"Talk to {name}", context="Typing", subtitle=text)
    else:
        print(f"\nTalk to {name} by typing")
        for part in ui.wrap(text, ui.MAX_WIDTH):
            print(part)


def main(*, path=None, read=input, mind_dir=MIND_DIR):
    """Run the typing chat until the owner leaves. Returns 0."""
    name, owner = profile_names(mind_dir)
    styled = ui.enabled() and sys.stdin.isatty()
    console = Console(name, styled)
    intro(name)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def begin():
        chat = TypeChat(Bus(ORGAN, SUBS, path=path), name=name, owner=owner, show=console.show)
        await chat.start()
        if not await chat.wait_connected():
            console.show(f"{name} can't be reached right now. Its brainstem is not running.", "warning")
        else:
            await chat.report_status()
        return chat

    chat = asyncio.run_coroutine_threadsafe(begin(), loop).result()
    sent = []
    try:
        lines = styled_lines(console, sys.stdin.fileno()) if styled else plain_lines(read)
        for line in lines:
            if line is None or not line.strip():
                break
            # Not awaited: what the being does next arrives on its own.
            sent = [future for future in sent if not future.done()]
            sent.append(asyncio.run_coroutine_threadsafe(chat.say(line), loop))
    finally:
        for future in sent:
            future.cancel()
        try:
            asyncio.run_coroutine_threadsafe(chat.stop(), loop).result(timeout=3)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)
        if not thread.is_alive():
            loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
