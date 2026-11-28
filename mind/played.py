"""Which clauses of a model reply Voice really played.

The mind hands a reply to Voice clause by clause (`say.request`), faster than
Voice can speak it. Each clause carries this reply's token (`reply`) and its
position (`seq`). Voice sends `say.played` with both when a clause's first
audio plays, and ends every reply it accepted with one final `say.played`
(`final: true`, and `stopped: true` when it stopped before the reply's end).
Voice's messages reach the mind in the order Voice sent them, so the final
receipt comes after every clause receipt of that reply.

Only a clause Voice reported counts as said. When a reply is interrupted,
the conversation history and the archive keep the clauses that started
playing, in order, marked as cut off, and nothing that never started. A
clause cut off partway counts: the owner heard its start. A reply of which
nothing played is not kept as said at all. Receipts that arrive after the
mind has closed a reply are ignored. See docs/PROTOCOL.md, `say.played`.
"""

import asyncio
import re
import secrets


TOKEN = re.compile(r"[0-9a-f]{16}")
# How long an interrupted reply waits for Voice's final receipt. It normally
# arrives within milliseconds of say.stop; a voice that crashed never sends
# it, and then the receipts already received decide.
SETTLE_SECONDS = 1.0


class PlayedReply:
    """One model reply's hand-off to Voice and Voice's receipts for it."""

    def __init__(self):
        self.token = secrets.token_hex(8)
        self.clauses = {}  # seq -> clause text, as handed to Voice
        self.started = set()  # seqs Voice reported playing
        self.stopped = None  # None until Voice's final receipt
        self.settled = asyncio.Event()
        self.closed = False
        self.task = None  # the wait after a cancellation, if one began
        # Set once the whole reply was written down (turn.py _converse_stream).
        self.written = False
        self.message = None  # its assistant entry in the history
        self.question = None  # the user entry it answers
        self.memory = None  # the archive it was written to, and its row
        self.episode_id = None
        self.said = None  # the words written there
        self.emotion = "neutral"
        self.last_said_before = None  # turn.py's last_said before this reply
        self.last_said_set = None  # and what this reply set it to

    def hand(self, seq, text):
        """Record a clause as it is sent to Voice."""
        self.clauses[seq] = text

    def observe(self, event):
        """Take one `say.played`. True when it belongs to this open reply."""
        if self.closed or event.get("reply") != self.token:
            return False
        if event.get("final") is True:
            self.stopped = event.get("stopped") is True
            self.settled.set()
        else:
            seq = event.get("seq")
            if type(seq) is int and seq in self.clauses:
                self.started.add(seq)
        return True

    async def settle(self, timeout=None):
        """Wait for Voice's final receipt, for at most `timeout` seconds."""
        if not self.clauses or self.settled.is_set():
            return
        try:
            async with asyncio.timeout(SETTLE_SECONDS if timeout is None else timeout):
                await self.settled.wait()
        except TimeoutError:
            pass

    def kept(self):
        """The clauses that count as said, in the order they were handed."""
        return [self.clauses[seq] for seq in sorted(self.clauses) if seq in self.started]

    def complete(self):
        """Every clause handed to Voice played."""
        return len(self.started) == len(self.clauses)

    def close(self):
        self.closed = True
