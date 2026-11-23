# Is a 4B dream model too heavy for this laptop?

Research report, 23 November 2026. This is the second pass on the overnight dream model. It challenges the recommendation in `2026-11-23-tiny-models-and-dreaming.md` (sections 0, 1.3 and 2). Desk research only: nothing was installed and no model weights were downloaded.

Target: MacBookPro9,2 (13-inch, mid-2012), Core i5-3210M or i7-3520M (not yet read from the unit), 2 cores / 4 threads, Ivy Bridge (AVX, F16C; no AVX2, no FMA), 35 W TDP, 16 GB DDR3-1600, single fan, old battery, Buildroot Linux 6.18. The current dream gate: asleep, lid closed, on AC, CPU at most 65 C, 00:00-05:00.

**About the numbers.** Nobody has published an LLM, encoder or thermal benchmark for this CPU or this chassis under Linux. Every speed, duty cycle and token budget for "this laptop" is an **estimate** unless marked **measured**. Measured figures come from other machines (named each time) or from third-party reviews of this model. Each claim gathered for this report was fact-checked separately. Corrections from that check are used here, and claims that could not be verified were dropped or are flagged.

---

## 1. Direct answer

**Partly yes. A 4B model is not too heavy for the memory, but under the current rules it is too heavy for the time and heat available, and it is the wrong default.**

- **Memory is not the problem.** Qwen3-4B-Instruct-2507 at Q4 is 2.38-2.50 GB on disk (measured file sizes) and about 3.1-3.4 GB resident with a 2-4K context (estimate). The waking 0.8B, the 4B, speech and Python together come to about 5-6 GB of 16 GB (estimate).
- **Time and heat are the problem.** On this AVX-only CPU, reading a prompt costs about as much as writing output, so cost per token grows with parameter count. Any model that keeps both cores busy draws about the same package power (about 30-35 W, estimate), so the model size decides how many hot hours the night takes. A 4B needs roughly 2-2.5x the CPU-hours of a 1.7-2B model for the same job (estimate).
- **The 65 C rule as written does not fit a 4B at full turbo.** This machine normally runs at 90-100 C under sustained load on Apple's fan curve (user reports). The die passes 65 C within seconds of full load, so pausing between 1-5 minute chunks cannot hold that limit. A strict 65 C stop/start rule probably gives a 15-40% duty cycle, or about 6-24K tokens a night for a 4B (estimate). That covers a typical day only marginally and does not cover a heavy one.
- **The first pass overstated the 4B budget.** Its 3-4 tok/s was scaled from a desktop i5-3470T that clocks higher than the i5-3210M. For the i5 on 2 threads, 2.3-3.4 tok/s (central about 2.9) is more likely (estimate). Its 60% duty cycle is also unlikely under a 65 C stop/start rule.
- **The first pass also used the 4B to check its own claims** (about 1 minute per claim, estimate). That roughly doubles the 4B's nightly work. A dedicated 0.4B checker scores about as well as a 4B judge on the standard benchmark, at about a tenth of the cost.

So it depends on two things: **which thermal rule you keep** and **whether the dream model also has to be its own checker.** With a continuous power cap and a dedicated checker, a 4B can fit a typical night. It is still not the right default, because a 1.7-2B model inside the same checked pipeline does the job in half the hot hours, and the evidence that the 4B is more faithful is real but small.

---

## 2. Recommended overnight design (one screen)

