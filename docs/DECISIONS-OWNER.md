# Owner decisions

Choices only the owner can make. Each has a recommended default and what it
changes. On 23 November he said "Use best judgement" on open questions, so
every default below is the one the work builds toward unless he overrides it.
Many are not built yet: the plan task named in each row builds it. Items marked
**Made** were settled by best judgement in [IMPROVEMENT-PLAN.md](IMPROVEMENT-PLAN.md)
section 2 or in the workshop design ([design/voice-apps.md](design/voice-apps.md),
"Decisions"). Items marked **Open** still need his word before the work that
depends on them ships.

To change one: say so in any session, and the change is recorded in
[DECISIONS.md](DECISIONS.md) with the date.

## Needed before the second Try image

| # | Question | Default | If he picks otherwise | Status |
|---|---|---|---|---|
| 1 | **Time-zone city** | Sydney (`Australia/Sydney`, AEDT in summer) as the factory zone; Setup can change it | Any city; until a zone is set the clock is UTC and night, quiet hours and dreams run about 11 h off | Made (plan 1.10) |
| 2 | **Battery mode** | **A**: battery stays in, charge capped at 80% (choice of 60-80) | **B**: battery removed. Needs MSR writes to undo the 1.2 GHz clamp and a power cap for the 60 W charger; a power cut is an instant power-off | Made (plan section 4) |
| 3 | **Lexar or a second stick** for Try | A **second stick**; keep the Lexar's Monterey installer until macOS chores (0.7) are done | Overwrite the Lexar (he approved this on 21 Nov, after qualification): no way back to macOS for the battery cap and firmware update without Internet Recovery | Made (second stick recommended; his 21 Nov approval still stands if he prefers the Lexar) |
| 4 | **Tag the source** of the frozen stick as `first-hardware-try` (plan 0.1) | Yes | Nothing can be tied to the stick that boots the Mac | **Open.** A commit exists ("PipOS: first public release", 23 Nov 23:13), made after the stick was built at 22:09; there is no tag, and nothing yet shows that commit is the stick's source |
| 5 | **Resend the two screenshots** of 23 Nov 08:19 and 09:11 | Resend them | They stay unreviewed | **Open** |

## Needed before installing to the SSD

| # | Question | Default | If he picks otherwise | Status |
|---|---|---|---|---|
| 6 | **Release-key custody** | A passphrase-protected owner key, two offline copies (the NVMe archive and a USB stick kept apart from the laptop); the passphrase lives only with the owner | One copy: losing it means the installed being can never be updated without a reinstall | Made (plan 1.2) |
| 7 | **Models partition size** | Twice the model filesystem's occupied bytes plus 96 MiB, rounded up to the next MiB | A fixed reservation would take the same space on every disk regardless of the packs on the Try media | Made (Shell Lab S9, 25 Nov) |
| 8 | **Recall order** | Relevance first, then the most recently confirmed fact within the same relevance band | Pure recency: newer facts win even when an older one matches better | Made (plan 3.3) |
| 9 | **Mic open while asleep** | Yes, for "Hey Pip" only, with a visible "ears open" cue; nothing is stored unless addressed | Closed: waking it needs a key or opening the lid | Made (plan 3.10) |
| 10 | **Announcements by default** | Timers and reminders on; the daily greeting off (needs the camera) | All off: timers never ring unless asked | Made (current default) |
| 11 | **Lid closed on battery** | Shut down cleanly after 10 minutes | Longer, or never (a closed bag on battery runs flat) | Made (plan section 4) |

## Character and features

