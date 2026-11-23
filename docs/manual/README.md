<!--
doc: index
verified: 2026-11-23T22:20
sources:
-->

# The PipOS manual

PipOS turns a 2012 MacBook Pro into **Pip**, a small offline AI being with
a face, a voice and a memory. This folder is the maintained documentation.
It describes what the code does now, and it grows as PipOS grows.

| Document | For | What it covers |
|---|---|---|
| [User guide](USER-GUIDE.md) | The owner | Meeting Pip, talking, memory, settings, privacy, install, backup, troubleshooting |
| [Reference](REFERENCE.md) | Owner and developers | Every spoken command, key, menu, maintenance command, file, setting and service |
| [Architecture](ARCHITECTURE.md) | Developers | Organs, bus, states, mind, memory, body, boot and updates, security model |
| [Developer guide](DEVELOPER-GUIDE.md) | Developers | Prerequisites, running, building, testing, where to change things |
| [Assessment](ASSESSMENT.md) | The project | Current state, strengths, risks, prioritized next steps |

The other documents keep their roles:

| Document | Role |
|---|---|
| [DESIGN-AND-PLAN.md](../../DESIGN-AND-PLAN.md) | The original design and phased plan: the *why* |
| [PROTOCOL.md](../PROTOCOL.md) | The bus topics: the normative message reference |
| [LAB.md](../LAB.md), [HARDWARE.md](../HARDWARE.md) | Detailed lab and hardware procedures |
| [USB-READINESS.md](../USB-READINESS.md), [DESIGN-COVERAGE.md](../DESIGN-COVERAGE.md) | Release coordination and evidence |

## Naming

| Name | What it refers to |
|---|---|
| **PipOS** | The product, as shown in the boot menu, installer and update certificate |
| **BeingOS** | The repository and code name, used in paths, packages and `being-*` services |
| **Pip** | The default name of the being. The owner can rename it |

## Keeping these documents true

Each page starts with a comment header:

```
<!--
doc: user-guide
verified: 2026-11-23T22:20
sources: mind/commands.py mind/turn.py …
-->
```

- **`sources`** lists the files whose behaviour the page describes.
- **`verified`** is when someone last checked the page against those
  files. It is either a date (`2026-11-23`) or a local time
  (`2026-11-23T22:20`). Use the time form when other work is landing the
  same day.

Run the checker from the repository root:

```bash
python3 scripts/check-docs.py
```

The checker does three things:

1. It lists every page whose source files changed after its `verified`
   date.
2. It reports source paths that no longer exist.
3. It reports broken relative links between documents.

It exits non-zero when it finds a problem, so it can run in a pre-commit
hook or a test.

When a feature lands:

1. Update the page or pages that describe it. Spoken commands go in the
   reference and the user guide. Architecture changes go in the
   architecture page.
2. Move items out of "not yet" and "designed but not built" lists.
3. Bump `verified:` on each page you re-checked.
4. At milestones (first physical boot, internal install, model choice),
   add a dated row to the assessment history and revise its verdict.

## Baseline and pending work

The manual was written and checked by independent reviewers on
23 November 2026, against the source as it stood at about 22:20:

- commit `b1fbeea`, the source of the first laptop Try image;
- a few later changes that the second review round picked up: the
  `ERASE MACOS` / `ERASE BEING` confirmation, the hidden boot menu on
  installed disks, the `being-wifi scan` and `ethernet` commands, and the
  `mind.progress` think timeout.

Other sessions were landing a large change set at the same time. As of
22:40 on 23 November, these changes are **not yet documented**:

- **The `hands` organ** (`mind/hands.py`, designed in
  `docs/design/voice-apps.md`). This is a voice-driven "workshop
  terminal". It runs as an unprivileged `being-shell` user, in a RAM-only
  `/workshop`, with no network.
- **The `bodycare` crate** (`crates/bodycare`). This is a power and
  thermal controller: charge limit, fan floor, CPU caps and a restore
  journal.
- **Brainstem resume.** A restarted brainstem picks up its coarse state,
  mute and sleep from `/run` instead of re-waking.
- **Further changes** in `mind/turn.py`, `mind/voice.py`, `mind/tts.py`,
  `mind/recognition.py`, `mind/commands.py`, the installer, GRUB and the
  console.

Run `python3 scripts/check-docs.py` to see exactly which pages these
changes affect. Once that work is committed, re-verify those pages and
move each item into the right document.
