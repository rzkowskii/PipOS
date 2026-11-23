# Changed commitments

Every place where PipOS departs from the original design of 20 November
([DESIGN-AND-PLAN.md](../DESIGN-AND-PLAN.md), historical), with the reason and
the owner's answer. On 23 November the owner said "Use best judgement" on open
questions, so a change marked **accepted (best judgement)** stands until he
says otherwise. Open choices he still has to make are in
[DECISIONS-OWNER.md](DECISIONS-OWNER.md). The current design is [DESIGN.md](DESIGN.md).

| # | Original commitment | What changed | Why | Owner |
|---|---|---|---|---|
| 1 | One image for the lab and the laptop | Two images from one tree: a lab image (serial, root login, lab body) and a laptop image (no shell, no sshd, no gettys), plus a visible LAN maintenance build | A lab image on the Mac would be a root shell on the owner's being; the QEMU tests need the shell | Accepted (best judgement, 23 Nov) |
| 2 | Voice in Rust on sherpa-onnx (Silero VAD, Zipformer, Piper) | PocketSphinx and Flite in the Python voice organ today; sherpa-onnx through ctypes is the planned upgrade (5.2, 5.3) | ONNX Runtime had no AVX-only build; PocketSphinx and Flite shipped first | Accepted (best judgement); natural voice is his choice (DECISIONS-OWNER 13) |
| 3 | Deep overnight model: start with the conversation model, then a 4B | Qwen3-1.7B Q4_0 on its own server; a 4B only as an optional arm | Research of 23 Nov; his own "4B is too heavy" | Accepted (his words) |
| 4 | Dreams in a small night window | Whenever the lid is closed on AC (night only as an option) | His description: a second model making sense of its data while closed | Accepted (best judgement); window choice is DECISIONS-OWNER 14 |
| 5 | Dream work stops at 65 °C | Bodycare holds a target temperature and stops only on hard limits | A fixed stop rule wastes cool nights and cannot react to battery heat | Accepted (best judgement) |
| 6 | "No shell on the screen" | No *login* shell; the workshop's caged, RAM-only shell is allowed | His 23 Nov request for voice-driven apps | Accepted (best judgement; voice-apps.md "Decisions" 1) |
| 7 | Wi-Fi first for connectivity | Wired Ethernet first; b43 with owner-staged firmware as an experiment; MT7612U USB fallback | b43 HT-PHY support for the BCM4331 is weak | Accepted (best judgement) |
| 8 | Updates over SSH or from a USB stick | The laptop image has no SSH; updates and backups move to the F2 screen from a USB stick (1.3, 1.4), signed with an owner release key (1.2) | No shell on the shipping image | Accepted (best judgement); key custody is DECISIONS-OWNER 6 |
| 9 | Models partition 48 GB | Reserved at install, default 24 GiB (16-32) | Room for the dream model and upgrades without a reinstall | Accepted (best judgement) |
| 10 | Mic closed while asleep (design §9) | Wake spotting stays on while asleep, with a visible "ears open" cue | Otherwise only a key or the lid can wake it | Accepted (best judgement); DECISIONS-OWNER 9 |
| 11 | Charge cap through the out-of-tree applesmc-next driver | An in-tree applesmc patch (BCLM), re-asserted by bodycare | applesmc is built in, so an out-of-tree module can never load | Accepted (best judgement) |
| 12 | Battery must stay in | Mode A (battery in, capped) is the default; Mode B (battery out, MSR and power caps) is an opt-in fallback | Linux can undo the 1.2 GHz clamp, with care | Accepted (best judgement); DECISIONS-OWNER 2 |
| 13 | Senses line on every turn | First-person notable changes only ("My battery is low") | Routine readings hurt the 0.8B model and cost tokens | Accepted (best judgement) |
| 14 | A Jev/OpenJev decision model beside the conversation model | Archived (about 7 GiB, and it lost a correct recall) | Ended negative in the lab | Accepted (best judgement); the lab notebook stays in the private project records |
| 15 | llama-bench for laptop measurements | Bench mode drives llama-server directly with production flags (`being.bench=1`) | One harness, the real flags, no extra binary on the appliance | Accepted (best judgement) |
| 16 | Name "BeingOS" (working title) | **PipOS**, "Persistent Intelligence Platform Operating System"; folders, packages and services keep their old names for now | His choice | Accepted (his choice) |
| 17 | Wi-Fi, default skills, installer, voice choice | Kept; the 23 Nov review called them drift, wrongly | He asked for them (recorded in the project log) | Accepted (his requests) |

When he answers a row differently, change the Owner column, add the date, and
open or close the plan task it names.