| Part | Recommendation | Why |
|---|---|---|
| **Default dream generator** | **Qwen3-1.7B**, thinking off, temperature 0, Q4_0 or IQ4_NL (community GGUF or `llama-quantize`; Qwen's official repo has only Q8_0). | Apache-2.0, plain transformer (easy KV prefix reuse, none of the hybrid-restore bugs), and the best independent sub-3B summary evidence: Vectara HHEM-2.1 board 4.4% hallucination against 2.7% for Qwen3-4B under the same protocol (measured, news summaries). About 2-2.5x the 4B's throughput (estimate). |
| **Challengers to audition** | NuExtract-2.0-2B (MIT, extraction specialist with verbatim-string fields); Qwen3.5-2B (same code path as the waking model, but PFaithBench over-answer 43.3%); MiniCPM5-2B. | All unproven for faithfulness on this task. Decide by the A/B test in section 7. |
| **Optional "long, cool night" arm** | Qwen3-4B-Instruct-2507, kept only if the A/B shows it finds clearly more correct facts and the thermal log shows room. | PFaithBench gives Qwen3-4B the lowest over-answer rate of six Qwen models (27.3%), so it is a bit more faithful. Probably not worth 2-2.5x the heat once the checks below exist. |
| **Not the default extractor** | Qwen3.5-0.8B (the waking model). Use it for pre-sorting, or as the writer of the journal line under a strict grammar. | No direct summary-faithfulness data for the 0.8B; 0.6-1B models fail badly as judges; project audition showed weak format compliance. It may yet pass the A/B, so keep it as an arm. |
| **Verifier stack** | L0 deterministic checks in Python stdlib → per-call extractive grammar so quotes cannot be invented → **MiniCheck-RoBERTa-L** (0.4B, MIT) as the claim checker, run in the pinned llama.cpp → existing candidate→confirmed rule. Drop the generator's self-check. | Section 6. |
| **Thermal control** | Replace stop/start at 65 C with a **continuous cap**: a small Python stdlib controller that sets `scaling_max_freq` (or RAPL PL1 if not locked by firmware) and steers on package temperature. Raise the fan floor. Name the sensor the rule uses. | At the same heat, a capped clock does roughly 1.3-1.6x more work than turbo plus pauses (estimate). Section 4. |
| **Thermal rule** | Owner's decision. Either keep 65 C as the **target of the cap** (not a stop trigger), or move to about 75-80 C with resume at 70 C, which is still 25+ C under the 105 C junction limit. Add a battery-temperature gate (about 40 C) and a hot-room gate. | Section 4. |
| **Threads** | Bench 2, 3 and 4 threads. At night speech is idle, so 4 threads may add 10-15% (estimate; one other CPU on the same benchmark page lost speed above 4 threads). | |
| **Budget-aware queue** | Carry unprocessed chunks to the next night; the rule-based dreaming covers the gap. Never force a night to finish. | |
| **Token savers** | Short local line ids such as `[17\|O]` mapped back in Python instead of `[E1234\|owner\|2026-11-23 19:02]`; a byte-stable instruction prefix reused across chunks; skip unchanged material. | Cuts extraction input by about 25-40% (estimate). On AVX-only cores a cached prompt token saves as much as a faster model would. |
| **Memory** | Keep the waking 0.8B and the dream model both resident; stop the dream server at a chunk boundary on wake. Do not swap models. | Re-reading 2.5 GB from SATA costs about 6-10 s (estimate); RAM is plentiful. |

---

## 3. Overnight compute budget

### 3.1 Demand: how many tokens a day needs

These are assumptions, not measurements. Nobody publishes turns-per-day for voice companions.

| Day | Transcript tokens | Dream-model tokens with a dedicated checker (prompt + output) | Dream-model tokens if the generator also checks itself |
|---|---|---|---|
| Light (about 20 exchanges) | about 1K | about 6K | about 8-9K |
| Typical (about 60 exchanges) | about 3K | about 10K | about 13-15K |
| Heavy (1-2 h of talk, about 200 exchanges) | about 10K | about 24K | about 30-35K |

Derivation (throughput estimate): about 1K-token chunks; extraction about 900 new prompt tokens per chunk with a cached 300-token prefix plus about 450 output tokens; entailment about 70 new tokens per claim when the chunk's KV cache is reused (about 180 if not); merge, digest and journal about 2.5-4K a night. The self-check column adds the generator's one-token entailment calls at the uncached cost. Add about 10% for model load, pauses and wake gaps.

### 3.2 Supply: tokens per night by model and thermal rule

Window 00:00-05:00 is 18,000 s. Tokens per night = blended tok/s x 18,000 x effective duty. The window is shorter whenever the owner goes to sleep after midnight.

Blended rates for an i5-3210M at 2 threads, mainline llama.cpp, Q4 (estimates, ±30%; the i7-3520M is about +17%):

- 4B dense (Qwen3-4B): 2.3-3.4 tok/s generation, 2.5-5 prompt; blended about 2.3-3.4.
- 1.7-2B dense: about 5-8 blended.
- MoE with about 1.5B active (LFM2.5-8B-A1B): about 1.6x the 4B over a mixed night, so about 4-5.5 (MoE on old x86 is unmeasured anywhere).
- Qwen3.5-0.8B: about 10-18 blended.

Thermal rules (all duty figures are estimates derived from forum temperatures and the Notebookcheck power figures; none measured on the owner's unit):

- **A. 65 C stop/start at full turbo, stock fan:** effective duty about 15-40%.
- **B. 65 C held as a target by a continuous cap** (about 13-16 W package, about 1.6-1.8 GHz): runs all night at about 55-70% of turbo throughput, so effective about 55-70%.
- **C. 80 C pause / 70 C resume, fan curve with a higher floor, no turbo (2.5 GHz):** about 85% duty at about 86% of turbo speed, so effective about 70-80%.

| Model | Rule A (65 C stop/start) | Rule B (65 C capped) | Rule C (80/70 C, no turbo) | Typical day (10K)? | Heavy day (24K)? |
|---|---|---|---|---|---|
| **4B dense, dedicated checker** | 6-24K | 23-43K | 29-49K | A: marginal. B, C: yes | A: no. B: yes on cool nights, marginal on warm ones. C: yes |
| **4B dense, self-checking (first-pass design)** | 6-24K | 23-43K | 29-49K | A: marginal to no (needs 13-15K). B, C: yes | A: no. B: marginal (needs 30-35K). C: marginal to yes |
| **1.7-2B dense, dedicated checker** | 14-58K | 50-100K | 63-115K | Yes under all three | A: marginal to yes. B, C: yes, with room for a second self-consistency pass |
| **MoE about 1.5B active** | 11-40K | 40-69K | 50-79K | Yes (A marginal at the low end) | A: marginal. B, C: yes |
| **Qwen3.5-0.8B** | 27-130K | 99-227K | 126-259K | Yes | Yes |

Compute minutes for the same jobs (throughput estimate): 4B typical 37-67 min, heavy 91-167 min; 2B typical 17-28, heavy 42-69; Qwen3-1.7B (interpolated) about 15-25 and 37-60; MoE 21-46 and 53-113; 0.8B 7-13 and 18-33.

**Reading the table.** The 4B is the only option that can fail a typical day, and it does so only under rule A. A heavy day is where model size matters: the 4B needs rule B on a cool night or rule C, while a 1.7-2B model fits under any rule. Every number here moves with the room temperature, the state of the thermal paste and whether the lid really blocks the exhaust, none of which has been measured.

---

## 4. Thermal and wear findings

### 4.1 What is known about this machine

| Finding | Status | Source |
|---|---|---|
| Wall power 7.2-10.7 W idle, 37.4 W at about 50% load, 55.1 W at maximum load, on this exact model (i5-3210M). | measured (Notebookcheck, one new unit, lid open, about 23 C room) | Notebookcheck review |
| In a Prime95 + FurMark run, the CPU briefly dropped to 1.2 GHz when a core reached 104 C, then returned to turbo. A 2-hour Cinebench R11.5 loop gave identical scores (clock speed not logged). All-core turbo 2.9 GHz, single-core 3.1 GHz. | measured (Notebookcheck) | Notebookcheck review |
| Idle fan constant 2000 rpm; load noise 40-44 dB(A); bottom case peak 48.4 C. Notebookcheck gives no load rpm and no idle core temperature. | measured (Notebookcheck) | Notebookcheck review |
| Users report 44-54 C idle, up to 90-100 C under load with the fan still at about 2000 rpm, and about 92-95 C at about 4500 rpm after a repaste (about 20 C room). Fan maximum about 6000-6100 rpm (hedged). | anecdotal user reports, macOS | MacRumors thread |
| i5-3210M: 35 W TDP, Tjunction 105 C. i7-3520M: 35 W, 2.9/3.6 GHz; its 3.4 GHz two-core turbo comes from Notebookcheck, not Intel. | primary (Intel ARK) | Intel ARK |
| Closing the lid may partly cover the hinge exhaust. Forum reports conflict; no controlled test exists. | unmeasured | MacRumors threads |

### 4.2 What this means for the 65 C rule

1. **The rule must name its sensor first.** coretemp per-core and package readings, SMC TC0P (CPU proximity) and TC0D can differ by 10-20 C under load. A proximity reading of 65 C may mean a die at 80 C or more. Until the sensor is named, no duty-cycle number means anything.
2. **Stop/start at chunk level cannot hold 65 C at turbo.** Rough thermal resistance from two forum anchors (about 47 C at about 5 W, about 95 C at about 35 W) is about 1.5-1.7 K/W at stock fan behaviour, so 65 C is reached at only about 15-17 W package (estimate). With little heatsink mass, a full-turbo chunk overshoots to 80-95 C long before a 1-5 minute chunk ends. After 20-40 minutes the chassis has warmed and headroom shrinks further. Each 5 C of warmer room costs about 5 C of headroom.
3. **A continuous cap does more work for the same heat.** Dynamic power scales roughly with f x V^2, and compute-bound llama.cpp throughput scales roughly with f. Running at about 1.8 GHz may cost about 40-50% of the core power of 2.9 GHz for about 62% of the throughput, so roughly 1.3-1.6x more tokens per joule than turbo plus pauses (estimate; real voltages on this chip are unknown). One core at turbo is worse per joule than two capped cores.
4. **65 C is a design choice, not a hardware limit.** Apple's own fan curve lets this machine sit at 90-100 C. A 75-80 C ceiling is still 25 C or more below the 105 C junction limit. This is the owner's decision. If 65 C stays, use it as the cap's target, not as a stop trigger.

### 4.3 The Linux controls that exist

| Control | Status on this CPU and kernel | Notes |
|---|---|---|
| **cpufreq** (`scaling_max_freq`) | Expected to work. | On CPUs without HWP, intel_pstate defaults to **passive** mode (reported as the `intel_cpufreq` driver). Then `scaling_max_freq` per policy is the simplest cap. `no_turbo` and `max_perf_pct` are global attributes, and active mode needs `intel_pstate=active` on the kernel command line. The Buildroot config must enable intel_pstate and/or acpi-cpufreq. (Primary: kernel docs.) |
| **RAPL powercap** | Driver supports Ivy Bridge in v6.18 (`X86_MATCH_VFM(INTEL_IVYBRIDGE, ...)`). | If firmware locked the limit, the kernel logs "locked by BIOS" and writes return -EACCES. Whether Apple's EFI locks PL1 on this board is unknown. `energy_uj` is root-only (since the 2020 PLATYPUS fix), so the logger needs root or a udev rule. |
| **intel_powerclamp** | Available. | Idle injection capped at 50%; kernel docs say a userspace PID controller can hold a target temperature with it. A fallback if frequency capping is not enough. |
| **Fan control** (applesmc `fan1_min`, `fan1_max`, `fan1_manual`, `fan1_output`) | In mainline applesmc v6.18. | mbpfan lists "MacBook Pro 9,2" as tested, but on Linux 3.10. Its defaults are 2000/6200 rpm and 63/66/86 C. If software takes the fan (`fan1_manual=1`) and then crashes, the fan stays fixed; a watchdog that restores `fan1_manual=0` is a safety requirement. Whether the SMC's own curve stays active under Linux without a daemon was not sourced. |
| **Undervolting** | Assume unavailable. | Offset voltage through MSR 0x150 is generally not supported on Ivy Bridge mobile (from general knowledge, not checked against a primary source). |

thermald is not needed. A controller of about 100 lines of Python stdlib writing these sysfs files fits the project rules.

### 4.4 Wear and safety

- **The old battery is the main wear risk, more than the CPU.** On AC every night it sits at 100% charge and gets warm. Battery University's generic Li-ion storage table (one year): at 25 C, 100% charge keeps 80% of capacity against 96% at 40% charge; at 40 C, 65% against 85% (measured, generic cells, not an Apple A1322 test).
- **Charge limiting is unconfirmed on MacBookPro9,2.** The SMC key BCLM (with BFCL) works on 2013-2017 pre-T2 Intel Macs (tested on MacBookPro11,3 and 11,5). A mid-2012 MacBookAir5,2 report was inconclusive, and Core 2 Duo boards lack the key. Mainline applesmc v6.18 exposes no charge-control attribute, so this needs the out-of-tree applesmc-next patches or a direct SMC key write. Probe read-only first.
- **Gate on battery temperature.** applesmc exposes battery sensors (TB0T/TB1T/TB2T on these boards). Pause above about 40 C. Watch for swelling (on this generation the trackpad stops clicking). A fresh battery is a cheap option for an always-on appliance.
- **Thermal paste is probably dried out after 12+ years.** Owners report about 81-85 C full load after a repaste, though results vary (anecdotal). iFixit has a heat-sink guide for this model. Repaste and dust once before drawing conclusions from the soak test.
- **Fan bearing.** Hours at high rpm every night wear the single fan. No rated life was found; the part is cheap and user-replaceable. Capping the fan at about 5000 rpm trades a little cooling for bearing life and noise.

### 4.5 Options that change the rules (owner's decision)

- **Daytime idle slices.** Let consolidation run in long idle stretches on AC at a low cap, preemptible by the waking model. This removes most of the nightly budget pressure without running hotter. Cost: fan noise and CPU contention while the owner may be nearby.
- **Lid open, display off.** If "lid closed" exists only to mean "asleep", backlight off with the lid open avoids the hinge-exhaust question entirely.
- **Placement.** Raising the rear hinge, a hard surface or a small external fan may matter more than a 5 C rule change. None was quantified.

---

## 5. Small-model and hybrid alternatives

### 5.1 Faithfulness evidence, corrected

- **Vectara HHEM-2.1 board** (older dataset, Qwen3 run from HF checkpoints with thinking off, temperature 0, about 831 CNN/DM articles; last updated Oct 2025): Qwen3-4B 2.7%, Qwen3-8B 3.0%, Phi-4-mini 3.4%, Qwen3-0.6B 3.7%, Qwen3-1.7B 4.4%, Gemma-3-1B 5.3%, Granite-4.0-micro 6.5%, Gemma-2-2B 7.0%, Qwen2.5-1.5B 15.8%, Llama-3.2-1B 20.7%, Qwen2.5-0.5B 25.2% (measured, news summaries).
- **Current Vectara board** (HHEM-2.3, updated 22 Oct 2026): qwen3-4b 5.7%, but accessed through Alibaba's DashScope API, so not clearly the 2507 Instruct checkpoint. **Correction to the first pass:** the Qwen3.5 entries at 10.5-12.1% are all large models (27B and up). No Qwen3.5-0.8B or 2B entry exists, so the board says nothing about the small Qwen3.5 models either way. Phi-4-mini moves from 3.4% to 23.5% between boards, so rankings do not carry over cleanly.
- **PFaithBench** (arXiv 2610.07894, 6 Oct 2026; context-faithful QA, thinking off), over-answer % (fabricating when the context does not support an answer): Qwen3-4B 27.3, Qwen3.5-0.8B 35.3, Qwen3.5-4B 35.3, Qwen3.5-2B 43.3, Qwen3-1.7B 48.2, Qwen3-0.6B 48.3 (measured). So the 4B family does have the best fabrication score here. **Correction:** with thinking on, small Qwen3 models over-answer about 84% of the time (they fabricate, not abstain). Thinking must be off and forbidden by grammar.
- **Vectara FaithJudge** (FaithBench summarisation column alone): Qwen2.5-1.5B 84.7%, Llama-3.2-1B 75.0%, Qwen2.5-0.5B 88.9% hallucination (measured). Free-form summaries by sub-2B models are unsafe; narrow extraction with checks is the right shape at any size.

### 5.2 Comparison table

CPU minutes are for a typical / heavy day on the i5 at 2 threads (estimates). RSS is with a 2-4K context.

| Option | Size / RSS | Licence | Est. CPU min (typical / heavy) | Faithfulness evidence | Runtime risk | Role |
|---|---|---|---|---|---|---|
| **Qwen3-4B-Instruct-2507** | 4.02B; Q4_0 2.38 GB, Q4_K_M 2.50 GB; RSS about 3.1-3.4 GB; f16 KV 147 KB per token (576 MiB at 4K) | Apache-2.0 | 37-67 / 91-167 | Best of the Qwen family: HHEM-2.1 2.7%, PFaithBench over-answer 27.3%. Current board 5.7% may be the original Qwen3-4B | Low (plain transformer) | Optional long-night arm |
| **Qwen3-1.7B** | about 2B with embeddings; community Q4_K_M 1.11 GB, Q4_0 1.06 GB | Apache-2.0 | about 15-25 / 37-60 (interpolated) | HHEM-2.1 4.4%; PFaithBench over-answer 48.2% (weak point, so the NONE option and the checker are required). No 2507 non-thinking refresh at this size | Low | **Default** |
| **Qwen3.5-2B** | Q4_0 1.21 GB, Q4_K_M 1.28 GB; KV 12 KB per token (hybrid) | Apache-2.0 | 17-28 / 42-69 | No Vectara entry; PFaithBench over-answer 43.3% | Medium (hybrid prefix caching depends on context checkpoints) | Challenger; same code path as the waking model |
| **NuExtract-2.0-2B** | about 2B (Qwen2-VL-2B base) | MIT | about 17-28 / 42-69 | Vendor only; verbatim-string fields and null on missing match the honesty rule | Medium (template must be applied by hand in llama.cpp) | Extraction-specialist challenger |
| **MiniCPM5-2B** | 2.52B; Q4_K_M 1.56 GB | Apache-2.0 | about 20-33 / 50-80 | None for faithfulness; reasoning-tuned | Low (llama arch); use min_p 0 | Challenger |
| **Qwen3.5-0.8B** (reuse) | Q4_K_M about 0.53-0.54 GB; already resident | Apache-2.0 | 7-13 / 18-33 | PFaithBench over-answer 35.3% (same as Qwen3.5-4B); no summary data; weak format compliance in the project audition | Medium (hybrid prefix caching) | Pre-sorting; grammar-limited journal writer; A/B arm |
| **LFM2-1.2B-Extract** | about 1.2B | LFM Open License v1.0 (not OSI) | 11-18 / 27-45 | Vendor only | Medium; no longer listed on Liquid's Nanos page, so possibly unmaintained | Watch |
| **MiniCPM5-1B** | 1.08B; Q4_K_M 688 MB | Apache-2.0 | 11-18 / 27-45 | None for extraction | Low (plain KV cache) | Fallback if hybrid caching on the 0.8B is fragile |
| **LFM2.5-8B-A1B** (MoE) | 8.3B total, 1.5B active; Q4_0 4.84 GB; RSS about 5.2 GB | LFM Open License v1.0 | 21-46 / 53-113 | Vendor "non-hallucination" figure measures world-knowledge abstention, not grounding | Medium; unmeasured on any old x86 | Backlog speed tier only. **Correction:** about 1.6x the 4B over a mixed night at 1K context (closer to 2x at 4K), not the first pass's 2-3x |
| **Granite 4.0 H-Tiny** | 7B total, 1B active; Mamba2 hybrid | Apache-2.0 | similar to the MoE above | None for grounding | Medium; Mamba2 scan on AVX1 untested | Apache alternative to the MoE |
| **Granite 4.2 3B** | about 2.2 GB Q4_K_M | Apache-2.0 | about 30-50 / 75-125 | None for 4.2; first pass reported 4.8 tok/s CPU-only on an unnamed old laptop (not re-verified) | Low; thinking on by default | Middle option, if the A/B wants one |
| **Encoder hybrid**: GLiNER2.5-small (74M) + stdlib keyphrases, clustering, LexRank, dates + NLI or MiniCheck gate; the 0.8B writes only the journal line | about 0.3-2 GB depending on runtime (gliner.cpp's README says about 1.8 GiB for F32 weights plus scratch) | Apache-2.0 models; own code | about 3-5 min per day (estimate) | Spans and labels only, so no generated text enters memory. Zero-shot relation F1 about 0.33 (P 0.39 with a gate; one synthetic news test) | **High**: needs PyTorch or ONNX Runtime, or the immature gliner.cpp (2 stars, no relations, no quantisation). The modality gate in that test was the 340M large Decide model | Research arm, not phase 1 |

### 5.3 Notes on the hybrid route

The encoder pipeline is the most attractive on paper: it costs minutes rather than hours, cannot invent text, and makes forgetting exactly testable (forget-then-recompute must be byte-identical to compute-without). It is not recommended for the first version because its runtime is the weakest link right now:

- Third-party ONNX Runtime builds compiled for AVX2 crash with SIGILL on Ivy Bridge (reported on an i7-3615QM). Microsoft's own build reportedly dispatches AVX2 kernels only where present, but that was tested only on Windows under Intel SDE. The Linux wheel on an AVX-only CPU is unverified.
- The microsoft/onnxruntime GitHub repo has returned 404 since about 17 Nov 2026 (confirmed again on 23 Nov), which blocks source builds for Buildroot. PyPI wheels (latest 1.32.0, 13 Nov 2026) need glibc 2.28+, CPython 3.11+, numpy, protobuf and flatbuffers.
- ONNX Runtime 1.28.2 has run in the project's QEMU Ivy Bridge guest through sherpa-onnx (measured, `docs/LAB.md`), but it is a pending trial, not shipped in the image.
- No GLiNER accuracy on conversational or ASR-noisy speech exists.

A cheaper variant with no runtime risk: label the owner's own transcripts offline on the Mac Studio with a large model, then train tiny linear models (hashed n-grams plus the planned static embeddings) for dialogue act, modality and importance. Inference is then pure stdlib Python. This is worth a test as a replacement for the rule-based importance scoring.

---

## 6. Verifier stack

Reject at every step; never repair.

1. **L0: deterministic checks (Python stdlib, free).**
   - `episode_id` is in this chunk's id set.
   - The quote is an exact substring of the stored ASR text after NFKC, casefold, whitespace collapse and punctuation strip.
   - Speaker matches; the being's own lines never become owner facts.
   - At least about 60% of the claim's content words appear in the quote or the turns either side; cap the number of new content words.
   - Every number (including spelled-out numbers), date, relative-date phrase and known name in the claim appears in the quote. Resolve relative dates against the episode timestamp with stdlib rules.
   - Negation polarity matches. Hedge, question and joke cues force modality away from "stated".
2. **Extractive grammar (by construction).** Python builds a GBNF per call in which the quote field can only be one of the exact sentences (or sentence ids) of the turn under review, `kind`/`speaker`/`modality` are enums, a literal NONE is offered first, `<think>` is impossible, and `claim` is at most about 120 characters. Quote fabrication becomes impossible and output gets shorter. This is a design inference, not tested.
3. **L1: claim checker.** **MiniCheck-RoBERTa-L** (0.4B, MIT): LLM-AggreFact average 73.5, and 71.9 / 75.9 on the TofuEval dialogue-transcript sets (measured, published). For comparison on the same benchmark: Llama-3.1-8B 70.3; Qwen3-4B as a judge 73.8 **in thinking mode** (HalluGuard Table 1); Qwen3-0.6B 35.0. It is `RobertaForSequenceClassification`, which llama.cpp's converter registers, so it should run in the already-pinned llama.cpp with no new runtime. Conversion and the two-label output have not been tested, and the exact invocation must be worked out on the Mac Studio. Premise: the turn plus one turn either side, with speaker labels; claim in the third person; 512-token limit. Cost about 5-10 s per claim here (estimate), against about 50-80 s for the 4B self-check.
   - Upgrade: MiniCheck-Flan-T5-L (75.0 overall, 73.6 / 77.3 on dialogue). Needs a small libllama helper, because llama-server has no encoder-decoder path.
   - In-llama.cpp second opinion: Horizon Labs hallucination-guard-base (ModernBERT sequence classification, Apache-2.0, 0.716 on LLM-AggreFact, 8K context so it can take a whole episode).
   - ONNX fallback: HHEM-2.1-Open (Apache-2.0, about 0.1B; third-party ONNX export exists). Its RAGTruth-Summ balanced accuracy is only 64.4%, so it must not be the only gate.
   - Calibration only, on the Mac Studio: FactCG-DeBERTa-L (75.6), Bespoke-MiniCheck-7B (77.4; CC BY-NC 4.0, so private lab use only).
4. **Promotion rule (existing).** Items stay "candidate" until re-extracted from a different episode on a later night or confirmed by the owner. Journal names, numbers and dates must appear in the cited quotes.

What the checker cannot catch: omissions, poor importance choices, joking-versus-stated mistakes the cue rules miss, and relative-time errors. Those depend on the generator, and they are the real risk of going smaller. That is why the choice is made by the A/B test below, not by leaderboard numbers.

A checker at about 73-75% balanced accuracy may pass on the order of a quarter of unsupported claims at a neutral threshold (rough arithmetic on benchmark figures). Short extractive claims behind L0 and the grammar should do much better, but the real false-accept rate is unknown until calibrated.

---

## 7. What must be measured on the real laptop, and how

Run as root on the laptop unless noted. Paths assume mainline drivers. Nothing below writes to the SMC or the firmware.

### 7.1 Identify the machine and its sensors

```sh
grep -m1 'model name' /proc/cpuinfo          # i5-3210M or i7-3520M
grep -m1 -o -w 'avx\|avx2\|fma\|f16c' /proc/cpuinfo | sort -u
for h in /sys/class/hwmon/hwmon*; do echo "$h $(cat $h/name)"; done
grep . /sys/class/hwmon/hwmon*/temp*_label    # coretemp labels; applesmc keys such as TC0P, TB0T
grep . /sys/devices/platform/applesmc.768/fan1_min /sys/devices/platform/applesmc.768/fan1_max
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver
cat /sys/devices/system/cpu/intel_pstate/status 2>/dev/null
ls /sys/class/powercap/
cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw /sys/class/powercap/intel-rapl:0/constraint_0_time_window_us
dmesg | grep -i -e rapl -e 'locked by BIOS'
```

Decide which sensor the dream rule uses and write it down.

### 7.2 Speed: llama-bench

On the laptop, lid open, cool room, after idling 10 minutes:

```sh
llama-bench -m qwen3-1.7b-q4_0.gguf -m qwen3-4b-2507-q4_0.gguf -m qwen3-4b-2507-q4_k_m.gguf \
  -m qwen3-4b-2507-iq4_nl.gguf -m qwen3.5-0.8b-q4_k_m.gguf \
  -t 2,3,4 -p 512,1024 -n 128 -r 3 -o csv > bench-turbo.csv
```

Repeat at two caps:

```sh
# passive mode (intel_cpufreq): cap per policy
for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do echo 2500000 > $f; done   # no turbo
# ... run llama-bench again > bench-2500.csv
for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do echo 1800000 > $f; done
# ... run llama-bench again > bench-1800.csv
# restore
cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq   # then write that value back
```

Read `energy_uj` before and after each run to get tokens per joule.

### 7.3 Thermal soak, lid closed

A busybox shell logger, every 5 seconds, while a fixed generation runs for 60 minutes with the lid closed and on AC:

```sh
E=/sys/class/powercap/intel-rapl:0/energy_uj
while true; do
  echo "$(date +%s) $(cat /sys/class/hwmon/hwmon*/temp*_input | tr '\n' ' ') \
$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq | tr '\n' ' ') \
$(cat /sys/devices/platform/applesmc.768/fan1_input) $(cat $E)"
  sleep 5
done >> soak.log &
llama-bench -m qwen3-4b-2507-q4_0.gguf -t 2 -p 0 -n 512 -r 40 -o csv > soak-4b.csv
```

Run the soak four ways: turbo with stop/start at 65 C (the current rule), capped at 1.8 GHz, no turbo with a fan floor of about 3000 rpm, and the same two with the lid open and the backlight off. Record steady temperature on the named sensor, the battery sensors, tokens per minute and tokens per joule. Then repeat the best two with the 1.7B. These runs replace every duty-cycle estimate in section 3.

### 7.4 Battery

- Read the applesmc battery temperatures during the soak (TB*T labels from 7.1).
- Check whether the SMC on this board has the BCLM and BFCL keys, using a read-only key listing (applesmc-next, or a small read-only SMC reader). Do not write any key until its presence and meaning are confirmed.
- Check cycle count and health, and inspect for swelling.

### 7.5 Quality A/B (on the Mac Studio, same quants)

- Build a gold set: about 50 turns of synthetic, non-owner dialogue plus, if the owner agrees, owner turns kept on the Mac Studio. Hand-label the facts and the NONE cases.
- Run the full pipeline (L0, grammar, checker) with Qwen3-1.7B, Qwen3.5-2B, NuExtract-2.0-2B, Qwen3.5-0.8B and Qwen3-4B-2507, thinking off.
- Measure accepted facts per day, precision after checking (by hand), NONE accuracy, rejection rate, and modality and speaker errors that slip past.
- Rule: adopt the smaller model if it keeps about 85-90% or more of the 4B's accepted correct facts at equal precision.
- Calibrate the MiniCheck threshold on the same set, using FactCG and Bespoke-MiniCheck-7B as reference labellers.
- Confirm on the laptop that the converted MiniCheck GGUF gives the same scores as PyTorch within a small tolerance.

### 7.6 Worth one test each

- Speculative decoding with Qwen3-0.6B as draft for Qwen3-4B on grammar-constrained JSON (only if the 4B arm survives).
- llamafile versus mainline llama.cpp prefill on AVX1. LocalScore snapshots suggest llamafile prefills 1.6-2.4x faster than it decodes on Ivy Bridge, but those pages could not be fetched (HTTP 402), so this is unverified.

---

## 8. Uncertainties

- No benchmark exists for the i5-3210M or i7-3520M. All speeds are scaled from a desktop i5-3470T (mgroeber) and quad-core Ivy Bridge LocalScore snapshots (unverified), so treat them as ±30%. The closest laptop anchor, a Sandy Bridge i7-2640M (pp512 1.98, tg128 1.90 on 7B Q4_0), suggests the optimistic end is less likely.
- No lid-closed, sustained-load Linux temperature log exists for this model. Every duty cycle is derived.
- Whether Apple's EFI locks RAPL PL1, whether the SMC has BCLM, and how the SMC fan behaves under Linux without a daemon are all unknown.
- The daily token demand is an assumption.
- Faithfulness boards measure news summaries or QA abstention, not fact extraction from first-person ASR transcripts.
- MiniCheck conversion to GGUF is untested.
- The 0.8B and 2B Qwen3.5 models have no direct summary-faithfulness data in either direction.

---

## 9. Sources

Thermal and power
- Notebookcheck, MacBook Pro 13 2.5 GHz Mid 2012 review: https://www.notebookcheck.net/Review-Apple-MacBook-Pro-13-2-5-GHz-Mid-2012-Notebook.79640.0.html
- Intel ARK i5-3210M: https://www.intel.com/content/www/us/en/products/sku/65708.html ; i7-3520M (Notebookcheck): https://www.notebookcheck.net/Intel-Core-i7-3520M-Notebook-Processor.74446.0.html
- MacRumors, 2012 13-inch overheating: https://forums.macrumors.com/threads/macbook-pro-2012-13-overheating.2125243/
- MacRumors, clamshell ventilation: https://forums.macrumors.com/threads/clamp-shell-mode-blocking-ventilation.2110874 ; https://forums.macrumors.com/threads/mbp-runs-very-hot-when-in-clamshell-mode.1126687
- MacRumors, repasting: https://forums.macrumors.com/threads/reapplying-thermal-compound.1477887 ; iFixit heat sink guide: https://www.ifixit.com/Guide/Installing+MacBook+Pro+13-Inch+Unibody+Mid+2012+Heat+Sink/10380
- mbpfan config and tested models: https://raw.githubusercontent.com/linux-on-mac/mbpfan/master/mbpfan.conf ; https://github.com/linux-on-mac/mbpfan/wiki/Tested-Mac-Models
- Linux v6.18 RAPL: https://raw.githubusercontent.com/torvalds/linux/v6.18/drivers/powercap/intel_rapl_common.c ; powercap docs: https://docs.kernel.org/power/powercap/powercap.html
- intel_powerclamp: https://www.kernel.org/doc/Documentation/driver-api/thermal/intel_powerclamp.rst
- intel_pstate: https://docs.kernel.org/admin-guide/pm/intel_pstate.html
- Linux v6.18 applesmc: https://raw.githubusercontent.com/torvalds/linux/v6.18/drivers/hwmon/applesmc.c

Battery
- Battery University BU-808: https://batteryuniversity.com/article/bu-808-how-to-prolong-lithium-based-batteries
- BCLM reports: https://github.com/omacom/omarchy/issues/11593 ; https://github.com/m83n/battery-guard ; https://forums.macrumors.com/threads/getting-system-management-controller-core-2-duo-macbooks-and-charge-limiter-to-play-nice-together.2439923/
- applesmc-next: https://github.com/c---/applesmc-next

Throughput
- mgroeber legacy llama.cpp benchmarks: https://www.mgroeber.de/benchmarking-llamacpp-on-legacy-hardware-2.html
- LocalScore (snapshots only, site returned HTTP 402): https://www.localscore.ai/result/1719 ; https://www.localscore.ai/result/2753
- LFM2 technical report (CPU tables): https://arxiv.org/html/2511.23404v1
- Qwen3-4B-Instruct-2507: https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507 ; GGUF: https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF
- Qwen3.5-0.8B config: https://huggingface.co/Qwen/Qwen3.5-0.8B/raw/main/config.json ; Qwen3.5-2B GGUF: https://huggingface.co/unsloth/Qwen3.5-2B-GGUF
- Qwen3-1.7B GGUF: https://huggingface.co/Qwen/Qwen3-1.7B-GGUF ; https://huggingface.co/unsloth/Qwen3-1.7B-GGUF
- LFM2.5-8B-A1B: https://www.liquid.ai/blog/lfm2-5-8b-a1b ; https://huggingface.co/LiquidAI/LFM2.5-8B-A1B-GGUF
- Granite 4.0 H-Tiny: https://huggingface.co/ibm-granite/granite-4.0-h-tiny
- Mehl et al. 2007 (words per day): https://news.utexas.edu/2007/07/05/do-women-really-talk-more-than-men-research-refutes-popular-beliefs

Faithfulness and small models
- Vectara leaderboard, current: https://github.com/vectara/hallucination-leaderboard ; HHEM-2.1 era: https://raw.githubusercontent.com/vectara/hallucination-leaderboard/dab7925603/README.md ; https://github.com/vectara/hallucination-leaderboard/tree/hhem-2.3-old-dataset
- PFaithBench: https://arxiv.org/abs/2610.07894 ; https://arxiv.org/html/2610.07894v1
- FaithJudge: https://github.com/vectara/FaithJudge
- NuExtract-2.0-2B: https://huggingface.co/numind/NuExtract-2.0-2B ; NuExtract3: https://huggingface.co/numind/NuExtract3-GGUF
- LFM2-1.2B-Extract: https://huggingface.co/LiquidAI/LFM2-1.2B-Extract ; Liquid Nanos: https://docs.liquid.ai/lfm/models/liquid-nanos
- MiniCPM5-1B: https://huggingface.co/openbmb/MiniCPM5-1B
- llama.cpp hybrid-cache issues: https://github.com/ggml-org/llama.cpp/issues/32316 ; https://github.com/ggml-org/llama.cpp/issues/28194

Checkers
- LLM-AggreFact: https://llm-aggrefact.github.io/ ; data: https://raw.githubusercontent.com/llm-aggrefact/llm-aggrefact.github.io/main/scores_data.json
- MiniCheck paper and code: https://arxiv.org/html/2404.10774 ; https://raw.githubusercontent.com/Liyan06/MiniCheck/main/minicheck/inference.py ; https://huggingface.co/lytang/MiniCheck-RoBERTa-Large ; https://huggingface.co/lytang/MiniCheck-Flan-T5-Large
- HalluGuard (Qwen3 judge baselines, think mode): https://arxiv.org/html/2510.00880v1
- HHEM-2.1-Open: https://huggingface.co/vectara/hallucination_evaluation_model ; ONNX export: https://huggingface.co/FamiliarTools/HHEM-2.1-Open-onnx
- FactCG: https://huggingface.co/yaxili96/FactCG-DeBERTa-v3-Large ; Bespoke-MiniCheck-7B: https://huggingface.co/bespokelabs/Bespoke-MiniCheck-7B
- Horizon Labs hallucination-guard: https://huggingface.co/Horizon-Labs/hallucination-guard-base
- llama.cpp converters: https://raw.githubusercontent.com/ggml-org/llama.cpp/master/conversion/bert.py ; https://raw.githubusercontent.com/ggml-org/llama.cpp/master/conversion/t5.py ; server: https://raw.githubusercontent.com/ggml-org/llama.cpp/master/tools/server/server-context.cpp
- ONNX Runtime MLAS dispatch: https://raw.githubusercontent.com/microsoft/MLAS/main/src/lib/platform.cpp

Hybrid and encoders
- GLiNER2: https://github.com/fastino-ai/GLiNER2 ; https://huggingface.co/fastino/gliner2.5-small-v1 ; gliner.cpp: https://github.com/sandrohanea/gliner.cpp ; llama.cpp PR #30504: https://github.com/ggml-org/llama.cpp/pull/30504
- GLiNER relation test: https://lyonwj.com/blog/gliner-decide-knowledge-graph-extraction ; GLiNER-Relex: https://arxiv.org/html/2605.10108v1
- ONNX Runtime SIGILL on Ivy Bridge: https://github.com/team-reflect/reflect-open/issues/1802 ; dispatch note: https://github.com/KenKaiii/Roamies/pull/148 ; wheels: https://pypi.org/pypi/onnxruntime/json
- Sleep-time compute: https://arxiv.org/abs/2504.13171 ; Letta sleep-time agents: https://docs.letta.com/guides/agents/architectures/sleeptime

Project files
- `research/2026-11-23-tiny-models-and-dreaming.md`
- `DESIGN-AND-PLAN.md`
- `docs/LAB.md`