| # | Question | Default | If he picks otherwise | Status |
|---|---|---|---|---|
| 12 | **Camera default** | Off; switched on in Care ("you can use your camera"), with the green LED explained | On by default: presence greetings work from the first boot | Made |
| 13 | **Voice** | Natural (Piper, Alba) from the first reply. Flite only as the fallback (Alba fails its check or will not load, or measures slower than 0.7 of real time on the laptop) and as "Original voice" in Setup | Keep Flite only: more robotic, lighter on the CPU | Made (his choice, 24 Nov, after hearing Flite on the laptop; [DECISIONS.md](DECISIONS.md) row 18) |
| 14 | **Dream window** | Whenever the lid is closed on AC | Night only (midnight to 5 am, as today) | Made (plan 6.1) |
| 15 | **Dream temperature target** | Package 80 °C held by bodycare; hard stops at 92 °C package or 40 °C battery | Cooler (70 °C) dreams slower; hotter is not offered | Made (plan 2.8) |
| 16 | **Dream model** | Qwen3-1.7B, shipped as the pinned ggml-org Q4_K_M until bench mode on the laptop compares it with a Q4_0 built from the original weights (`lab/quant_candidates.py`); a 4B only as an optional "long cool night" arm | 4B by default: hotter and slower, and may not fit the models partition with upgrades | Made (plan section 2); Q4_K_M as the interim quant, his choice, 28 Nov |
| 17 | **Workshop scope** (voice apps) | On by default; terminal, body monitor and clock; RAM-only files; scripts and generated commands wait for "run it"; built-in one-line recipes use a countdown; offline shelf instead of downloads; no terminal in guest mode | Off by default, or wider scope (network installs) after the proof of concept | Made (voice-apps.md "Decisions", Shell Lab S7) |
| 18 | **LFM licence** for the LFM2.5-1.2B audition | Not accepted: the audition skips LFM models until he reads and accepts the licence | Accept: LFM2.5 joins the waking-model audition (plan 4.10) | **Open** |
| 19 | **Studio-assisted mode** (a remote cortex on the Mac Studio) | Off; the appliance accepts only its own loopback model | On: an explicit opt-in host, shown on the face; conversations leave the laptop over the LAN | Made (default off; plan 7.10 waits on him) |
| 20 | **Try logging** on the stick | On, with MAC and serial numbers masked (plan 3.16) | Off: a failed boot leaves nothing to read | Made |
| 21 | **Speaker enrolment** (plan 5.4) | Offered in Setup, opt-in; until enrolled every voice is "unknown" and only addressed speech is kept | Required: nothing works until he enrols | Made |
| 22 | **Which natural voice** | Alba (`en_GB-alba-medium`), chosen from a 27-voice audition. The present archive uses a research-only Lessac derivative for the private stick. For public media use Piper's 9 November retrain. Piper 1.5 and scratch-prepared Sherpa rendered it on the development Mac. Production pack preparation and the Ivy Bridge audition are still open. | Another auditioned voice whose licence allows redistribution, such as Cori | Made (his choice, 24 Nov); the swap is open before a public release |
| 23 | **Face style** | Eyes-only by default. Brows and mouth can be enabled independently. | Choose eyes and brows, eyes and mouth, or both. | Made (25 Nov, [Face Lab D1](../research/2026-11-25-face-expression-integration-plan.md#7-decisions-for-you-safe-default-first)). Native layers are in source. Laptop qualification remains open. |
| 24 | **Shell layout and controls** | One surface under the top-centre eye strip. Keyboard and voice first, captions off by default, Internet-only lookup and weather. | The [Shell Lab decisions](../research/2026-11-25-shell-lab-integration-plan.md#2-decisions-needed-from-you) record the full set of eleven choices. | Made (25 Nov); browser proof complete, later product phases open |
| 25 | **Drawn marks during conversation** (replaces the D6 hold for these eight) | With Effects switched on, Pip may show blush, hearts, tears, sweat drops, the sleep bubble, flame and steam, snow, and X or spiral pupils by itself during a conversation. Effects stays off by default; with it off every pose shows eyes-only. Never flame or steam while the body is actually hot (bodycare pressure hot or critical), and never X or spiral pupils while Pip is unwell, link-lost or recovering from a crash: the honest cues own the face then. Still lab-only because he was not asked: sick, queasy, sneeze, hiccup | Any mark off: it is stripped from every pose and the poses stay eyes-only | Made (28 Nov, about 14:25 AEDT, his answer on a question card, relayed by the coordinator session; the two safety rules are the coordinator's, inside what he allowed) |

## Already decided by him

- The product name is **PipOS** ("Persistent Intelligence Platform Operating System"); the being is **Pip**. Folders and services keep their old names for now.
- Wi-Fi, Internet mode, default skills and the installer are his requests and stay (opt-in, offline by default).
- "Merge all changes" (21 Nov): every staged patch is decided in plan 1.21.
- Training never uses his conversations.
- The natural voice is Alba, by default from the first reply (24 Nov; items 13 and 22).
- The keyboard works as on a Mac: the top row does what is printed on it, and Pip's own F-keys need fn (24 Nov; [DECISIONS.md](DECISIONS.md) row 19).
- The cable development stick is for development only and never installs (24 Nov; [DECISIONS.md](DECISIONS.md) row 20).
