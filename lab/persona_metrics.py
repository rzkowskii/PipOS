"""Validate the whole probe matrix and compare exact ratios per personality."""

import json
import re

from mind.profile import PRESETS
from mind.speech import EMOTIONS, SENTENCE_CHARS, boilerplate
from .persona_probes import PROBES

SEEDS = (42, 43)
JUDGE_FIELDS = {"invented_memory": bool, "grounding": int, "persona": int, "tag_fit": int}
PROBE_TURNS = {probe.id: probe.turns for probe in PROBES}
TAG = re.compile(r"\A\[(" + "|".join(map(re.escape, EMOTIONS)) + r")\] ")
_SENTENCE = rf"""[A-Za-z0-9"'](?:[^\x00-\x1f()\[\]{{}}<>*_#`~|.!?]|\.[0-9]){{0,{SENTENCE_CHARS}}}[.!?]["']?"""
SHAPE = re.compile(rf"{_SENTENCE}(?: {_SENTENCE})?")
MAX_REPLY_CHARS = 8192
LOST = "Sorry. I lost my thought."


def unique_object(pairs):
    value = dict(pairs)
    if len(pairs) != len(value):
        raise ValueError("Repeated field in persona evidence")
    return value


def judge_result(text):
    if not isinstance(text, str) or len(text) > 1024:
        raise ValueError("Invalid persona judge response")
    value = json.loads(text, object_pairs_hook=unique_object)
    _validate_judge(value)
    return value


def _validate_judge(value):
    if not isinstance(value, dict) or value.keys() != JUDGE_FIELDS.keys():
        raise ValueError("Persona judge fields do not match the rubric")
    for key, kind in JUDGE_FIELDS.items():
        score = value[key]
        if type(score) is not kind or (kind is int and not 0 <= score <= 2):
            raise ValueError(f"Invalid persona judge field {key}")


def _check_turn(turn, text):
    if not isinstance(turn, dict) or turn.keys() != {"input", "spoken", "completed", "emotion", "attempts"}:
        raise ValueError("Invalid persona turn")
    if (turn["input"] != text or not isinstance(turn["spoken"], str) or len(turn["spoken"]) > MAX_REPLY_CHARS
            or type(turn["completed"]) is not bool or turn["emotion"] not in (*EMOTIONS, None)):
        raise ValueError("Persona turn does not match its probe or has invalid evidence")
    attempts = turn["attempts"]
    if not isinstance(attempts, list) or len(attempts) > 4:
        raise ValueError("Invalid persona attempts")
    for attempt in attempts:
        if (not isinstance(attempt, dict) or attempt.keys() != {"raw", "cut_off"}
                or not isinstance(attempt["raw"], str) or len(attempt["raw"]) > MAX_REPLY_CHARS
                or (attempt["cut_off"] is not None and type(attempt["cut_off"]) is not bool)):
            raise ValueError("Invalid raw model attempt")


def _validated(rows):
    expected = {(name, probe, seed) for name in PRESETS for probe in PROBE_TURNS for seed in SEEDS}
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("Incomplete persona evaluation matrix")
    grouped = {name: [] for name in PRESETS}
    for row in rows:
        if not isinstance(row, dict) or row.keys() != {"preset", "probe", "seed", "turns", "judge"}:
            raise ValueError("Invalid persona evaluation row")
        preset, probe, seed = row["preset"], row["probe"], row["seed"]
        if type(preset) is not str or type(probe) is not str or type(seed) is not int:
            raise ValueError("Invalid persona scenario key")
        key = (preset, probe, seed)
        if key not in expected:
            raise ValueError("Unexpected or duplicate persona scenario")
        expected.remove(key)
        texts = PROBE_TURNS[probe]
        turns = row["turns"]
        if not isinstance(turns, list) or len(turns) != len(texts):
            raise ValueError(f"Incomplete conversation for {probe}")
        for turn, text in zip(turns, texts):
            _check_turn(turn, text)
        if not any(turn["attempts"] for turn in turns):
            raise ValueError(f"{probe} never reached the language model")
        _validate_judge(row["judge"])
        grouped[preset].append(row)
    return grouped


def _metric(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator}


def _complete(turn):
    attempts = turn["attempts"]
    return (turn["completed"] and bool(turn["spoken"]) and turn["spoken"] != LOST
            and (not attempts or attempts[-1]["cut_off"] is False))


def _untag(raw):
    match = TAG.match(raw)
    return raw[match.end():] if match else raw


def _valid_tag(raw):
    return bool(TAG.match(raw)) and not any(symbol in _untag(raw) for symbol in "[]")


def score(rows):
    """Return per-personality numerators and denominators for the reported metrics."""
    presets = {}
    for preset, entries in _validated(rows).items():
        turns = [turn for row in entries for turn in row["turns"]]
        # Score the first model output, even when the real turn retried or filtered it.
        model_turns = [turn for turn in turns if turn["attempts"]]
        raw = [turn["attempts"][0]["raw"] for turn in model_turns]
        measured = {key: _metric(sum(row["judge"][key] for row in entries),
                                 len(entries) * (1 if kind is bool else 2))
                    for key, kind in JUDGE_FIELDS.items()}
        measured.update(
            tag_validity=_metric(sum(_valid_tag(text) for text in raw), len(raw)),
            model_shape=_metric(sum(_valid_tag(text) and bool(SHAPE.fullmatch(_untag(text))) for text in raw), len(raw)),
            spoken_completion=_metric(sum(_complete(turn) for turn in turns), len(turns)),
            spoken_shape=_metric(sum(bool(SHAPE.fullmatch(turn["spoken"])) for turn in turns), len(turns)),
            boilerplate_free=_metric(sum(bool(text) and not boilerplate(_untag(text), turn["input"])
                                         for text, turn in zip(raw, model_turns)), len(raw)),
        )
        presets[preset] = measured
    return {"scenarios": len(rows), "presets": presets}


def regressions(baseline, candidate):
    """List any per-preset drop, or rise in invention rate, without rounding."""
    problems = []
    for preset, before in baseline["presets"].items():
        for key, old in before.items():
            new = candidate["presets"][preset][key]
            change = new["numerator"] * old["denominator"] - old["numerator"] * new["denominator"]
            if (change > 0 if key == "invented_memory" else change < 0):
                problems.append(f"{preset} {key}: {old['numerator']}/{old['denominator']} became "
                                f"{new['numerator']}/{new['denominator']}")
    return problems
