"""Pip's owner permissions, shared by Care and the organs that use them.

Each permission reads the existing owner control on /mind so the same
selection governs this system and a previous A/B slot after rollback.
Runtime interlocks such as the lid, firewall and thermal checks still apply.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .dream_policy import consent_window as dream_window, pending_once
from .initiative import InitiativePolicy, load_policy as announcement_policy
from .network_policy import (
    INTERNET, LOCAL_ONLY, PAIRED_LAN, NetworkPolicy,
    load_policy as network_policy, web_lookup_ready,
)

CAMERA_MARKER = "camera.enabled"
WORKSHOP_DISABLED = "apps.disabled"
NETWORK_LABELS = {LOCAL_ONLY: "Offline", PAIRED_LAN: "Paired local network", INTERNET: "Internet"}


def camera_allowed(marker: Path) -> bool:
    try:
        info = marker.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def workshop_allowed(disabled_marker: Path | None) -> bool:
    if disabled_marker is None:
        return True
    try:
        disabled_marker.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


def dream_allowed(mind: Path) -> bool:
    return dream_window(mind) is not None


def _switch(allowed):
    return "On" if allowed else "Off"


@dataclass(frozen=True)
class Capabilities:
    camera: bool
    network: NetworkPolicy
    web_ready: bool
    workshop: bool
    announcements: InitiativePolicy
    dream_window: str | None

    def rows(self):
        quiet = self.announcements.quiet_hours
        web = "Available" if self.web_ready else "Waiting for Internet" if self.network.permits_web_lookup else "Off"
        dreams = {None: "Off", "night": "At night", "closed": "Whenever the lid is closed", "tonight": "Tonight"}
        return (
            ("Camera", _switch(self.camera)),
            ("Network", NETWORK_LABELS[self.network.effective_mode]),
            ("Web lookups", web),
            ("Workshop", _switch(self.workshop)),
            ("Reminders", _switch(self.announcements.reminders)),
            ("Greetings", _switch(self.announcements.greetings)),
            ("Morning brief", _switch(self.announcements.brief)),
            ("Quiet hours", f"{quiet.start} to {quiet.end}" if quiet else "Off"),
            ("Dreams", dreams[self.dream_window]),
        )

    def spoken(self):
        camera = "You allow my camera for local use." if self.camera else "My camera is off."
        network = {
            LOCAL_ONLY: "I'm offline",
            PAIRED_LAN: "I can use the paired local network",
            INTERNET: "I have permission to go online",
        }[self.network.effective_mode]
        web = " and can do web lookups" if self.web_ready else ""
        workshop = "open" if self.workshop else "closed"
        dreams = {"tonight": "Dreaming is allowed for one night.", None: "Dreaming is off."}
        dream_line = dreams.get(self.dream_window, "Dreaming is on.")
        return (f"{camera} {network}{web}. Workshop access is {workshop}. {dream_line} "
                "You can see my announcement settings under Permissions in Care.")


def load_capabilities(mind, *, temporary=False, local_now=None):
    mind = Path(mind)
    network = NetworkPolicy() if temporary else network_policy(mind)
    standing_dream = dream_window(mind)
    dream_status = standing_dream
    if dream_status is None and local_now is not None:
        try:
            if pending_once(mind, local_now) is not None:
                dream_status = "tonight"
        except (TypeError, ValueError):
            pass
    return Capabilities(
        camera=camera_allowed(mind / CAMERA_MARKER),
        network=network,
        web_ready=web_lookup_ready(mind, policy=network),
        workshop=workshop_allowed(mind / WORKSHOP_DISABLED),
        announcements=announcement_policy(mind),
        dream_window=dream_status,
    )
