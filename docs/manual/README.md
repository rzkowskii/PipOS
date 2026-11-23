<!--
doc: index
verified: 2026-11-24T04:50
sources:
-->

# The PipOS manual

PipOS turns a 2012 MacBook Pro into **Pip**, a small offline being with a
face, a voice and a memory. The project is aiming for a being with a life
of its own; it does not claim that Pip is sentient. This folder is the
maintained documentation. It describes what the code does now, and it
grows as PipOS grows.

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
| [STATUS.md](../../STATUS.md) | Where PipOS stands: evidence per milestone, blockers, what needs the laptop |
| [PIP-FUNCTIONS.md](../PIP-FUNCTIONS.md) | What Pip can do: the function spec and the wave 2 plan |
| [DESIGN.md](../DESIGN.md), [DECISIONS.md](../DECISIONS.md), [DECISIONS-OWNER.md](../DECISIONS-OWNER.md) | The current design, the decisions taken, and those left to the owner |
| [IMPROVEMENT-PLAN.md](../IMPROVEMENT-PLAN.md) | The phased plan |
| [DESIGN-AND-PLAN.md](../../DESIGN-AND-PLAN.md) | The original design and phased plan: the *why* (historical) |
| [PROTOCOL.md](../PROTOCOL.md) | The bus topics: the normative message reference |
| [LAB.md](../LAB.md), [HARDWARE.md](../HARDWARE.md), [FIRST-BOOT.md](../FIRST-BOOT.md), [ISO.md](../ISO.md) | Detailed lab, hardware, first-boot and ISO procedures |
| [USB-READINESS.md](../USB-READINESS.md), [DESIGN-COVERAGE.md](../DESIGN-COVERAGE.md) | Superseded release records of 21–22 November, kept for history |

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
verified: 2026-11-24T04:50
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
   time (the commit time for a committed, unchanged file, otherwise the
   file's modification time).
2. It reports source paths that no longer exist.
3. It reports broken relative links and `#anchors` between documents.

It exits non-zero when it finds a problem, so it can run in a pre-commit
hook or a test. `scripts/check fast` runs it as an advisory step.

When a feature lands:

1. Update the page or pages that describe it. Spoken commands go in the
   reference and the user guide. Architecture changes go in the
   architecture page.
2. Move items out of "not yet" and "designed but not built" lists.
3. Bump `verified:` on each page you re-checked.
4. At milestones (first physical boot, internal install, model choice),
   add a dated row to the assessment history and revise its verdict.

## Baseline

The four main pages (user guide, reference, architecture, developer
guide) were re-checked section by section on 24 November 2026 against
the `wave4` working tree: commit `f414c51` (waves 1 to 3: the first-boot
fixes, the Pip functions of wave 2, and the fixes from a full review)
plus the wave 4 changes in progress that night. Where wave 4 work was
still uncommitted when the pages were stamped, the pages describe the
working tree as it then stood; `scripts/check-docs.py` flags any later
change.
