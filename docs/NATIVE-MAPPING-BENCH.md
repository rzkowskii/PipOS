# Native model mapping evidence

`lab/native_mapping_bench.py` fills the storage/RAM evidence gap around the
external PipOS-Research Stage 1b Q5_K/Q6_K and Stage 2 shortlist kits. Those
kits remain responsible for kernel correctness and speed. This tool records
the filesystem and block device holding each fixture, available RAM, initial
page residency, and first-versus-second pass time over a read-only file-backed
mapping.

For the cold arm, boot native Linux and run this before any command has read
the fixture files. Copy the script beside the already verified kit, select the
kit's Q4_K, Q5_K and Q6_K data files, and write the result to PIPLOGS:

```sh
python3 native_mapping_bench.py \
  --file q4=/absolute/path/to/reflex-q4k.bin \
  --file q5q6=/absolute/path/to/reflex-q56.bin \
  --file model=/models/reflex.gguf \
  --output /absolute/path/to/PIPLOGS/native-mapping.json
```

Use the paths and labels from the particular signed research kit; the example
names are illustrative. `cold_eligible` is true only when at most 2% of that
file's pages were resident before the first pass. A false value is a warm
measurement, not failed evidence: reboot and repeat to obtain a cold arm.
Compare `file_fraction_of_spare_ram` and `MemAvailable` alongside the two pass
times so a fast warm result is not mistaken for SATA performance.

The tool does not drop caches, call `sync`, change readahead or power settings,
mount media, invoke a benchmark binary, download data, or write any input. It
refuses to replace an existing report. Its only write is the requested JSON
output, whose file stat and source/build IDs bind it to the native run. Keep
the external kit's hashes and Stage 1b/2 result archive with this report.

## Installed startup comparisons

The MacBookPro9,2 installed kernel accepts two independent one-boot
options. Neither runs on a Try or unconfirmed trial boot.

- `being.model_warm_mib=64`, `128` or `256` starts a low-priority read of
  the model's leading pages as soon as the models volume is available.
  It checks memory headroom and publishes bytes read and elapsed time.
  Cortex and Voice can start while the read is in progress.
- `being.mesa_cache=1` lets the actual Face and Setup renderers populate
  a private 16 MiB Mesa cache on the installed archive volume, for reuse
  on later boots with the same option. Safe graphics uses the RAM cache.
  Record the first population boot separately from reuse boots.

For the model comparison, keep the signed image, hardware and model
identical and compare the normal boot with one selected read size.
Capture the first successful turn and `model warm` journal record. For the
graphics comparison, keep the read-size option fixed and compare first
swap and first unblanked frame in the boot report. A native panel recording
can establish when those frames became visible on the screen.
