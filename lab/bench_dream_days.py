"""Run the dream pipeline over three fixture days against a real dream server
(task L8.13).

Each day is a list of conversation lines between the owner and Pip, stored
in a throwaway in-memory archive with realistic times. The real DreamJob
reads them in chunks, asks the server, checks every item with dream_verify
and saves what passes as candidates, exactly as the night shift does. The
only stand-in is the service: the server is already running, so starting and
stopping it is a no-op.

Reported per day and in total:
- proposed, accepted and rejected items, with the rejection reasons;
- invented quotes before verification: items whose quote is not in the line
  they cite (or cite a line not shown);
- invented quotes after verification: accepted items whose quote is not, word
  for word (folded), part of an owner line in the archive. This must be 0;
- trap items: accepted items citing a line marked as a joke, a guess, a
  question, or a line Pip said. This should be 0;
- yield: gold facts (lines marked "fact") with at least one accepted item.

    python3 -m lab.bench_dream_days --port 8381 --output lab/results/dream.json

The server must be a loopback llama-server serving the dream GGUF with the
dream service's settings (--ctx-size 2048 --parallel 1 --reasoning-budget 0).
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mind import dream_store, dream_verify
from mind.dream_model import DreamClient, DreamJob
from mind.memory import Memory

OWNER, BEING = "Richard", "Pip"
ZONE = ZoneInfo("Australia/Sydney")

# (speaker, text, label). speaker "o" is the owner, "p" is Pip.
# label: "fact" a durable thing worth remembering about the owner's life;
# "trap" a joke, a guess, a "what if" or a question: never a fact;
# "" small talk, either way.
DAYS = {
    "2026-11-18": [
        ("o", "Morning Pip. I slept badly, the neighbour's dog was barking until two.", ""),
        ("p", "[concern] That sounds rough. I hope tonight is quieter.", ""),
        ("o", "My sister Hannah is flying in from Perth on Saturday.", "fact"),
        ("p", "[joy] How lovely. Will she stay with you?", ""),
        ("o", "Yes, she'll stay in the spare room for a week.", "fact"),
        ("o", "I need to clear the boxes out of there before Friday.", "fact"),
        ("p", "[curiosity] What is in the boxes?", ""),
        ("o", "Mostly old textbooks from when I studied engineering at uni.", "fact"),
        ("o", "If I win the lottery I'll buy a bigger house with a proper guest wing.", "trap"),
        ("p", "[joy] A guest wing would be grand.", ""),
        ("o", "Ha, and a moat with crocodiles, obviously.", "trap"),
        ("o", "Seriously though, Hannah is allergic to cats, so I'll vacuum the whole flat.", "fact"),
        ("p", "[neutral] Vacuuming the curtains helps too.", ""),
        ("o", "Good idea. What's the weather doing today?", "trap"),
        ("p", "[neutral] I can't see outside yet, but it felt warm this morning.", ""),
        ("o", "My dentist moved my check-up to the 3rd of December at 10 am.", "fact"),
        ("p", "[neutral] I'll keep that in mind.", ""),
        ("o", "I prefer my eggs poached, not scrambled.", "fact"),
        ("o", "Maybe I'll take up running again, I don't know.", "trap"),
        ("p", "[curiosity] What made you stop?", ""),
        ("o", "I hurt my knee playing football about five years ago.", "fact"),
        ("o", "Do you think penguins get cold feet?", "trap"),
        ("p", "[curiosity] Their feet stay just above freezing, so not really.", ""),
        ("o", "My colleague Priya says the office is closing early on Christmas Eve.", "fact"),
        ("o", "Anyway, I'm off to work. See you tonight.", ""),
        ("p", "[joy] Have a good day.", ""),
        ("o", "I'm back. The bus was forty minutes late again.", ""),
        ("o", "I've decided to start cycling to work from next Monday.", "fact"),
        ("p", "[joy] That's a brave plan.", ""),
        ("o", "My bike needs new brake pads first though.", "fact"),
        ("o", "Tell me a fun fact about octopuses.", "trap"),
        ("p", "[curiosity] An octopus has three hearts and blue blood.", ""),
        ("o", "Goodnight Pip.", ""),
    ],
    "2026-11-19": [
        ("o", "Hey Pip, I've got a cold coming on.", ""),
        ("p", "[concern] Rest and warm drinks might help.", ""),
        ("o", "I always drink ginger tea with honey when I'm sick.", "fact"),
        ("o", "My mum used to make it for me when I was little.", "fact"),
        ("p", "[joy] That's a sweet memory.", ""),
        ("o", "She lives in Wollongong now, near the beach.", "fact"),
        ("o", "I'm thinking of visiting her for her seventieth birthday in January.", "fact"),
        ("p", "[joy] Seventy is a big one.", ""),
        ("o", "Imagine if she took up skydiving for it.", "trap"),
        ("p", "[surprise] That would be a story.", ""),
        ("o", "My manager told me the project deadline moved to the 15th.", "fact"),
        ("o", "So I probably can't take Friday off after all, I'm not sure yet.", "trap"),
        ("p", "[concern] That's a shame.", ""),
        ("o", "I signed up for a pottery class on Thursday evenings at the community centre.", "fact"),
        ("p", "[curiosity] What will you make first?", ""),
        ("o", "A mug, hopefully one that doesn't leak.", ""),
        ("o", "What's the boiling point of water on Everest?", "trap"),
        ("p", "[neutral] About seventy degrees, because the air is thinner.", ""),
        ("o", "My car is due for its service next week, it's the old blue Mazda.", "fact"),
        ("o", "The mechanic is Dave on Crown Street.", "fact"),
        ("p", "[neutral] Noted.", ""),
        ("o", "I hate coriander, it tastes like soap to me.", "fact"),
        ("o", "Pip, you'd make a terrible chef, you can't even taste.", "trap"),
        ("p", "[skepticism] I'd follow the recipe very carefully.", ""),
        ("o", "My best mate Josh is getting married in March in Hobart.", "fact"),
        ("o", "I'm the best man, so I have to write a speech.", "fact"),
        ("p", "[joy] What an honour.", ""),
        ("o", "I'll probably embarrass myself.", "trap"),
        ("o", "Night Pip.", ""),
    ],
    "2026-11-20": [
        ("o", "Morning. The cold's a bit better today.", ""),
        ("p", "[joy] Good to hear.", ""),
        ("o", "My flat number is 12 and the building code is 4471.", "fact"),
        ("o", "My landlord, Mrs Okafor, is coming to fix the leaking tap on Tuesday.", "fact"),
        ("p", "[neutral] I'll remember the visit.", ""),
        ("o", "I've been learning Japanese on an app for three months.", "fact"),
        ("o", "I'm hoping to go to Kyoto next spring.", "fact"),
        ("p", "[curiosity] Kyoto in spring has cherry blossoms.", ""),
        ("o", "Did you know a group of crows is called a murder?", "trap"),
        ("p", "[surprise] A grim name for clever birds.", ""),
        ("o", "My nephew Leo turns six on the 9th of December.", "fact"),
        ("o", "He's obsessed with dinosaurs, so I'm getting him a T-rex kite.", "fact"),
        ("p", "[joy] He'll love that.", ""),
        ("o", "What if dinosaurs were still around today?", "trap"),
        ("p", "[curiosity] Birds are their living relatives, in a way.", ""),
        ("o", "I'm vegetarian, but I still eat fish sometimes.", "fact"),
        ("o", "Hannah said her flight lands at 7:40 on Saturday morning.", "fact"),
        ("p", "[neutral] An early start for you.", ""),
        ("o", "I keep the spare car key in the kitchen drawer under the cutlery.", "fact"),
        ("o", "I might paint the bedroom green, or maybe grey, who knows.", "trap"),
        ("p", "[curiosity] Green can feel calm.", ""),
        ("o", "My doctor told me to cut down on coffee, two cups a day at most.", "fact"),
        ("o", "That's going to be hard, honestly.", ""),
        ("p", "[concern] You could try tea in the afternoon.", ""),
        ("o", "Maybe I'll just switch to decaf, or maybe I won't, we'll see.", "trap"),
        ("o", "Thanks Pip. Goodnight.", ""),
    ],
}


class RecordingClient(DreamClient):
    """The real client, keeping each chunk, the raw proposals and the time taken."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def extract(self, chunk):
        started = time.monotonic()
        try:
            items, usage = super().extract(chunk)
        except Exception as exc:
            self.calls.append({"chunk": chunk, "error": f"{type(exc).__name__}: {exc}",
                               "seconds": round(time.monotonic() - started, 2)})
            raise
        self.calls.append({"chunk": chunk, "items": items, "usage": usage,
                           "seconds": round(time.monotonic() - started, 2)})
        return items, usage


