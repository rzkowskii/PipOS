"""Keyboard Care page for the owner's saved phrase, time and event habits."""

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import console_ui as ui, habits, intents
from .care_routes import guest_active
from .try_mode import installed_trial_pending


def _safe(value, limit=75):
    text = "".join(character if character.isprintable() else "?" for character in str(value))
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _step_name(step):
    if isinstance(step, habits.AlarmStep) and step.available:
        return habits.alarm_phrase(step) + " (asks for yes each run)"
    if isinstance(step, habits.SleepStep):
        return "go to sleep (final step; asks for yes each run)"
    intent = intents.BY_ID.get(step) if isinstance(step, str) else None
    return intent.canonical if intent is not None and step in habits.STEP_IDS else f"Unavailable ({_safe(step, 24)})"


def _lines(habit):
    state = "On" if habit.enabled else "Off"
    lines = [f"Trigger: {_safe(habit.trigger)}", f"State: {state}", "Steps:"]
    lines.extend(f"  {index}. {_step_name(step)}" for index, step in enumerate(habit.steps, 1))
    return lines


def _show(title, habit, *, warning=None):
    ui.screen(title, context="Care")
    for line in _lines(habit):
        ui.line(line)
    if warning:
        ui.line()
        ui.line(ui.paint(warning, "warning"))


def _screen_receipt():
    """Pin the current open-lid owner screen across a typed confirmation.

    Developer terminals do not set BEING_SCREEN_DIR.  On the appliance the
    local-screen broker owns ``show`` and removes/replaces it on lid, guest or
    bus changes, so a stale receipt cannot authorize a write.
    """
    if guest_active():
        return False
    runtime = os.environ.get("BEING_SCREEN_DIR")
    if not runtime:
        return None
    path = Path(runtime) / "show"
    try:
        info = path.stat()
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return False
    return path, info.st_ino, value


def _receipt_current(receipt):
    if receipt is False or guest_active():
        return False
    if receipt is None:
        return True
    path, inode, value = receipt
    try:
        return path.stat().st_ino == inode and path.read_text(encoding="ascii").strip() == value
    except (OSError, UnicodeError):
        return False


def _confirm(mind, title, habit, challenge, warning):
    if installed_trial_pending(mind):
        ui.note("Habits stay unchanged until this installed system is confirmed.", "warning")
        return False
    receipt = _screen_receipt()
    if receipt is False:
        ui.note("Habits stay private while the lid is closed or a guest is here.", "warning")
        return False
    _show(title, habit, warning=warning)
    answer = input(ui.prompt(f"Type {challenge} to confirm, or Enter to cancel: ")).strip()
    if answer != challenge:
        ui.note("Nothing changed.", "muted")
        return False
    if not _receipt_current(receipt) or installed_trial_pending(mind):
        ui.note("The owner screen or system state changed. Nothing was saved.", "warning")
        return False
    return True


def _replace(store, mind, previous, revised, challenge, warning):
    from . import care

    if not _confirm(mind, "Review habit change", revised, challenge, warning):
        return None
    zone = care.zone_of(mind) if revised.trigger_kind == "time" else None
    if not store.replace(previous, revised, zone=zone):
        ui.note("That habit changed elsewhere. Reopen it and try again.", "warning")
        return None
    ui.note("Habit saved.", "success")
    return store.get(revised.trigger)


def _choose_step(prompt, mind):
    from . import care

    value = input(ui.prompt(prompt)).strip()
    if not value:
        return None
    zone = care.zone_of(mind)
    now = datetime.now().astimezone() if zone is None else datetime.now(zone)
    step = habits.recipe_step(value, "Pip", now)
    if step is None:
        ui.note("Choose a supported action, or a non-repeating alarm at one local clock time.", "warning")
    return step


def _edit_steps(store, mind, habit):
    from . import care

    entries = [("a", "Add a step"), ("r", "Replace a step"), ("d", "Delete a step")]
    choice = care.menu("Edit habit steps", entries, intro=_lines(habit))
    if choice is None:
        return habit
    steps = list(habit.steps)
    if choice == "a":
        if len(steps) >= habits.MAX_STEPS:
            ui.note("A habit has at most six steps.", "warning")
            return habit
        step = _choose_step("Action to add: ", mind)
        if step is None:
            return habit
        if any(isinstance(saved, habits.SleepStep) for saved in steps):
            ui.note("Go to sleep is already the final step.", "warning")
            return habit
        steps.append(step)
    else:
        value = input(ui.prompt("Step number: ")).strip()
        if value not in {str(number) for number in range(1, len(steps) + 1)}:
            ui.note("Choose one of the listed step numbers.", "warning")
            return habit
        index = int(value) - 1
        if choice == "r":
            step = _choose_step("Replacement action: ", mind)
            if step is None:
                return habit
            steps[index] = step
        elif len(steps) == 1:
            ui.note("A habit must keep at least one step.", "warning")
            return habit
        else:
            steps.pop(index)
    revised = replace(habit, steps=tuple(steps))
    if any(isinstance(step, habits.SleepStep) and index != len(revised.steps) - 1
           for index, step in enumerate(revised.steps)):
        ui.note("Go to sleep must be the final step.", "warning")
        return habit
    if revised.trigger_kind != "phrase" and habits.private_steps(revised.steps):
        ui.note("Alarms and reminder reads require an addressed habit phrase.", "warning")
        return habit
    return _replace(store, mind, habit, revised, "SAVE",
                    "These are the exact actions the habit will run, in order.") or habit


def _habit_page(store, mind, habit):
    from . import care

    while True:
        toggle = "Turn on" if not habit.enabled else "Turn off"
        choice = care.menu("Habit", [("1", "Edit steps"), ("2", toggle),
                                      ("3", "Delete habit")], intro=_lines(habit))
        if choice is None:
            return
        if choice == "1":
            habit = _edit_steps(store, mind, habit)
        elif choice == "2":
            revised = replace(habit, enabled=not habit.enabled)
            verb = "ENABLE" if revised.enabled else "DISABLE"
            saved = _replace(store, mind, habit, revised, verb,
                             "Turning it off keeps its trigger and steps; turning it on lets it run again.")
            if saved is not None:
                habit = saved
        elif choice == "3":
            if not _confirm(mind, "Delete habit", habit, "DELETE",
                            "Delete removes this trigger and its steps from PipOS."):
                continue
            if not store.remove(habit, forget=True):
                ui.note("That habit changed elsewhere. Nothing was deleted.", "warning")
                continue
            ui.note("Habit deleted.", "success")
            return


def page(mind, temporary=False):
    """List and manage the bounded habit catalogue from the owner screen."""
    from . import care

    mind = Path(mind)
    if guest_active():
        ui.note("Habits are private while a guest is here.", "warning")
        return
    store = habits.Store(mind)
    while True:
        known = store.habits()[:habits.MAX_HABITS]
        if not known:
            care.menu("Habits", [], intro=["No habits are saved.",
                                            "Say “new habit” to Pip to create one."])
            return
        entries = [(str(index), _safe(habit.trigger), "On" if habit.enabled else "Off")
                   for index, habit in enumerate(known, 1)]
        intro = ["Choose a habit to see its trigger and every step."]
        if temporary:
            intro.append("In Try, changes last until you restart.")
        choice = care.menu("Habits", entries, intro=intro)
        if choice is None:
            return
        _habit_page(store, mind, known[int(choice) - 1])
