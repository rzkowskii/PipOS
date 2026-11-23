# Tiny models, speech, recall, vision, character and dreaming for BeingOS

Research report, 23 November 2026. Desk research only: nothing was installed and no model weights were downloaded.

Target machine: MacBookPro9,2 (13-inch, mid-2012). Core i5-3210M or i7-3520M (still to be checked), 2 cores / 4 threads, Ivy Bridge with SSE4.2, AVX and F16C, and no AVX2, FMA or AVX-512. 16 GB DDR3-1600. HD Graphics 4000. Pinned runtime: llama.cpp `v1.0.0` (`CORTEX_VERSION` in `os/package/cortex/cortex.mk`; nightly b12359, released 13 Nov 2026).

**About the numbers.** Nobody has published llama.cpp, ONNX or speech benchmarks for this CPU, or for any model under 2B on a 2-core Ivy Bridge. Every speed given for "this laptop" is an **estimate** unless it is marked **measured**, and the derivation is given in section 1.3. Vendor benchmark scores are self-reported and often use different harnesses and thinking modes.

---

## 0. One-screen summary: the recommended stack

| Role | Recommendation | Why | Expected speed on this laptop |
|---|---|---|---|
| **Waking conversation** | Keep **Qwen3.5-0.8B** (Apache-2.0) as the default and speed floor. Audition, in this order: **MiniCPM5-1B** (Apache-2.0, plain Llama architecture), **LFM2.5-1.2B-Instruct** (LFM licence), **Qwen3.5-2B** (Apache-2.0), **LFM3-1.7B** (LFM licence; thinking is hard to switch off). | The 0.8B model is the only one already proven to fit a 3 s first word. Its weakness is reply quality (6/24 complete replies in the project's own audition), not speed. MiniCPM5-1B matches it in speed class and uses a pure-attention KV cache, so it avoids the open llama.cpp restore bugs on hybrid models. | Qwen3.5-0.8B: prefill about 15-28 tok/s, decode about 8-15 tok/s. 1B class: about 12-24 prefill, 8-12 decode. 2B class: about 6-11 prefill, 5-8 decode. **All estimates.** |
| **Overnight dream model** | **Qwen3-4B-Instruct-2507** (Apache-2.0, plain transformer, non-thinking) first. Then **Granite 4.2 3B** (Apache-2.0; thinking on by default, so switch it off), and **LFM2.5-8B-A1B** (MoE, about 1.5B active; LFM licence; about 5 GB) as the speed option. Keep Qwen3.5-4B and Gemma 4 E4B as comparison arms. | Qwen3 4B has the best small-model faithfulness evidence found (Vectara 5.7% hallucination). Every Qwen3.5 size Vectara lists does worse (10.5-12.1%). Its plain architecture makes prefix caching across chunks easy. | 4B dense: about 3-4 tok/s for both prompt and output (estimate), so about 30-40K tokens per night at a 60% thermal duty cycle. The MoE should give about 2-3x that, but this is unmeasured on any old x86. |
| **Dream pipeline** | Deterministic pre-pass → grammar-constrained JSON extraction (temperature 0, thinking off) → verbatim-quote and episode-id check in Python → one-token entailment check → supersede-only merge → calendar summary tree → cited dream journal → transitive forgetting. | Small models hallucinate most when they summarise. Checking quotes by exact substring catches most of that at no cost. Forgetting research in 2026 (BEMULA) found no memory system passed more than 1.5% of forget requests end to end, and that derived artefacts are where deleted data survives. | Fits in one night for a typical day of 3-10K transcript tokens (estimate). |
| **Inference settings** | 2 threads pinned one per physical core, `--poll 0` tested. Use quants with an AVX1 prompt path (Q4_0, preferably vendor QAT/QAD, IQ4_NL or Q8_0) and benchmark them against Q4_K_M. f16 KV cache, context 4096 or less. A byte-stable persona prefix, warm and cached. `--ctx-checkpoints` left at the default (never 0). `--load-mode mmap+mlock`. Use GBNF for the emotion tag and reply length. | On AVX-only cores prompt reading is about as slow as generation (**measured** on an i5-3470T, a 2C/4T Ivy Bridge: pp512 2.31, tg128 2.46 tok/s on a 7B Q4_0). Every uncached prompt token costs about one decode step. Weight repacking, ternary formats, bitnet.cpp, T-MAC and ik_llama.cpp all need AVX2. | Speculative decoding and MTP are probably no help here. One llama-bench run will settle it. |
| **Speech** | VAD: **Silero VAD** (MIT). ASR: **Moonshine v2 Small Streaming** (MIT), with Tiny as the fallback. TTS: a **Piper** medium voice on one thread (GPL-3 because of espeak-ng); bench **tiny-voca** (4.5M, Apache-2.0, no espeak-ng) and **KittenTTS 2 nano** against it. Speaker ID: **CAM++** through sherpa-onnx. Wake: gaze plus VAD, with sherpa-onnx keyword spotting as an option. | Moonshine streams partials while the owner is speaking, so most ASR work happens while the LLM is idle. Piper has the best measured weak-CPU numbers. | Moonshine Small: about 0.5-1.0 s after end of speech (estimate; **measured** 527 ms on a 4-core Pi 5). Piper medium: RTF about 0.3-0.5 on one thread (estimate; **measured** 0.774 on one Pi 4 thread). |
| **Recall** | Keep FTS5. Add a vector table in plain SQLite with pure stdlib Python search, fused with BM25 by reciprocal rank fusion and a cosine floor. Phase 0: static **blink-8M** embeddings (MIT, no model process). Phase 1: **granite-embedding-small-english-r2** (47M, Apache-2.0) or **bge-small-en-v1.5** (33M, MIT) in a second `llama-server --embedding`. No sqlite-vec. | Same retrieval quality as bge-small at the cost of one small model. sqlite-vec needs a CPython rebuild, its Makefile adds `-mavx2`, and it keeps deleted vectors until a whole chunk empties. | About 40-80 ms per query (estimate). Brute force over 5K vectors in Python: about 60-90 ms (estimate). |
| **Vision** | Free cues first: applesmc ambient light, frame-difference motion, the existing pico detector. Owner recognition with **YuNet** (MIT) plus **SFace** (Apache-2.0) in OpenCV DNN. "What do you see?" with **Qwen3.5-0.8B's own vision projector** (Apache-2.0, about 205 MB) in a **separate, on-demand llama-server**. | No second language model is needed for vision. llama-server refuses slot save/restore (HTTP 501) whenever `--mmproj` is loaded, so vision must not run inside the conversation server. | YuNet about 3-8 ms per frame at 160x120. SFace about 30-90 ms per face. One look at 256-320 px about 4-10 s. **All estimates.** |
| **Character** | A short, byte-stable persona prefix (about 200 tokens). A tighter GBNF (`[emotion]` plus 1-2 sentences, no newlines). A boilerplate filter. A **persona LoRA** trained on synthetic dialogue from **Qwen3.8-27B** (Apache-2.0) on the Mac Studio, then merged and requantised. Put most of the liveliness budget into the face. | Moving the voice from prompt tokens into weights saves prefill time on every turn. Control vectors are buggy in llama.cpp right now. Train only on synthetic, non-owner data, because a LoRA cannot be made to forget. | No runtime cost once merged. |

**Before deciding anything, run one llama-bench pass on the real laptop.** See section 8.

---

## 1. Tiny conversation LLMs (waking model)

### 1.1 What is new since the 16 November research

- **No open Qwen4 small model.** Qwen4-Max (16 Nov 2026) is API-only. Alibaba says smaller open models are coming but gives no date or sizes (secondary sources only). Qwen3.6 and Qwen3.8 have nothing under 27B, so Qwen3.5-0.8B and 2B are still Qwen's newest small text models.
- **LFM3-1.7B** (Liquid, HF repo 23 Oct, launch 27 Oct 2026). Strongest model under 2B on paper. Mainline llama.cpp support was merged in PR #31252 on 27 Oct, so the pinned v1.0.0 should include it.
- **MiniCPM5-1B** (19-21 May 2026) and **MiniCPM5-2B** (7 Sep 2026), both Apache-2.0 and both plain LlamaForCausalLM.
- **Microsoft Aion-1.0-Instruct-2.3B** (18 Nov 2026, MIT). It runs only through community GGUFs on a patched llama.cpp, or through an official INT4 ONNX build. Its card says it is meant for working on text you supply, not for world knowledge.
- **Tencent WePocketModel1.0-2B** (repo created 16 Nov 2026, Apache-2.0). A Qwen3.5-2B fine-tune with a Qwen3_5 architecture, so it may load in mainline. Not assessed.
- There is no SmolLM4, no Phi-5 and no 2026 small OLMo. Inkling 2 Mini (20 Nov) is 26B total, so it is not tiny.

### 1.2 Comparison table

| Model | Size / GGUF | Released | Licence | Runtime | Quality evidence | Est. speed here (prefill / decode tok/s) | Fit |
|---|---|---|---|---|---|---|---|
| **Qwen3.5-0.8B** (current) | 0.8B; 24 layers (18 Gated DeltaNet + 6 attention); 248K vocab (about 0.25B tied embeddings). Q4_K_M 533 MB, Q4_0 507 MB, IQ4_NL 507 MB, Q8_0 812 MB | HF 28 Feb 2026; announced about 2 Mar | Apache-2.0 | Mainline (qwen35); hybrid recurrent state; MTP head | Vendor, non-thinking: IFEval 52.1, MMLU-Pro 29.7. **Project audition: 6/24 complete replies, 11/24 useful.** In one Pi 5 test it complied with 0/6 format tasks under default reasoning. | 15-28 / 8-15 | Keep as default and speed floor |
| **MiniCPM5-1B** | 1.08B total, 0.68B non-embedding; 24 layers, GQA 16/2. Official Q4_K_M 688 MB, Q8_0 1154 MB, F16 (no Q4_0; make one with llama-quantize) | 19-21 May 2026 | Apache-2.0 | Mainline (llama arch); plain KV cache; `enable_thinking=False`, no-think temp 0.7, top_p 0.95 | Vendor claims 1B-class state of the art in thinking mode (MMLU-Pro 48.85). **Measured** Pi 5 (Ollama): 18.4 tok/s decode, 869 MB RSS, against 8.8 for Ollama's (larger, vision-bundled) Qwen3.5-0.8B tag | 18-24 / 9-12 | **First audition.** Easy rollback for revised ASR partials and no hybrid-restore bugs |
| **LFM2.5-1.2B-Instruct** | 1.17B; 16 layers (10 short-conv + 6 GQA); 65K vocab. QAD-Q4_0 696 MB, Q4_K_M 731 MB | 6 Jan 2026 | LFM Open License v1.0 (free below US$10M annual revenue; not OSI) | Mainline (lfm2); recurrent conv state | Vendor: IFEval 86.23, IFBench 47.33, MMLU-Pro 44.35. **Measured** Pi 5: 15.5 tok/s decode; 16/18 format compliance. Card targets agents, extraction and RAG, not chat. Its 350M sibling scored 0/24 in the project audition | 12-15 / 8-10 | Second audition; note the licence |
| **Qwen3.5-2B** | 2B; Q4_K_M 1.28 GB, Q4_0 1.215 GB | 28 Feb / 2 Mar 2026 | Apache-2.0 | Mainline; same template and code paths as the 0.8B | Vendor, non-thinking: IFEval 61.2, MMLU-Pro 55.3. Artificial Analysis Progress Index 171 (non-reasoning) | 6-11 / 5-8 | Apache quality step with zero integration change; needs warm prefix and read-ahead |
| **LFM3-1.7B** | 1.73B; 24 layers (13 conv, 5 SWA, 6 global); 128K vocab. QAD-Q4_0 1.05 GB, Q4_K_M 1.10 GB | 23/27 Oct 2026 | LFM Open License v1.0 | Mainline (lfm3, PR #31252); needs b11960 or later | Vendor (thinking mode): IFEval 96.66, IFBench 73.03, AA-Omniscience non-hallucination 92.93. Progress Index 216. **Measured** Pi 5 llama-bench: pp512 61.89, tg128 9.10 | 6-11 / 6-8 | Best on paper. **Thinking is on by default** and the card calls `enable_thinking=False` "inconsistent". Post-trained for agent harnesses |
| **Gemma 4 E2B-it** | 2.3B effective / 5.1B with per-layer embeddings; ggml-org Q4_0 2.84 GB; Google QAT q4_0 3.35 GB | HF 2 Mar 2026; launch about 2 Apr | Apache-2.0 | Mainline (gemma4); SWA; MTP drafter; `--lazy-mode` for PLE | MMLU-Pro 60.0, IFBench (loose) 38. **Measured** Pi 5: 9.03 tok/s decode at 2 threads, 13.06 with MTP n=2. Google's own Pi 5 llama.cpp Q4_0 figure is only about 24/4 | 4-8 / 4-7.5 | Slowest and largest. Audition only if the others lack warmth |
| **MiniCPM5-2B** | 2.52B total / 1.98B non-embedding; 42 layers; Q4_K_M 1.56 GB | 7 Sep 2026 | Apache-2.0 | Mainline (llama); use `min_p 0` (llama.cpp's 0.05 default causes repetition) | Vendor: IFEval 86.7, MMLU-Pro 70.8. Progress Index 221 (top at about 2B). **Measured** Pi 5: tg128 8.48 at 2 threads, pp512 20.9 | 7-8 / 4-6 | Reasoning-tuned and too slow for waking turns; better overnight |
| **LFM2.5-350M / 230M** | 350M / 230M | Mar / Jun 2026 | LFM | Mainline | **Project audition: 0/24 complete replies** | 45-55 / 25-35 (350M) | Not for the voice. Possibly a structured helper |
| **Granite 4.0 Nano (H-1B, 1B, 350M)** | 0.35-2B | 28 Oct 2025 | Apache-2.0 | Mainline | LFM's table: Granite-4.0-1B IFEval 79.61 | — | Apache fallback only |
| **Nemotron 3.5 Bolt E4B 4GB** | 8B total / 4B effective; Q4_K_M 6.18 GB | 23 Oct 2026 | OpenMDW-1.1 | GGUF | RoleBench 71.21 vs 60.51 for its Gemma 4 E4B base | about 4 / 3 | Too slow. Strong evidence that persona post-training matters |
| **Aion-1.0-Instruct-2.3B** | 2.29B; 21 SWA + 7 global layers | 18 Nov 2026 | MIT | Patched llama.cpp ("aion" arch, community GGUFs) or INT4 ONNX | Vendor IFEval 83.90; not trained for world knowledge | 4-6 / 4-6 | Watch; re-check once mainline supports it |
| BitNet b1.58 2B4T, Falcon-E, Ternary Bonsai 2 | 2B class | 2025-2026 | MIT / TII / Apache | bitnet.cpp, TQ1_0/TQ2_0/Q2_0 | — | Scalar fallback | **Reject.** All x86 kernels are AVX2-only. bitnet.cpp's I2_S functions have *empty bodies* on a non-AVX2 build, so it could compile and then quietly produce garbage |

Others checked and not recommended: Llama 3.2 1B (Llama licence, IFEval 52), MobileLLM-R1 (FAIR non-commercial), SmolLM2/3, Phi-4-mini (3.8B), Ministral 3 3B, OLMo 2 1B, Falcon-H1-Tiny, Gemma 3 1B/270M, Exaone 4.0 1.2B (non-commercial).

### 1.3 How the speed estimates were derived

- **Anchor (measured):** mgroeber.de, i5-3470T, a 35 W Ivy Bridge with 2 cores / 4 threads at 2.9-3.6 GHz, nearly the same chip as this laptop's. Llama 7B Q4_0, 4 threads, llama.cpp b6878 (tinyBLAS on): **pp512 2.31, tg128 2.46 tok/s**. Same page, Sandy Bridge i7-2640M laptop (2C/4T): **pp512 1.98, tg128 1.90**.
- That works out to about 13-16 billion parameter multiply-adds per second, for both prompt and generation. Generation is limited by arithmetic, not memory bandwidth: a 7B Q4_0 at 2.46 tok/s reads only about 9 GB/s against 15-18 GB/s achievable.
- **Prefill** ≈ (13-16)e9 ÷ non-embedding parameters. The LM head runs only for the last prompt token, so models with large vocabularies prefill relatively faster than they decode.
- **Decode** ≈ 0.75 × (13-16)e9 ÷ parameters used per token, including the output head. The 0.75 allows for small-model overheads.
- The lower ends of the ranges allow for the i5-3210M's lower clocks (about 15% less) and the i7-2640M result.
- **Raspberry Pi proxies:** Pi 5 *decode* numbers transfer at about 0.85-0.9x. Pi 5 *prefill* does not: a Pi 5 reads prompts about 5-7x faster than it decodes, while this CPU does not.
- **Uncertainty:** ±30% or more. Hyper-Threading, current-build kernel changes, Q4_0 vs Q4_K_M, and thermal throttling in the 13-inch chassis could all move these.

**Time to first word** ≈ uncached prompt tokens ÷ prefill rate + first-clause tokens ÷ decode rate (estimates):

| New tokens per turn | Qwen3.5-0.8B | 1B class | 1.7-2B class | Gemma 4 E2B |
|---|---|---|---|---|
| 100 | about 4-6 s | about 6-9 s | about 10-15 s | about 13-20 s |
| 40 | about 2 s | about 3-3.5 s | about 5 s | about 6-8 s |

So every token of identity text, sense readings or recall notes has to be cached or cut. Speech arrives at about 3 tokens/s, slower than every candidate's prefill rate, so feeding stable ASR partials to the model while the owner is still speaking ("read-ahead") hides most of the prompt cost.

### 1.4 Techniques worth adopting

1. **Warm, byte-stable prefix plus read-ahead.** Covered in section 3.
2. **Grammar for the emotion tag.** A GBNF of the form `[joy|calm|...] ` plus free text forbids `<think>`, which matters for LFM3 and MiniCPM5. With the tag set enforced by grammar, lower Qwen's recommended `presence_penalty 2.0` to 0-1.5 so repeated tags are not penalised. Check that fluency does not suffer.
3. **Disable thinking explicitly, per family:**
   - Qwen3.5: off by default; also pass `--reasoning off` or `enable_thinking:false`. It has no `/no_think` soft switch.
   - LFM3: on by default and "inconsistent" when switched off. Use grammar plus `--reasoning-budget 0`.
   - Gemma 4: on only if `<|think|>` starts the system prompt.
   - Granite 4.2, MiniCPM5, Nemotron: chat-template kwargs.
4. **Persona LoRA.** Covered in section 7. It is probably the largest quality lever for the 0.8-1.2B class, the only class that clearly fits the 3 s target.

### 1.5 What to avoid

- Ternary and 1-2-bit formats (scalar fallback on this CPU).
- Thinking mode in any waking model.
- The LFM2.5-350M/230M models as the voice (already measured).
- Choosing on IFEval: the project audition showed it does not predict warm in-character speech at this size.
- Waking-turn use of MiniCPM5-2B, Nemotron Bolt or Gemma 4 E4B.
- Loading the Qwen3.5 mmproj in the conversation server (section 6).
- Trusting the Pi 5 Ollama comparison that made Qwen3.5-0.8B look slow. Ollama's default tag bundles vision and uses a bigger quant (1.6 GB RSS), so the comparison says nothing about DeltaNet speed.

---

## 2. Overnight dream model and consolidation pipeline

### 2.1 Budget

For a 4B dense Q4 model on 2C/4T Ivy Bridge, both prompt and output run at about **3-4 tok/s** (estimate, scaled from the i5-3470T and i7-2640M anchors). The 00:00-05:00 window at about 60% thermal duty cycle therefore buys about **30-40K tokens in plus out** per night (estimate). That is enough for:

- one extraction pass over a typical day (3-10K transcript tokens);
- a verification pass;
- a short journal.

It is not enough for agentic self-editing memory loops or thinking-mode reasoning. Running the dream model in thinking mode is also a poor fit: at about 3 tok/s, thinking tokens would use up the nightly budget. Use thinking off, with grammar-constrained output.

### 2.2 Comparison table

| Model | Size | Released | Licence | Runtime | Evidence | Est. speed here | Fit |
|---|---|---|---|---|---|---|---|
| **Qwen3-4B-Instruct-2507** | 4.02B; Q4_K_M about 2.5 GB (estimate) | 5 Aug 2025 | Apache-2.0 | Mainline; plain transformer, so ordinary KV prefix reuse | Vectara HHEM-2.3 (updated 22 Oct 2026): qwen3-4b **5.7%** hallucination. The board does not say whether that is the 2507 variant | about 3-4 / 3-4 | **First choice** |
| **Granite 4.2 3B** | card says 3B, metadata 4B; about 2.2 GB Q4_K_M | 25 Aug 2026 | Apache-2.0 | Mainline; **thinking on by default**, so set `enable_thinking=False` | IFBench 74.33. **Measured: 4.8 tok/s** generation CPU-only, Q4_K_M, on an old laptop with HD 4000 + GT 720M (CPU not named). The GPU-offloaded runs were slower | about 4-5 decode | Second choice; no faithfulness data for 4.2 yet |
| **LFM2.5-8B-A1B** | 8.3B total / about 1.5B active; 18 conv + 6 GQA layers. Q4_0 4.84 GB, Q4_K_M 5.16 GB | 28 May 2026 | LFM Open License v1.0 (US$10M threshold) | Official GGUF, day-one llama.cpp | Vendor IFEval 91.84. No faithfulness or old-x86 data | 6-10 / 5-10 (no evidence at all on old x86) | Speed option: buys verification and self-consistency, at the cost of about 5 GB more RAM and a non-Apache licence |
| **Qwen3.5-4B** (current plan) | 4.66B incl. vision; Q4_K_M 3.03 GB | 27 Feb 2026 | Apache-2.0 | Mainline; hybrid state; MTP head | **Measured** Pi 5: 3.12 tok/s, rising to 4.83 with tuned MTP (n=3). Every Qwen3.5 size on Vectara: 10.5-12.1% | about 3 | Comparison arm, not the default |
| **Gemma 4 E4B-it** | 4.5B effective / 8B with embeddings | 2 Apr 2026 | Apache-2.0 | Mainline; SWA | Family on Vectara: 5.2-7.4% (no small-variant entry). Slow prompt reading on Pi 5 | about 2-3 | Comparison arm |
| **MiniCPM5-2B** | 2.52B | 7 Sep 2026 | Apache-2.0 | Mainline (llama) | No faithfulness data | about 4-6 | "Light dream" tier or entailment checker; check that thinking can be disabled |
| **Gemma 4 E2B / Qwen3.5-2B** | 2B class | 2026 | Apache-2.0 | Mainline | — | about 5-8 | Hot-night or short-night tier |
| Phi-4-mini, Ministral 3 3B | 3-3.8B | 2025 | MIT / Apache | GGUF | Vectara **23.5% / 24.2%** | — | **Avoid** for faithful summarisation |
| Any 7-9B dense | — | — | — | — | Qwen3-8B 4.8% | about 1.5-2 | Too slow for nightly use |
| Aion-1.0-Instruct-2.3B | 2.29B | 18 Nov 2026 | MIT | Patched llama.cpp only | Tagged for summarisation; provenance questioned by third parties | — | Watch |

### 2.3 Recommended pipeline

Keep the existing gate: asleep, lid closed, on AC, 00:00-05:00, temperature limit, cursor checkpoint, rejection of superseded revisions.

0. **Budget and thermal control.**
   - Store a token budget per night, measured once with llama-bench.
   - Duty-cycle at chunk granularity (1-5 minutes, committed atomically): pause at a high temperature mark, resume at a low one. 80/65 C is a possible starting pair, but the owner chooses the marks.
   - Consider `intel_pstate no_turbo` and an applesmc fan floor (mbpfan lists MacBookPro9,2 as tested).
   - On wake, drop the in-flight chunk, which commits nothing, unload the dream model, and give the conversation model priority.
   - Expect sustained 2-thread load to pass 65 C within minutes. That is inferred from user reports of 75-103 C on this model, not measured.
1. **Deterministic pre-pass (no model).**
   - Drop deleted, superseded and low-confidence ASR segments. Flag low-confidence ones as "heard uncertainly".
   - Tag the speaker of every turn (owner / being / other).
   - Score importance with rules: explicit "remember", corrections, names, dates, repeated topics, emotion words.
   - Following RecMem, send the model only what is important, recurring or flagged.
   - Pack the rest into chunks of 800-1,200 tokens, each line prefixed with its id, e.g. `[E1234|owner|2026-11-23 19:02]`.
   - Today's rule-based dreaming stays as the fallback.
2. **Extraction (model).**
   - Temperature 0, thinking off, `response_format` json_schema.
   - Keep the schema flat, with no `$ref`: up to 8 items per chunk, each with `kind` (enum), `claim` (≤160 chars), `quote` (≤200 chars), `episode_id` (anchored pattern), `speaker` (enum), `modality` (stated / reported / hypothetical / joking / unsure) and `time_ref`.
   - Keep the instruction prefix byte-stable and under 300 tokens so it is reused across chunks.
3. **Deterministic verification (Python stdlib).**
   - `episode_id` must be in this chunk's id set.
   - The normalised quote must be a substring of that episode's text.
   - The speaker must match the stored speaker.
   - The being's own lines can never become owner facts.
   - Reject anything that fails; never repair it.
4. **Entailment check (model).**
   - Premise: the episode turn ±1 turn. Hypothesis: the claim.
   - A grammar forces a one-token answer: SUPPORTED / NOT_SUPPORTED / UNCLEAR. Read the token logprob and calibrate the threshold on the Mac Studio against HHEM-2.1-Open. HHEM needs PyTorch, so it stays a lab-only tool.
   - About 1 minute per claim at about 3 tok/s (estimate).
5. **Merge.**
   - Mem0-style ADD / SUPERSEDE / NOOP, decided by FTS5 match on subject and kind. Ask the model only about ambiguous pairs.
   - New items start as `candidate`. They become `confirmed` when re-extracted from a different episode on a later night, or confirmed by the owner. Owner corrections always win.
   - Never edit a row in place.
   - Conflicts go to an "open questions" list the being may ask about while awake.
6. **Summary tree and themes.**
   - Day digests use verified items only, and every sentence cites item ids.
   - Weekly digests on Sunday nights.
   - Themes are cross-links over items seen on at least 2 days.
   - Every node stores its child ids and the transitive union of its source episode ids.
7. **Dream journal and morning remark.**
   - Built from at most 10 verified items, with every sentence cited.
   - Post-check: names, numbers and dates in a sentence must appear in the cited quotes.
   - Imaginative imagery goes only in a separate creative field that is never read as fact.
   - The morning remark is one tentative sentence, with its source shown on request.
8. **Forgetting contract.**
   - A derivation table: derived_id → source_id plus revision.
   - Forgetting cascades transitively to facts, themes, digest ancestors (deleted and marked dirty for re-derivation), journal entries, FTS rows (secure-delete), vectors, llama.cpp slot-save files and host prompt caches, scratch files, and TTS caches.
   - Backups apply a tombstone list on restore.
   - Test with BEMULA's categories: direct, indirect, inferential, replacement and extraction-attack.

### 2.4 Research techniques: what to take and what to leave

| Work | Take | Leave |
|---|---|---|
| Sleep-time compute (Letta, arXiv 2504.13171) | Precompute a morning brief, open threads and reminders as small source-linked items the waking model can read cheaply | Free-form self-editing of memory blocks: token-hungry and it destroys provenance |
| Generative Agents reflection (2304.03442) | Insights that cite their source records; reflections inherit the transitive source set | Model-scored importance for everything (use rules first) |
| Mem0 (2504.19413), A-MEM (2502.12110) | The ADD/UPDATE/NOOP decision shape | In-place rewriting and note "evolution". BEMULA measured Mem0's forgetting efficacy at 8.5-17% |
| MemoryOS, LightMem (2510.18866) | Tiers (episodes → day topics → durable facts), offline consolidation, filtering first | — |
| RAPTOR (2401.18059) | A fixed calendar tree with dirty-marking on forget | Embedding clustering |
| Auto-Dreamer (2605.20616), RecMem (2605.16045) | "Supersede the region, keep provenance"; consolidate only what recurs (RecMem reports about 87% fewer construction tokens) | Training a consolidator |
| MemIR (2605.25869) | Typed atoms that separate evidence, cues and claims; speaker and modality on every item | — |
| BEMULA (2610.18052), Agentic Unlearning (2602.17692), Forgetting Without Restarting (2609.04875) | Dependency-closure deletion; the "behave as if never seen" test standard | Deletion done by asking the model |
| Speculative decoding with the 0.8B as draft for a 4B Qwen | Worth one test: grammar-constrained JSON is very predictable | — |

---

## 3. CPU inference techniques on Ivy Bridge

### 3.1 What the llama.cpp source says (read on master, 23 Nov 2026)

| Path | AVX1 support? |
|---|---|
| Single-token dot products: Q4_0, Q4_1, Q5_x, Q8_0, all K-quants, IQ4_NL/XS, IQ1-3, MXFP4, Q1_0 | Yes (`__AVX__` branches) |
| TQ1_0, TQ2_0, Q2_0 (ternary; the format Bonsai uses on upstream) | **No.** AVX2 only, otherwise generic scalar code |
| Weight repacking (Q4_0, Q4_K, IQ4_NL, MXFP4 8x8) | **No.** All gated on `ggml_cpu_has_avx2()`, so `--repack` does nothing here |
| llamafile tinyBLAS prompt matmul | Yes for F32, F16 (with F16C), Q8_0, Q4_0, Q5_0, IQ4_NL. **No K-quants on x86** |
| Vulkan on HD 4000 | **No.** Mesa hasvk sets `storageBuffer16BitAccess` only for Gen8+, and llama.cpp throws "Unsupported device". Clover has been removed from Mesa. The one Ivy Bridge GPU measurement (Beignet OpenCL) was slower than CPU only, 2.3 vs 4.8 tok/s |

The shipped Q4_K_M therefore reads its prompt through the generic path, while Q4_0, IQ4_NL and Q8_0 use tinyBLAS. But the mgroeber anchor (b6878, tinyBLAS on, Q4_0) still read prompts no faster than it generated. **Expect a small gain at best, and measure it.**

### 3.2 Techniques, ranked by expected value

| # | Technique | Expected value here |
|---|---|---|
| 1 | **Warm, byte-stable prefix.** `--slot-save-path` with `/slots` save/restore, `--cache-ram`, `--ctx-checkpoints` (default 32; never 0, because 0 re-prefills every turn: #32316). Lower `--checkpoint-min-step` (default 8192) so a checkpoint lands at the end of the stable prefix | Largest. **Caveat:** #28194 (open) says `/slots restore` gives no reuse on hybrid, recurrent and SWA models because checkpoints are not saved with the slot; #32568 (open) covers missed shared-prefix checkpoints. The project's LAB.md reports successful warm reuse after restore, so find out which code path the workaround relies on before changing builds. A pure-attention model (MiniCPM5) avoids this whole family of bugs |
| 2 | **Fewer prompt tokens; variable content last.** On the Mac, removing body telemetry saved about 70-80 ms; on this CPU the saving would be roughly 20x larger (estimate) | Large and free. Also compare tokenizer efficiency across candidates on the real prompt |
| 3 | **Quant choice.** Bench Q4_K_M, Q4_0 (vendor QAT/QAD where one exists), IQ4_NL and Q8_0. Q8_0 may decode about as fast, because this CPU is compute-bound. Check the output-tensor quant with gguf-dump; Qwen3.5-0.8B's 248K x 1024 head is about a third of its per-token work | Small to moderate |
| 4 | **Threads.** `--threads 2` pinned one per physical core (`--cpu-mask`/`--cpu-strict`); test `--poll 0` so spinning does not starve face, ASR and TTS; test `--threads-batch 3-4` for prompt reading while the owner is silent. Measure with speech running | Moderate |
| 5 | **Model size by compute, not bandwidth.** Bandwidth ceilings (about 28-38 tok/s for 0.53 GB) are only upper bounds here | Guides model choice |
| 6 | **Speculative decoding, MTP, n-gram.** Verifying k drafted tokens costs about k decode steps when pp ≈ tg. On a 4-core Pi 5, tuned MTP helped (Gemma 4 E2B 1.45x, Qwen3.5-4B 1.55x at n=3) | Probably none. Skip unless llama-bench at batch 2-4 is at least 2x faster per token than batch 1 |
| 7 | KV-cache quantisation | None: KV is tens of MB at 4K context; keep f16 (F16C) |
| 8 | `--load-mode mmap+mlock` | Keeps the model resident overnight; huge pages give at most a few percent (estimate) |
| 9 | ik_llama.cpp, bitnet.cpp, T-MAC | **Unusable.** ik_llama.cpp says "AVX2 or better", its AVX1 fix PR #2138 has been open since July, and run-time repack garbles output on AVX1. bitnet.cpp has empty bodies without AVX2. T-MAC targets `core-avx2` and was last updated June 2025 |
| 10 | **Build hygiene.** `GGML_NATIVE=OFF`, SSE4.2/AVX/F16C on, AVX2/FMA/BMI2/AVX-512 off (already in cortex.mk), `GGML_LLAMAFILE=ON`. Upstream ivybridge CPU variant: #17966 / PR #18186. Check that the system_info line shows `AVX = 1, F16C = 1, AVX2 = 0, LLAMAFILE = 1` | Required |

**Pinned build check.** v1.0.0 (13 Nov 2026, b12359) postdates several fixes this report relies on:
- LFM3 support (PR #31252, 27 Oct);
- the Qwen3.5 LoRA conversion fix (PR #28324, 30 Sep);
- full ModernBERT (PR #18330, 19 Feb);
- EmbeddingGemma 2 (PR #30054, 6 Oct);
- Qwen3.5 vision (PR #19468, 10 Feb).

It should include all of them, but confirm locally. A possible vision-encode regression of about 2.7x (#27649, closed as stale with no fix) may be present.

---

## 4. Speech

### 4.1 Comparison table

| Component | Name / size | Released | Licence | Runtime | Evidence | Est. here | Fit |
|---|---|---|---|---|---|---|---|
| ASR | **Moonshine v2 Small Streaming** 123M; Tiny 34M (26M in another table); Medium 245M | Paper 12 Feb 2026; moonshine-voice 0.1.6 (6 Nov 2026) | MIT (current README: all sizes and languages; only legacy non-streaming non-English models are non-commercial) | C++ on ONNX Runtime; bundled Silero VAD and speaker ID | Vendor table (WER is OpenASR-Leaderboard average; latency after VAD end-of-phrase): Small 7.84% at 527 ms on Pi 5; Tiny 12.0% at 237 ms; Whisper Tiny 5,863 ms on the same Pi. The table survives only in a fork of the official README | Small 0.5-1.0 s; Tiny 0.25-0.5 s | **Recommended.** Re-run its own benchmark script on the laptop |
| ASR alt | NVIDIA Parakeet Realtime EOU 120M | 2025-26 | NVIDIA Open Model License | NeMo; sherpa-onnx support unofficial (#2805, poor-quality reports) | 9.30% average WER; end-of-utterance token at 160 ms p50 | similar to Moonshine Small | Not practical in sherpa-onnx yet |
| ASR fallback | sherpa-onnx streaming Zipformer 20M | 2023 | Apache-2.0 | sherpa-onnx | "Real time on Pi 4" | light | Fallback; same engine as KWS and speaker ID |
| ASR watch | Earlybird-29M | 10 Nov 2026 | model licence unknown | unknown | Single author; claims to beat Moonshine Tiny; ties Whistle | — | Watch |
| ASR watch | Whistle (Cactus) 16.9 MB | 2 Oct 2026 | Apache-2.0 (card) | CPU single file | LibriSpeech clean 4.31%; AMI 26%; not streaming | — | Tiny offline fallback |
| ASR reject | whisper.cpp tiny/base, Vosk small, Kyutai STT 1B+, Parakeet-TDT-0.6B, Arc Mini (CC BY-NC) | — | — | — | Whisper pads to 30 s windows and does not stream; tiny is about 2-5 s per utterance here (estimate) | — | Skip |
| VAD | **Silero VAD v6** about 2 MB | 2025-26 | MIT | ONNX | about 0.2 ms per 32 ms chunk | under 1% of a core | Recommended. TEN VAD: Apache plus extra conditions; avoid |
| TTS | **Piper** medium voice (e.g. en_US-lessac-medium, about 63 MB) | piper1-gpl active (v1.8.0 per secondary sources) | **GPL-3.0** (embeds espeak-ng); each voice has its own licence | ONNX; libpiper or sherpa-onnx | **Measured** Pi 4 RTF 0.774 / 0.482 / 0.390 / 0.357 at 1-4 threads | RTF 0.3-0.5 on one thread | **Recommended now.** If images are ever distributed, run it as a separate GPL process and ship the source |
| TTS bench | **tiny-voca** 4.5M; ONNX 21.4 MB, GGUF Q4_0 8.7 MB | 14-16 Nov 2026 | Apache-2.0; "dizione" phonemizer, no espeak-ng | ONNX / GGUF | Author: about 70x real time on 4 threads; 8th and 13th of 50 English models on the Open TTS Leaderboard | very low (estimate) | **Bench first**; it could remove the GPL dependency |
| TTS bench | **KittenTTS 2 nano** 17M / pico 7M | 2026 | Apache-2.0 including the bundled G2P (engine not named) | ONNX (int4 needs ORT 1.20+) | Vendor: pico faster than real time on one phone thread | RTF well under 0.5 (estimate) | Bench |
| TTS bench | Gradium Phonon 31M English, model.q8.gguf 64.5 MB | about 20 Oct 2026 | Apache-2.0 (40M multilingual: CC BY-NC) | Python / Rust / WASM, streaming | No CPU numbers | — | Bench |
| TTS | Kyutai Pocket TTS 100M; english 18M/7M/2M (Oct 2026) | Jan / Oct 2026 | Code MIT; **weights CC-BY-4.0, gated** | PyTorch; Rust (AVX2 build plus an SSE4.2 "-v2" build at the same default speed) | 4-vCPU x86 VM: 2.3-2.5x real time | 100M: RTF 1.5-3 (too slow); tiny variants borderline | Watch |
| TTS | Supertonic 2/3 66M/99M | 6 Jan / 29 Apr 2026 | OpenRAIL-M weights | ONNX | 4-core Xeon RTF 0.121 at 2 steps | RTF 0.4-0.8 | **Repo archived, development ended.** Not recommended |
| TTS overnight | Kokoro-82M | 2025 | Apache weights; espeak-ng (GPL) in common runtimes | ONNX | **Measured** Pi 4 RTF 6.6 (1 thread) / 2.8 (4 threads) | RTF 1.5-3 | Overnight pre-rendering of stock phrases only |
| TTS reject | NeuTTS Air/Nano, KittenTTS 2 1.7B | 2025-26 | various | llama.cpp backbone | competes with the LLM | — | Skip |
| Speaker ID | **3D-Speaker CAM++** 7.2M (ONNX 29.6 MB) | 2023-24 | Code Apache-2.0; check each ModelScope checkpoint | sherpa-onnx speaker-embedding API | VoxCeleb1-O EER 0.65% (ERes2Net-base 0.84%) | tens of ms per embedding | Recommended |
| Wake | sherpa-onnx open-vocabulary KWS (about 3M) | — | Apache-2.0 | sherpa-onnx | No published false-accept rates | small | Optional. openWakeWord's pretrained models are CC-BY-NC-SA; avoid them |

### 4.2 Techniques

- Streaming encoder ASR, so the work overlaps the owner's speech.
- Sentence-level TTS streaming from LLM output. Try a thread split of llama `-t 3` plus TTS `-t 1`, and measure it.
- **Measure fp32 against int8 for every ONNX model.** Without AVX2 or VNNI, int8 can be slower.
- Build ONNX Runtime and sherpa-onnx from source with `-march=ivybridge`. The design already lists "ONNX Runtime without AVX2" as a risk, so verify this early.
- Listening gated on gaze plus VAD instead of an always-armed wake word.
- Owner speaker enrolment with 5-10 utterances, a cosine threshold and an "unknown/guest" class. Embeddings carry provenance links so they can be deleted.
- Character voice by light DSP (pitch or formant shift, comb filter), which also makes it clear the being is a machine.
- Overnight pre-rendering of common phrases with a better model; cached audio carries provenance.

---

## 5. Semantic recall

### 5.1 Comparison table

| Model | Size | Released | Licence | Runtime | Quality | Est. per query here | Fit |
|---|---|---|---|---|---|---|---|
| **granite-embedding-small-english-r2** | 47M, 384-d, 8K context | 15 Aug 2025 | Apache-2.0 | llama.cpp ModernBERT: PR #15641 plus full support in PR #18330 (19 Feb 2026), so it should be in v1.0.0 | MTEB-v2 retrieval 53.9 (same as bge-small); long-document retrieval better | 40-80 ms | **Default**, after a parity test (cosine above 0.99 against sentence-transformers) |
| **bge-small-en-v1.5** | 33M, 384-d | Sep 2023 | MIT | llama.cpp BERT (mature) | MTEB-v2 retrieval 53.9 | 40-60 ms | Fallback |
| snowflake-arctic-embed-xs | 22M | Apr 2024 | Apache-2.0 | llama.cpp BERT | MTEB v1 retrieval 50.15 | 25-35 ms | If the budget is very tight; test calibration on paraphrases |
| all-MiniLM-L6-v2 | 22.7M | 2021 | Apache-2.0 | llama.cpp | retrieval 41.95 | 20-35 ms | Speed baseline only (the model DESIGN-AND-PLAN.md currently names) |
| **blink-8M** (model2vec static) | 7.7M, 256-d | 2026 | MIT | Pure Python is possible | MTEB 55.35; retrieval 38.86 | 1-3 ms | **Phase 0**, no model process. Weak on negation |
| static-retrieval-mrl-en-v1 | 30.5K x 1024, Matryoshka | — | Apache-2.0 | Pure lookup | NanoBEIR 0.503 | 1-3 ms | Phase 0 alternative; put it in the A/B |
| EmbeddingGemma 2 (text path) | 270M; Q8_0 310 MB | about Oct 2026 | Apache-2.0 (the card's mention of the Gemma prohibited-use policy is unverified) | llama.cpp PR #30054 (6 Oct), so it should be in v1.0.0 | MTEB eng v2 68.46 (67.78 at 256-d) | 250-400 ms | Quality ceiling for the A/B; adopt only if measured under about 250 ms |
| MongoDB mdbr-leaf-ir | 23M (109M teacher) | Sep 2025 | Apache-2.0 | No GGUF | BEIR 53.55 | — | Phase 2 idea: embed documents overnight with the teacher, queries with the student |
| Rejected | Qwen3-Embedding-0.6B (1.2-1.8 s per query), Harrier-270M, LFM2.5-Embedding-350M (LFM licence), Jina v5 nano (CC BY-NC, forked llama.cpp), nomic v1.5/v2, EmbeddingGemma 1 (Gemma terms) | | | | | | Too slow or wrong licence |
| Rerankers | ms-marco-MiniLM-L6, jina-reranker-v1-tiny, gte-reranker-modernbert-base | | Apache-2.0 | `llama-server --reranking` | — | 300-600 ms for the top 10 | Not on the speaking path; possibly for overnight dream linking |

### 5.2 Design

- **Storage.** `episode_vec(episode_id PRIMARY KEY REFERENCES episodes ON DELETE CASCADE, src_rev, model_id, dim, q8 BLOB, bits BLOB)`.
  - Turn on `PRAGMA foreign_keys=ON`, `PRAGMA secure_delete=ON`, and FTS5 `secure-delete=1`. FTS5 secure-delete needs **SQLite 3.42 or later**; check Buildroot's version.
  - After each forget, run `wal_checkpoint(TRUNCATE)`.
  - Evict in-RAM codes and caches on forget.
  - Treat vectors as personal text, because inversion attacks exist.
- **Search.**
  - Under about 5K rows: brute force over int8 or `array('f')` with `math.sumprod`.
  - Above that: a Hamming prefilter on sign bits (`int.bit_count`, 100K vectors in about 15-35 ms, estimate), then exact rescoring of the top 200.
  - Fuse the FTS5 bm25 top 50 with the vector top 50 by RRF (k = 60).
  - Apply a cosine floor, so weak matches return "I don't recall" rather than a guess.
  - (Pure-Python timings were measured on an M4 Max and scaled by an assumed 4-6x; that scaling is an estimate.)
- **Serving.** A second llama-server with `--embedding`, F16 or Q8_0 (not Q4 on a model this small), `-t 2 -c 512`, bound to localhost. Embed the final utterance during the gap while ASR finalises.
  - The ggml-org EmbeddingGemma 2 card now shows a `llama serve` command. Check that v1.0.0 still provides `llama-server` with the same flags, and how its server lockdown changes treat a second localhost server.
- **Avoid sqlite-vec.** Python's stdlib sqlite3 cannot load extensions on Buildroot (`--enable-loadable-sqlite-extensions` is off). Its Makefile adds `-mavx2` whenever the build host shows AVX (unless `OMIT_SIMD`). It frees deleted vectors only when a whole chunk of about 1,024 empties, and 0.1.10-alpha.2 fixed a deletion data leak.
- **Night work.** Re-embed rows whose `src_rev` changed, and propose dream links from vector neighbours. Bulk-embedding 100K short sentences with bge-small would take about 45-70 minutes (estimate).

---

## 6. Vision

### 6.1 Comparison table

| Component | Name | Released | Licence | Runtime | Evidence | Est. here | Fit |
|---|---|---|---|---|---|---|---|
| Detector | **YuNet** (`2023mar` for OpenCV 4.x; `2026may` is a dynamic-input re-export for the OpenCV 5 engine) | 2023 / 2026 | MIT | OpenCV DNN, libfacedetection | **Measured:** 160x120 at 0.69 ms on an i7-12700K, 6.23 ms on a Pi 4B | 3-8 ms at 160x120; 12-30 ms at 320x240 | Recommended; its 5 landmarks give alignment and head pose |
| Recogniser | **SFace** (128-d) | Dec 2021 | Apache-2.0 | OpenCV `FaceRecognizerSF` | Accuracy 0.994 (dataset not named, probably LFW). **Measured** 68.8 ms on a Pi 4B | 30-90 ms per face | Recommended; run once per face track |
| Recogniser | EdgeFace XS/XXS | 2023 | Code BSD-3, **weights CC BY-NC-SA 4.0** | PyTorch | LFW 99.57-99.73 | under 20 ms | Licence conflict |
| Recogniser | InsightFace buffalo_s/l | 2021-22 | **Models non-commercial**; licences now by email request | ORT | — | — | Avoid |
| VLM | **Qwen3.5-0.8B + mmproj-F16** (about 205 MB; 12-layer ViT, width 768, patch 16, 2x2 merge) | Feb/Mar 2026 | Apache-2.0 | llama.cpp mtmd (PR #19468, merged 10 Feb 2026). No Q8_0 projector published; make one locally if wanted | Non-thinking: MMStar 55.9, OCRBench 79.1, RealWorldQA 61.6 | 4-10 s at 256-320 px; 10-30 s at 448 px | **Recommended**, in a separate server |
| VLM | LFM2.5-VL-450M | late 2025 / 2026 | LFM | Official GGUF | MMStar 43.0 | a few seconds less | Fallback only |
| VLM | LFM3-VL-2B (1.92B) | 27 Oct 2026 | LFM | GGUF, b11960+ | **Non-thinking** MMStar 62.2, OCRBench 82.2 (the thinking-mode 74.4 is not a fair comparison) | 2-3x Qwen | Too slow |
| VLM | SmolVLM 256M/500M | 2025 | Apache-2.0 | llama.cpp | MMStar 34.6/38.3 | 7-20 s per 512 tile | Superseded |
| VLM reject | Gemma 4 E2B vision, Moondream 2/3/3.1, Florence-2, InternVL3-1B | | | | | | Heavier, weaker, no runtime, or restrictive licence |
| Expression | HSEmotion enet_b0 | 2021-22 | Code Apache-2.0; weights trained on AffectNet (research-only dataset) | ONNX | AffectNet 8-class about 61% (not re-verified) | 50-100 ms | Skip by default |

### 6.2 Techniques

- **Separate on-demand vision server.** llama-server returns HTTP 501 for slot save/restore when `--mmproj` is loaded. Issue #19466 was closed as stale, still unfixed. MTP and speculative decoding are also refused with mmproj.
  - Launch a second server only for a "look": same mmapped GGUF, plus the mmproj, `-np 1 -c 1024`, `--image-max-tokens 64-196`.
  - Show a "looking" face state and use a spoken filler while it works.
- **Image token budget.** Downscale and crop to 256-448 px yourself. 448 px is about 196 image tokens; 256 px is about 64. Log "image slice encoded in N ms" separately from prefill. Benchmark the v1.0.0 build against an older tag because of #27649.
- **Honest description.** One or two literal sentences, "I can't tell" allowed, no guessing identity, age or emotion. Store only a provenance-linked text description, never the image. Use non-thinking mode.
- **Owner template.**
  - Opt-in enrolment of 5-10 captures; store embeddings only, encrypted, deletable via provenance.
  - Decide by majority over 3-5 frames, with an "unsure" band; re-tune the threshold on the owner's own webcam frames.
  - Templates can be inverted into recognisable faces (Idiap research), so never send them off-device.
  - There is no liveness check, so a photo would pass. It personalises greetings only and never authenticates anything.
- **Free cues.**
  - applesmc ambient light sensor (ALV0/ALV1; check that it returns real values on this model under kernel 6.18).
  - Frame difference at 2-5 fps on 160x120 greyscale, used to gate everything else.
  - "Looking at me" from YuNet landmarks.
- **Build without AVX2.** OpenCV with `CPU_BASELINE=SSE4_2`, `CPU_DISPATCH=SSE4_1;SSE4_2;AVX;FP16`, and only the core, imgproc, dnn and objdetect modules.
- **Expression.** If used at all: smile/neutral only, driving the eyes briefly, never stored, never spoken as fact. Its scientific validity is contested (Barrett et al. 2019).

---

## 7. Character and persona

### 7.1 Model notes for the character

- **Qwen3.5-0.8B** stays the default. Its sampler card says temp 1.0, top_p 1.0, top_k 20, presence 2.0 for non-thinking mode. For persona chat, test temp 0.7-0.8 with presence 1.0-1.5 (a suggestion, not from the card; the card's vision preset happens to be 0.7 / 0.8 / 1.5).
- **MiniCPM5-1B** should be auditioned (it was missing from the persona survey).
- **MiniCPM5-2B** and **Gemma 4 E2B** are possible step-ups if Phase 1 shows headroom. MiniCPM5-2B has 42 thin layers, which may hurt more on 2 cores than its Pi 5 lead suggests.
- **LFM2.5** is weaker on licence fit and its card is tuned for low temperature (0.1).
- **Aion** needs a patched runtime.

### 7.2 Techniques

| Technique | Recommendation |
|---|---|
| Persona prefix | About 200 tokens, byte-stable: name, 3-5 traits, voice rules, 2-3 tiny example exchanges. After it: a code-computed self/body block of about 40 tokens (age, battery, temperature, lid state, hours since the owner was last seen, low ASR confidence), retrieved memories with episode ids, recent turns. Watch the logs for "forcing full prompt re-processing" |
| GBNF | Tighten the existing `mind/speech.py` grammar: tag + 1-2 sentences, no newlines, markdown, asterisks or emoji; `max_tokens` about 48-64. Grammar guarantees the format, not label accuracy, so measure accuracy on fixtures |
| **Persona LoRA** | 30-50 handwritten seed dialogues expanded to 1,500-3,000 multi-turn dialogues, every turn tagged. Include honest "didn't hear" / "don't know" turns and anti-sycophancy turns. Unsloth bf16 LoRA (about 3 GB VRAM for 0.8B; QLoRA not recommended for Qwen3.5), r = 16, alpha = 16, all projections, exactly the inference chat template, non-thinking only. Convert with `convert_lora_to_gguf.py` (Qwen3.5 crash fixed by PR #28324), A/B with `--lora-scaled`, then merge and requantise. **Never train on owner data**, because weights cannot honour real forgetting. Unsloth on Apple silicon and MLX support for qwen3_5 were not verified; a CUDA GPU or Colab may be needed |
| Data generator licence | Use **Qwen3.8-27B** (Apache-2.0) on the Mac Studio. Qwen3.8-Flash-Next is under `qwen-community-1.0`, a custom licence: read it before using it as a generator. Frontier API terms restrict using outputs to build competing models. Record the generator and prompts in a manifest |
| Anti-boilerplate | Never call it an "assistant". Include adversarial turns in training. A regex post-filter on the clause stream ("As an AI", "How can I assist") triggers one regeneration, then a canned in-character line. Use logit_bias sparingly |
| Grounded self-model | PAI-Bench (Sep 2026, preliminary): identity facts were recalled when asked directly 48/48 but used unprompted only 1/48; explicit field cues raised that from 0/8 to 7/8. So name the fields and supply them from code only |
| Control vectors | **Not for v1.** #32341 (open): steering is written into the KV cache and compounds, there is no runtime scale, and the cache is not invalidated when the vector changes. #32503 (open): the generator places vectors one block too deep (seen on qwen35moe). Express mood through the tag, the face and a one-line mood field instead |
| Face does the work | The Anki Cozmo/Vector lesson: families of 3-5 parametric eye variants per emotion with jitter, saccade glance-aways while "thinking" to cover latency, sub-100 ms reflexes to its name or a face, idle curiosity and boredom. HRI evidence is modest (small, single studies) but consistent |
| Persona eval | Extend `lab/model_audition.py`: multi-turn probes, rate of invented answers against empty memory, boilerplate rate, tag validity and accuracy, reply length. Use a fixed local judge (judge choice alone moved PAI-Bench scores by 12.5 points) |

---

## 8. What must be measured on the real laptop first

1. **CPU model** (`/proc/cpuinfo`): i5-3210M or i7-3520M; flags show `avx f16c` and no `avx2`.
2. **llama-bench**, with speech idle and then running:
   - `-t 2` and `-t 4`;
   - `-p 16,64,128,256 -n 32,64`;
   - Qwen3.5-0.8B as Q4_K_M, Q4_0, IQ4_NL and Q8_0;
   - then MiniCPM5-1B, LFM2.5-1.2B QAD-Q4_0, Qwen3.5-2B and LFM3-1.7B;
   - batch 1 vs 2-4 per-token cost (decides speculation and MTP);
   - `--poll 50` vs `--poll 0`.
3. **System info line:** `AVX = 1 | F16C = 1 | AVX2 = 0 | LLAMAFILE = 1`, and that v1.0.0 loads lfm3, the ModernBERT embedder, the Qwen3.5 mmproj and LoRA GGUFs.
4. **Cache behaviour per model:** a warm-prefix hit after `/slots restore` and after reboot (#28194); rollback when an ASR partial is revised; no "forcing full prompt re-processing" in normal turns.
5. **End-to-end time to first audio** (p50/p95) with warm prefix, read-ahead, Moonshine and Piper all running, compared with the 3 s target.
6. **Thermals:** sustained 2-thread inference with the lid closed. Time to 65/80 C, throttling, the effect of no_turbo and a fan floor, and tokens per minute over an hour.
7. **ONNX Runtime and sherpa-onnx built without AVX2:** Moonshine Small/Tiny latency, Piper/tiny-voca/Kitten RTF on one thread, fp32 vs int8.
8. **Embedder:** parity test and per-query latency for granite-r2 / bge-small / EmbeddingGemma 2; RSS.
9. **Vision:** YuNet and SFace times; mtmd encode time at 256/320/448 px.
10. **Small checks:** applesmc light values; SQLite version 3.42 or later; that the Moonshine benchmark script runs.

## 9. Suggested order of work

1. Measurements 1-3 (half a day). These replace most of the estimates in this report.
2. Cheap inference wins: quant swap if it measures faster, thread pinning and `--poll`, a shorter and stable prompt, checkpoint settings.
3. Audition the waking models with `lab.model_audition` in the order MiniCPM5-1B → LFM2.5-1.2B → Qwen3.5-2B → LFM3-1.7B. Promote one only if it clearly beats 6/24 complete replies **and** keeps p50 first audio at about 3 s or less.
4. Speech swap: Silero + Moonshine Small streaming + Piper (bench tiny-voca and Kitten nano), then CAM++ enrolment.
5. Tighter GBNF, boilerplate filter, persona eval harness, face behaviours.
6. Recall Phase 0 (blink-8M + RRF + cosine floor + cascading vector deletes), then Phase 1 (granite-r2).
7. Overnight pipeline, stages 0-3 (no model beyond extraction) with Qwen3-4B-2507, plus thermal duty-cycling. Then entailment, merge, summary tree and journal. Benchmark Granite 4.2 3B and LFM2.5-8B-A1B on the same held-out nights.
8. Persona LoRA on whichever waking model wins (synthetic data only).
9. Vision: free cues → YuNet + SFace → on-demand Qwen3.5 vision server.
10. Re-check for open Qwen4 small weights and for mainline Aion support.

## 10. Open uncertainties

- No measurement of any model on this CPU exists. All laptop speeds are estimates (±30% or more).
- Vendor quality numbers come from mixed harnesses and thinking modes. There is no persona benchmark covering the 0.8-2B candidates.
- The status of the hybrid-model checkpoint and restore bugs at the pinned build is unknown, and it conflicts with the project's own LAB results.
- LFM3 and Granite 4.2 may leak thinking even when it is switched off.
- Vectara's qwen3-4b entry may be the original Qwen3-4B, not the 2507 variant.
- The Moonshine table comes from a fork of the official README. Licences still to confirm: Pocket TTS voices, KittenTTS G2P engine, 3D-Speaker checkpoints, HSEmotion weights.
- Qwen4 small model status is from secondary sources only.
- 2026 memory and forgetting papers (BEMULA, MemIR, Auto-Dreamer and others) are unreviewed preprints.

## 11. Sources

**Hardware and runtime**
- mgroeber.de legacy benchmark: https://www.mgroeber.de/benchmarking-llamacpp-on-legacy-hardware-2.html
- llama.cpp x86 quants: https://github.com/ggml-org/llama.cpp/blob/master/ggml/src/ggml-cpu/arch/x86/quants.c
- repack.cpp: https://github.com/ggml-org/llama.cpp/blob/master/ggml/src/ggml-cpu/repack.cpp
- sgemm.cpp: https://github.com/ggml-org/llama.cpp/blob/master/ggml/src/ggml-cpu/llamafile/sgemm.cpp
- Server README: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- Grammars README: https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md
- Issues: #28194 https://github.com/ggml-org/llama.cpp/issues/28194 · #32568 https://github.com/ggml-org/llama.cpp/issues/32568 · #32316 https://github.com/ggml-org/llama.cpp/issues/32316 · #21831 https://github.com/ggml-org/llama.cpp/issues/21831 · #22384 https://github.com/ggml-org/llama.cpp/issues/22384 · #19466 https://github.com/ggml-org/llama.cpp/issues/19466 · #19712 https://github.com/ggml-org/llama.cpp/issues/19712 · #27649 https://github.com/ggml-org/llama.cpp/issues/27649 · #21125 https://github.com/ggml-org/llama.cpp/issues/21125 · #32341 https://github.com/ggml-org/llama.cpp/issues/32341 · #32503 https://github.com/ggml-org/llama.cpp/issues/32503 · #17966 https://github.com/ggml-org/llama.cpp/issues/17966
- PRs: #31252 https://github.com/ggml-org/llama.cpp/pull/31252 · #19468 https://github.com/ggml-org/llama.cpp/pull/19468 · #15641 https://github.com/ggml-org/llama.cpp/pull/15641 · #18330 https://github.com/ggml-org/llama.cpp/pull/18330 · #30054 https://github.com/ggml-org/llama.cpp/pull/30054
- v1.0.0 release: https://github.com/ggml-org/llama.cpp/releases/tag/v1.0.0
- Mesa hasvk: https://gitlab.freedesktop.org/mesa/mesa/-/blob/main/src/intel/vulkan_hasvk/anv_device.c
- BitNet: https://github.com/microsoft/BitNet · T-MAC: https://github.com/microsoft/T-MAC · ik_llama.cpp: https://github.com/ikawrakow/ik_llama.cpp
- Old-laptop Granite 4.2 / OpenCL measurement: https://github.com/nipegun/llama.cpp-opencl
- Pi 5 MTP article: https://mjrovai.com/articles/slm-on-raspberry-pi-mtp/ · pi5-llm-arena: https://github.com/leemailto2008/pi5-llm-arena · Pi 4/5 article: https://simeononsecurity.com/articles/ai-models-raspberry-pi-4-5/ · peppe8o: https://peppe8o.com/install-llama-cpp-raspberry-pi/ · note.com Pi 5 bench: https://note.com/sakuya_mikagami/n/na61e0c077d17?hl=en · Google Pi 5 / LiteRT: https://developers.googleblog.com/mastering-edge-ai-on-raspberry-pi-with-litert-and-gemma/

**Language models**
- https://huggingface.co/Qwen/Qwen3.5-0.8B · https://huggingface.co/unsloth/Qwen3.5-0.8B-GGUF · https://huggingface.co/Qwen/Qwen3.5-2B · https://huggingface.co/ggml-org/Qwen3.5-4B-GGUF · https://artificialanalysis.ai/articles/qwen3-5-small-models
- https://huggingface.co/openbmb/MiniCPM5-1B · https://huggingface.co/openbmb/MiniCPM5-1B-GGUF · https://huggingface.co/openbmb/MiniCPM5-2B-GGUF · https://github.com/openbmb/minicpm
- https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct · https://huggingface.co/LiquidAI/LFM3-1.7B · https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF · https://www.liquid.ai/blog/lfm3 · https://www.liquid.ai/blog/lfm2-5-8b-a1b · https://docs.liquid.ai/lfm/help/model-license · https://www.liquid.ai/blog/lfm2-5-350m-no-size-left-behind
- https://huggingface.co/google/gemma-4-E2B-it · https://huggingface.co/google/gemma-4-E4B-it · https://ai.google.dev/gemma/docs/integrations/llamacpp
- https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507 · https://huggingface.co/ibm-granite/granite-4.2-3b · https://huggingface.co/blog/ibm-granite/granite-4-nano
- https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-4GB · https://huggingface.co/microsoft/Aion-1.0-Instruct-2.3B · https://huggingface.co/prism-ml/Ternary-Bonsai-2-2B-gguf
- https://huggingface.co/Qwen/Qwen3.8-27B · https://huggingface.co/Qwen/Qwen3.8-Flash-Next
- Tiny-model index: https://artificialanalysis.ai/models/open-source/tiny · Vectara leaderboard: https://github.com/vectara/hallucination-leaderboard
- Qwen4 status (secondary): https://local-ai-zone.github.io/blog/November_2026_AI_Model_Updates.html · https://manifold.markets/Terminator2/will-alibaba-publish-open-weights-f

**Memory, dreaming, forgetting**
- Sleep-time compute: https://arxiv.org/abs/2504.13171 · Generative Agents: https://arxiv.org/abs/2304.03442 · Mem0: https://arxiv.org/abs/2504.19413 · A-MEM: https://arxiv.org/abs/2502.12110
- MemoryOS: https://arxiv.org/abs/2506.06326 · LightMem: https://arxiv.org/abs/2510.18866 · RAPTOR: https://arxiv.org/abs/2401.18059 · HippoRAG: https://arxiv.org/abs/2405.14831
- Auto-Dreamer: https://arxiv.org/abs/2605.20616 · Complementary curation and consolidation: https://arxiv.org/abs/2610.39790 · RecMem: https://arxiv.org/html/2605.16045 · MemIR: https://arxiv.org/abs/2605.25869
- BEMULA: https://arxiv.org/html/2610.18052v1 · Agentic Unlearning: https://arxiv.org/abs/2602.17692 · Forgetting Without Restarting: https://arxiv.org/pdf/2609.04875
- HHEM-2.1-Open: https://huggingface.co/vectara/hallucination_evaluation_model · TrueBrief: https://arxiv.org/abs/2601.04212 · Self-consistency: https://arxiv.org/abs/2203.11171
- mbpfan: https://github.com/linux-on-mac/mbpfan · MacRumors thermal thread: https://forums.macrumors.com/threads/mid-2012-macbook-pro-13-spiking-to-103c-when-doing-anything-intensive.2385704

**Speech**
- Moonshine: https://github.com/moonshine-ai/moonshine · https://github.com/nxpatterns/moonshine-voice · https://arxiv.org/abs/2602.12241 · https://petewarden.com/2026/02/13/announcing-moonshine-voice/
- sherpa-onnx VITS/Piper tables: https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/vits.html · Kokoro tables: https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html · KWS: https://k2-fsa.github.io/sherpa/onnx/kws/index.html
- https://github.com/OHF-Voice/piper1-gpl · https://huggingface.co/n4ze3m/tiny-voca · https://huggingface.co/KittenML/kitten-tts-2-nano · https://huggingface.co/gradium/phonon-31m-en · https://github.com/kyutai-labs/pocket-tts · https://huggingface.co/kyutai/pocket-tts · https://github.com/supertone-oss-archive/supertonic
- https://huggingface.co/nvidia/parakeet_realtime_eou_120m-v1 · https://github.com/k2-fsa/sherpa-onnx/issues/2805 · https://arxiv.org/abs/2611.14421 · https://amical.ai/blog/tiny-speech-models
- https://github.com/modelscope/3D-Speaker · https://huggingface.co/davidscripka/openwakeword · https://github.com/ten-framework/ten-vad · https://heyneo.com/blog/kokoro-supertonic-inflect-nano-pocket-tts-cpu-benchmark

**Recall**
- https://huggingface.co/ibm-granite/granite-embedding-small-english-r2 · https://huggingface.co/CompendiumLabs/bge-small-en-v1.5-gguf · https://huggingface.co/Snowflake/snowflake-arctic-embed-xs · https://huggingface.co/minishlab/blink-8M · https://huggingface.co/sentence-transformers/static-retrieval-mrl-en-v1
- https://huggingface.co/google/embeddinggemma-2 · https://huggingface.co/ggml-org/embeddinggemma-2-GGUF · https://huggingface.co/Qwen/Qwen3-Embedding-0.6B · https://huggingface.co/MongoDB/mdbr-leaf-ir
- https://huggingface.co/blog/embedding-quantization · https://github.com/asg017/sqlite-vec/releases · https://raw.githubusercontent.com/asg017/sqlite-vec/main/Makefile · https://docs.python.org/3/library/sqlite3.html · https://gitlab.com/buildroot.org/buildroot/-/raw/master/package/python3/python3.mk · https://sqlite.org/fts5.html · https://sqlite.org/pragma.html#pragma_secure_delete · https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf

**Vision**
- https://github.com/opencv/opencv_zoo/blob/main/benchmark/README.md · https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet · https://huggingface.co/opencv/face_recognition_sface · https://github.com/ShiqiYu/libfacedetection
- https://huggingface.co/Idiap/EdgeFace-XXS · https://raw.githubusercontent.com/deepinsight/insightface/HEAD/README.md
- https://huggingface.co/LiquidAI/LFM2.5-VL-450M · https://huggingface.co/LiquidAI/LFM3-VL-2B · https://huggingface.co/blog/LiquidAI/lfm3 · https://arxiv.org/html/2504.05299 · https://raw.githubusercontent.com/ggml-org/llama.cpp/HEAD/docs/multimodal.md · https://huggingface.co/moondream/moondream3-preview
- https://codebrowser.dev/linux/linux/drivers/hwmon/applesmc.c.html · https://github.com/av-savchenko/hsemotion · https://publications.idiap.ch/attachments/papers/2024/OtroshiShahreza_ICASSP-2_2024.pdf

**Character**
- https://unsloth.ai/docs/models/qwen3.5/fine-tune · https://www.anthropic.com/research/persona-vectors · https://arxiv.org/abs/2601.10387 · https://arxiv.org/abs/2609.13637 · https://arxiv.org/pdf/2510.04648 · https://arxiv.org/pdf/2511.10277
- https://randym32.github.io/Anki.Vector.Documentation/guides/Vector%20character%20design%20guide.html · https://openai.com/en-GB/policies/terms-of-use/

**Project files**
- `DESIGN-AND-PLAN.md` · `docs/LAB.md` · `os/package/cortex/cortex.mk`
