# First USB readiness

This records the November 21–22 release qualification. See [PipOS status](../STATUS.md#current-handover--26-november-2026) for the subsequent physical boots and current work.

**Physical media is on hold as of 22 November 2026.** The target is the maintainer's 13-inch MacBookPro9,2 with Intel HD 4000, 16 GB RAM and a 256–512 GB SATA SSD. The first physical milestone is an Option-key boot into Try. Installing to an internal SSD is a separate decision.

**Qualification run, 22 November 2026 (interim; details in the local status record).** Candidate `first-usb-20261121-2150` is **not** qualified. Its installed recovery exposed a product defect: after a power cut during an update, the FAT boot-state partition becomes read-only on the next boot (`fat_free_clusters: deleting FAT entry beyond EOF`), and every later update and slot confirmation fails. `first-usb-20261121-2300` adds an ESP check (`fsck.fat` packaged, `systemd-fsck@` before `/bootstate`) and passed the same interrupted-update, unhealthy-trial and rollback run, and legacy model migration. Two further product defects were then found and fixed in `first-usb-20261122-0053` (seal `15eceec6f6258b161f928c7929c24a0071a3cdffd4b8b0d2c4932a23bc323311`, Try `667c6acb341cb5501d9bf785277be0764cd3a7872d02c1e725fb0c32d086860a`). Spoken time zones never resolved on the device because the packaged tzdata links its regions. A name heard during the 1.5 s wake was dropped. The frozen lab tests now carry the reviewed fixture corrections (lazy CA store, BusyBox IPv6, face renderer timing). Requalification of 0053 is in progress. Recall ranks relevance before recency, which remains an owner decision. Long soaks and every physical check remain open.

This is the release-coordination record. The [design coverage table](DESIGN-COVERAGE.md) assigns outstanding work and acceptance criteria for every numbered design section, original phase and subsequent design experiment. The [lab guide](LAB.md) contains the individual development procedures. The project log records changes and evidence as each workstream finishes them.

## Current candidate, 22 November 2026

Cache-assisted candidate `first-usb-20261122-0053` passed all 22 suites in the frozen guest controller. It passed installed boot, signed A/B updates, power-loss recovery, automatic rollback after an unhealthy signed trial, and preservation of identity and memory. The spoken time-zone and wake-name fixes passed the skills and Dream Journal checks on this seal. The separate 15-minute endurance rehearsal passed. Local records: Sealed release · Guest report · Installed recovery · Endurance rehearsal

The exact Lexar-sized image booted over virtual USB, and a byte readback after the test verified both GPT copies, both write extents and all three payload partitions. The physical Lexar still contains the Monterey installer. The MacBook's firmware, audio, radio and thermals require native testing. Local records: Geometry boot · Rehearsal readback

The supplemental legacy model migration test failed an aggregate write-count assertion during donor rejection. The queue wrapper returned zero despite the unittest failure. The read-write test mount can permit background ext4 initialization. Establish the writer with a controlled rerun before sign-off. The older 2300 seal passed this migration test. It also exposed a separate ambiguous recall case where the combined Restore and retention probe retrieved the older of two matching memories. Standalone Restore and retention checks passed on 0053. Local records: Migration run · Earlier combined recall

The qualification run completed the repairs and started the final checks before it was interrupted. Two bounded rechecks were interrupted the same way. The running checks were then monitored to completion and the seal was verified again. The builder and test guests are stopped. Local record: Work log

## Earlier coordination and checkpoints

**Latest combined host checkpoint, 19:38 AEDT.** All 519 checks passed with zero skips on source/model snapshot `50fbdc90940aea570ef5847407c527b962a6f3d8307e8971d1e18c433732ab79`. That comprises 86 Rust tests, 418 Python tests and 15 host-side lab checks, including the six tests using a private Qwen server and 16 encrypted-backup checks. Eleven inputs had changed by the end of the run. This is the host baseline for the next integration pass. Local record: Qualification and drift report

**Installed writer checkpoint.** The corrected repack includes the Python TLS extension that the first installed boot lacked. It passed the installation power cut and cold boots into both slots without USB. RAUC rejected the signed factory-channel bundle and an unsigned test file, then installed the signed installed-channel bundle. Slot B confirmed health with the same life ID and remembered fact. Both attached uninvolved disks passed byte-for-byte comparison. The integrated release build, failed-trial rollback and interruption during the update remain open. Local records: Startup diagnostic · Corrected guest results

## Integrated candidate under test

The first clean compile reached image assembly and failed because the Debian builder lacked `mkfs.ext4`. The fix added e2fsprogs and an early packaging check. The retry reused the pinned Buildroot cache through a disposable write overlay, rebuilt the organs, TLS components and kernel configuration, and sealed `first-usb-20261121-2135`. The `2132` attempt was stopped before packaging to reconfigure OpenSSL with its required command-line tool. All 529 host checks passed on `2132`. The only frozen input change in `2135` is the packaging script that adds that reconfiguration. Thirty-five targeted host checks then passed on `2135`. Local records: Clean build log · Sealed candidate · Host report

The exact Try image passed the EFI asset audit. A 61.9 GB disposable disk file passed GPT and byte readback, then booted the local menu through virtual USB. The factory image booted and passed the L1 face tests. L0 found two failed systemd-networkd sockets on default Offline boot. The failures are being traced, and the independent suites continue. Local records: Guest controller · Offline startup diagnostic · Lexar geometry test

The 61.9 GB Lexar's Monterey installer is approved to be overwritten after qualification. The Lexar is still unchanged. Its prepared image ranges and partition hashes are recorded separately from the compact source image. Local record: Lexar plan

## Active workstreams

Release coordination reviewed every workstream, the design and the checkout. Work is assigned directly by workstream. Each workstream records its source changes, exact artifact or source identity, commands, observed result, outstanding work and cleanup in the project log. Release coordination owns closure of dependencies and any item without a dedicated workstream.

| Workstream | Current responsibility | Evidence needed for the candidate |
|---|---|---|
| Installer and A/B updates | Disk writer, install identity and installed A/B updates | Supply disposable USB → SATA installation, interruption, removal/reboot, actual signed installed-channel deployment, rejection and rollback evidence with a second disk attached. Supply the installed QA medium from the exact two bundles. |
| Installer options | Built-in display and keyboard for Try, Install, Restore and personalization | Exercise the actual screen and controls without a serial shell. Reach the existing writer and backup service, show the target’s identity, test cancellation and carry only eligible settings. |
| Backup and restore | Encrypted backup, restore, correction/forget continuity | Publish the callable restore contract. Test a replacement disk, stale or missing deletion proof, interrupted restore, corruption, exclusions, and identity plus a retained control after boot. |
| Decision-model exploration | Bounded overnight memory worker and source-linked journal | Demonstrate consent, power/thermal gating, interrupt/resume and stale-revision rejection after edits. Keep measured research decisions and physical model choices visible. |
| Default skills | Owner network capabilities, starting with weather | Expose choices to setup, prove zero outbound lookup when disabled, and test timers/reminders and conversation through outages. Resolve system-level policy and clock sync with hardware. |
| Face animation | Complete face states through the live event path | Supply image-bound pose/wake, lid, gaze and recovery checks. Carry 20/60 fps host evidence into the physical HD 4000 measurement plan. |
| Performance on the 2012 MacBook Pro | CPU/latency, cache behavior and physical timeout defaults | Record the measured promotion decision on each patch. Isolate QEMU-only timeout allowances; verify normal appliance defaults. Supply the physical benchmark workload. |
| Observability | LAN enrollment, exact packaged hardware drivers, firmware and telemetry | Check real candidate contents against the hardware checklist. Prepare offline radio provisioning, Ethernet recovery, hardware inventory and private on-device measurements. |
| Release coordination | Source freeze, signed model-pack baseline, candidate assembly, verification, soak and decision | Keep a single candidate directory, evidence from that exact set, and the outstanding scope list. Open follow-on work when dependencies clear. |

The Installer and A/B updates workstream owns the current installer experiment. Each workstream can run focused host checks and stage named outputs. Reserve the shared builder and long-running guest tests in the project log before integration. Keep each run's media, disks, screenshots and logs attributable to its workstream. Whichever workstream starts a guest or builder manages it through cleanup.

### Work that follows the current workstreams

| Dependency | Next workstream and deliverable |
|---|---|
| Restore and retention formats settle | The Backup and restore and Installer options workstreams finish guest, session-only, explicit-only and expiry policies through the actual setup, archive, derived records and backup paths. |
| Overnight publication is stable | Decision-model exploration compares recall on held-out conversations before and after publication. Face animation connects a genuine dream result to the next visible wake while keeping the closed panel dark. |
| Installed A/B and recovery are verified | Installer and A/B updates moves on to the remaining body and senses behaviors, including sensor reactions, trackpad interruption, consent-gated eye-contact attention and deliberate single-frame object description. |
| Speech and media interfaces settle | Decision-model exploration runs the production speech candidate auditions with Performance on the 2012 MacBook Pro. Release coordination keeps the remaining sound cues, speaker identification and signed model selection until their implementations and privacy controls are verified. The MacBook's measured performance determines the selected runtime. |
| The exact build and guest gates pass | Release coordination runs bounded lab endurance checks and identifies the Try image for hardware qualification. Phase 7 alternatives and older laptops retain their explicitly optional status in the design. |

## What the previous runs establish

| Evidence | Verified | Limit of the result |
|---|---|---|
| Try virtual USB | Two boots reset the RAM-only trial. A writable attached SATA disk remained byte-identical. | Standalone Try with an earlier system payload. |
| Factory rollback, interrupted update, power cuts | Signed factory updates, recovery after failed trial boots, interrupted inactive-slot update and 20 seeded power cuts. | Installed disks have their own identifiers and update compatibility. |
| Managed Wi-Fi, camera, spoken cadence, LAN deployment | Simulated radio reconnect, opt-in camera release, clause playback, and actual signed deployment over the paired LAN workflow. | Separate staged images and fixtures. Native audio, camera, radio and heat need the MacBook. |
| Release-coordination Rust run, Python, source list | 76 Rust and 224 self-contained Python cases passed on an isolated 19:14 AEDT snapshot. | Six live-server tests passed earlier in the 211-case run. Run these six again on the frozen candidate with a dedicated server. |

The exact SquashFS contents of the artifacts on disk were checked. SHA-256 prefixes for default factory, standalone Try, hardware/LAN, camera, installer preflight, Ivy Bridge and voice cadence were respectively `3c612d7d375d`, `6e0e3b3407f7`, `e3e4045c2003`, `05783f68b7fa`, `f2021db1e033`, `8dc0380fd0b5` and `2433741c414e`. These identify the observed revisions. Keep the complete hashes for release decisions. A new filename or a more recent timestamp cannot establish inclusion of another workstream's changes.

## Results by workstream

| Workstream | New evidence | Acceptance still open |
|---|---|---|
| Install and screen | The isolated writer test verified installation interruption, USB removal, both installed slots and signed update rejection and acceptance. Separate screen artifacts cover keyboard navigation, installation and identity-only Restore. Installer artifact hashes · Installed channel · Screen Restore | Repeat on the frozen release candidate and exercise full archive Restore, installed failed-trial rollback and interrupted updates. |
| Memory | Encrypted backup and restore fixtures cover wrong keys, stale/missing proof, old snapshots, a retained control and recovery after interruption. The first overnight worker publishes a source-linked journal and rejects work from superseded revisions. Backup checks · Dream checks | The installed Mind service exercised retention modes across cold boot on a disposable overlay with a scripted model. Held-out overnight recall also has host evidence. Verify both on the sealed image and complete the on-screen transplant. Retention guest run · Held-out recall |
| Face, voice and power | Face and brainstem now communicate low-charge state. Host checks verify the target 120/60-second timeouts and enable longer waits on the marked QEMU path. The prompt-cache experiment stays experimental after worsening cancellation in all four measured pairs. Face evidence · Timeout checks · Prompt experiment | Render the live cues on the selected media, exercise real voice through the installed stack, and record on-target accuracy, latency, heat and frame pacing. |
| Connectivity, skills and hardware | Weather refused disallowed lookups on the host. The packaged hardware inspection located the Ethernet, graphics, audio and sensor drivers and found no b43 firmware on the audited image. Weather checks · Hardware inventory | Qualify device-wide Offline/paired-LAN/Internet traffic, clock sync, quiet hours, an offline wireless firmware route and Ethernet recovery. The MacBook must identify the actual card and router behavior. |
| Model and release tooling | The shipped Qwen descriptor includes its immutable source, licence and weight digest. Host fixtures exercise the signed pack, and a real-weight check used a temporary signer. The release tools refuse differing root payloads, changed inputs and incomplete guest suites. The packaging driver was exercised using fixture media. Model pack checks · Release tool checks | Run the clean packaging driver on the selected sources, verify both embedded copies, and exercise the actual signed service. An old unsigned model partition needs an explicit rollback/provision/upgrade drill. A change of model needs cache invalidation keyed to the new weights. Run release suites and endurance checks. |

## Promotion gates

| Gate | Workstream | Required evidence | Current release state |
|---|---|---|---|
| Scope and dependencies | Release coordination and every workstream | Each committed behavior in the coverage table has evidence on the chosen inputs, an explicit disposition by the maintainer, or a named check that first becomes possible on the hardware. Record optional experiments and their measured promotion decisions individually. | Open. Core software work remains in progress. |
| Reproducible inputs and media | Release coordination | Freeze the actual checkout and model inputs, including untracked files. Use a clean Buildroot output to generate factory, Try, both signed RAUC channels and their QA fixtures in one pass. Match the release manifest, slots, bundled root payload, model identity, trust and compatibility. | Open. No integrated release manifest accepted. |
| Boot, install and restore | Installer and A/B updates; Installer options; Backup and restore | Local-screen walk-through. Leave a second writable disk byte-identical during Try. Positively select the disposable target and test cancel, wrong target, interruption, reboot with USB removed and the active A/B mounts. Restore a prior identity on a blank target, apply edits or recover only permitted material, and verify after cold boot. | Open. Earlier Try is a baseline. |
| Update, data loss and confidentiality | Installer and A/B updates; Backup and restore; Observability | Install the exact signed installed-channel bundle on the inactive slot, test foreign/unsigned rejection, interrupted update, failed-trial rollback and healthy confirmation. Observe the same profile/life ID, retained history and absence of forgotten text. Test backups, caches, migrations, defaults for camera/audio/network, access revocation, and exclusion of private credentials. | Open on the integrated candidate. The isolated installed test covered channel rejection, healthy confirmation, life ID and one retained fact. |
| Integrated being | All organ workstreams; Release coordination | Run the host suites including six real-model cases; then exercise L0–L6, profile/rename, skills, dream work, local-screen entry, recovery and offline conversation on the sealed artifact set. Record any Wi-Fi-only hwsim kernel separately. Capture emitted sound/face evidence, missing cases, failures, skips and retries. | Open. Source, staged-image, and exact-candidate evidence have different meanings. |
| Persona | Release coordination | Compare the waking model and all four personalities against the accepted baseline using the same pinned local judge. Include the baseline report and a fresh candidate report when persona inputs change. [Persona release evaluation](LAB.md#persona-release-evaluation) | Evaluator and candidate checks are in source. Native reports are pending. |
| Endurance and hardware handover | Release coordination; Performance on the 2012 MacBook Pro; Observability | Record actual elapsed observation, service restarts, failures, cleanup and artifact ID. Exercise face, organ recovery and appliance endurance in the appropriate lab/physical environments. The design specifies a 24-hour face run, 48-hour recovery soak and one-week appliance soak. | Open. A short rehearsal establishes the harness only. |
| First physical Try | The maintainer; Observability | After the software review, identify the intended external stick and record the go-ahead to write it. Log Option-key EFI boot, internal-disk isolation, built-in display/keyboard/trackpad, lid, camera/LED, radio/firmware, mic/speakers, a real spoken turn, and recovery. For first-pixel evidence, film the panel at 240 fps from firmware picker through the face, once normally and once with boot messages. Keep the PIPLOGS “Boot display handoff” lines (EFI/simpledrm/i915 and the first face swap) beside the clip; record every black frame or mode flash rather than inferring continuity from a healthy boot. | Requires the MacBook and chosen USB. |
| Internal installation and qualification | The maintainer; Observability; Release coordination | Before any erase, complete macOS firmware, battery/charge-cap and recovery-media preparations. Verify the target SSD and explicit erase selection, remove USB and boot the installed being; run the Phase 0–6 native targets and long soaks. | Requires separate authorization and measured hardware results. |

The laptop signing command writes `beingos-laptop-update.raucb.provenance.json`
next to the bundle. Run
`python3 -m lab.hardware_image TRY.img --match-laptop-bundle UPDATE.raucb --release-certificate RELEASE.pem`
to match the selected Try image's source, build and SquashFS content to the
receipt and hash the exact image and bundle under test. The content digest omits
SquashFS padding. The public release certificate must match both the
embedded RAUC keyring and the receipt fingerprint. For release evidence,
add `--verify-laptop-bundle` on a host with RAUC available. The audit verifies
the signature with the supplied certificate, compares the signed manifest and
hashes the extracted root filesystem against the receipt and the Try content.
Save the report with `bundle_verified_by_rauc` set to `true` alongside the
rehearsal results. The receipt separately records the extraction at signing.
For a release-key change, use the separate bridge audit and retain evidence
of rollback and the first replacement-key update. [Rotation procedure](HARDWARE.md#rotate-the-laptop-release-key)

For media intended for public release, run the candidate test command with
`--manifest` and `--require-alba-retrain`. Before starting any guest suite, it
audits the sealed Try and factory images, including the laptop versions when
present. Each matching audit log is named by image role under
`alba_retrain_reports` in `report.json`. For a single image, the same flag works
with `lab.hardware_image`.

The voice audit checks the signature against each system slot's embedded model
certificate and descriptor, then verifies every file listed in the signed pack.
It checks the pinned Alba retrain, runtime card, token table and upstream notices,
and refuses the known private model anywhere on the models partition. Complete
redistribution notices and native speech qualification remain separate release
evidence.

## Integration order

1. Resolve cross-workstream interfaces first. The installer invokes the writer and the backup service. Backup consumes the durable edit history and excludes keys/network profiles. Dreams consume eligible, versioned memories and discard superseded work. The UI consumes owner capability choices, and the weather path enforces them. Coordinate overlapping edits before the freeze.
2. Select a fresh candidate name and use the [candidate build entry point](../lab/build.sh). From the repository run `lab/build.sh candidate NAME` in the agreed builder window. It records inputs, freezes a source copy, makes one clean build and packages its artifacts under `out/candidates/NAME/`. The manifest binds the media to the frozen design, coverage table, readiness record and inspection of the weights inside both disks. A failed or partial directory retains diagnostics. A sealed `release.json` identifies a packaged candidate. Full regression and promotion still follow.
3. Run host validation and guest qualification from the frozen source directory. Use `python3 -m lab.candidate_tests --manifest ../release.json --persona-baseline ../persona-baseline.json --output ../guest-evidence`. Add `--persona-result` if the persona inputs changed, and `--wifi-image` for a separately identified hwsim companion built from the same sources. The screen-enabled `media/beingos-try-screen.img` is the release Try target. Run any remaining feature cases and retain named logs and per-case outcomes there. From the project root, recheck the manifest using `python3 -m lab.candidate_manifest verify --root out/candidates/NAME/source out/candidates/NAME/release.json`. A changed input or artifact needs a new named candidate and affected gates rerun.
4. Record one dated verdict with the candidate name, full Try checksum, test and endurance reports, specific open hardware observations and the maintainer's scope decisions. The earlier images remain development evidence. The record must explicitly identify which USB image is proposed before the first physical write.

The source manifest and preserved snapshot provide a reproducible identifier for the candidate. No physical erase is part of this release-coordination pass.