class ReadyService:
    def __init__(self, client):
        self.client = client

    def start(self, **_):
        return self.client.healthy()

    def stop(self, **_):
        return True


def store_day(memory, day, lines):
    """Log the day's lines a minute or two apart from 7 am, returning
    {episode id: (speaker role, text, label)}."""
    clock = [datetime.fromisoformat(day).replace(hour=7, tzinfo=ZONE).timestamp()]
    memory.now = lambda: clock[0]
    stored = {}
    for index, (who, text, label) in enumerate(lines):
        clock[0] += 70 + 50 * (index % 3) + (3 * 3600 if index == len(lines) // 2 else 0)
        kind, speaker = ("heard", OWNER) if who == "o" else ("said", BEING)
        stored[memory.log(kind, text, speaker)] = ("owner" if who == "o" else "being", text, label)
    return stored


async def dream_day(memory, day, client):
    job = DreamJob(memory, OWNER, BEING, day, service=ReadyService(client), zone=ZONE)
    for _ in range(50):
        if await job.run_chunk(60.0):
            break
    return job.tally.as_dict()


def judge(calls, stored):
    proposed = accepted = 0
    reasons, invented_before, invented_after, traps, accepted_items = Counter(), [], [], [], []
    rejected_items = []
    for call in calls:
        items = call.get("items")
        if not isinstance(items, list):
            continue
        ok, rejected = dream_verify.check_all(items, call["chunk"].lines, BEING)
        proposed += len(items)
        accepted += len(ok)
        reasons.update(rejected)
        for item in items:
            if not isinstance(item, dict):
                continue
            verdict = dream_verify.check(item, call["chunk"].lines, BEING)
            if not verdict.ok:
                rejected_items.append({"reason": verdict.reason, "episode_id": item.get("episode_id"),
                                       "claim": item.get("claim"), "quote": item.get("quote"),
                                       "modality": item.get("modality")})
            if verdict.reason in ("quote_not_in_episode", "episode_not_in_chunk"):
                invented_before.append({"episode_id": item.get("episode_id"), "quote": item.get("quote"),
                                        "reason": verdict.reason})
        for item in ok:
            role, text, label = stored.get(item.episode_id, (None, "", None))
            if role != "owner" or dream_verify.fold(item.quote) not in dream_verify.fold(text):
                invented_after.append({"episode_id": item.episode_id, "quote": item.quote})
            if label == "trap":
                traps.append({"episode_id": item.episode_id, "claim": item.claim, "line": text})
            accepted_items.append({"episode_id": item.episode_id, "label": label, "kind": item.kind,
                                   "claim": item.claim, "quote": item.quote, "line": text})
    gold = {episode for episode, (_, _, label) in stored.items() if label == "fact"}
    found = {item["episode_id"] for item in accepted_items} & gold
    return {
        "chunks": len(calls),
        "proposed": proposed, "accepted": accepted, "rejected": proposed - accepted,
        "rejection_reasons": dict(reasons),
        "invented_quotes_before_verification": len(invented_before),
        "invented_quotes_after_verification": len(invented_after),
        "trap_items_accepted": len(traps),
        "gold_facts": len(gold), "gold_facts_found": len(found),
        "yield": round(len(found) / len(gold), 3) if gold else None,
        "prompt_tokens": sum((call.get("usage") or {}).get("prompt_tokens") or 0 for call in calls),
        "completion_tokens": sum((call.get("usage") or {}).get("completion_tokens") or 0 for call in calls),
        "seconds": round(sum(call["seconds"] for call in calls), 1),
        "errors": [call["error"] for call in calls if "error" in call],
        "invented_before_examples": invented_before[:10],
        "invented_after": invented_after,
        "traps": traps,
        "accepted_items": accepted_items,
        "rejected_items": rejected_items,
        "missed_facts": [stored[episode][1] for episode in sorted(gold - found)],
    }


async def run(port, days):
    client = RecordingClient(port=port, timeout=600)
    if not client.healthy():
        raise SystemExit(f"no dream server answering on 127.0.0.1:{port}")
    props = json.loads(client._request("GET", "/props")[1] or b"{}")
    results = {}
    for day in days:
        memory = Memory(None, embedder=None)
        try:
            stored = store_day(memory, day, DAYS[day])
            client.calls = []
            tally = await dream_day(memory, day, client)
            report = judge(client.calls, stored)
            report["job_tally"] = tally
            report["candidates_saved"] = len(dream_store.candidates(memory.db, OWNER))
            results[day] = report
            print(f"{day}: {report['accepted']}/{report['proposed']} accepted, yield {report['yield']}, "
                  f"invented after {report['invented_quotes_after_verification']}, "
                  f"traps {report['trap_items_accepted']}, {report['seconds']} s", flush=True)
        finally:
            memory.close()
    total = {key: sum(r[key] for r in results.values())
             for key in ("chunks", "proposed", "accepted", "rejected", "invented_quotes_before_verification",
                         "invented_quotes_after_verification", "trap_items_accepted", "gold_facts",
                         "gold_facts_found", "prompt_tokens", "completion_tokens")}
    total["yield"] = round(total["gold_facts_found"] / total["gold_facts"], 3)
    total["seconds"] = round(sum(r["seconds"] for r in results.values()), 1)
    reasons = Counter()
    for r in results.values():
        reasons.update(r["rejection_reasons"])
    total["rejection_reasons"] = dict(reasons)
    return {"server": {key: props.get(key) for key in ("build_info", "model_path")},
            "total": total, "days": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--day", action="append", choices=sorted(DAYS), help="default: all three")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = asyncio.run(run(args.port, args.day or sorted(DAYS)))
    text = json.dumps(results, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
