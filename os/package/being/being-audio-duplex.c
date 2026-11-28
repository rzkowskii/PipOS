#define _POSIX_C_SOURCE 200809L
#include <alloca.h>
#include <alsa/asoundlib.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* Diagnostic evidence helper only. Production Voice/Ears remain half duplex. */
#define RATE 48000U
#define CHANNELS 2U
#define PERIOD 480UL
#define PERIODS 4U
#define SECONDS 4U
#define STIMULUS_FIRST_FRAME (RATE / 2U)
#define STIMULUS_PULSE_FRAMES (RATE / 200U)
#define STIMULUS_INTERVAL_FRAMES RATE
#define STIMULUS_PULSES 4U

static volatile sig_atomic_t stopping;
static void stop_now(int sig) { (void)sig; stopping = 1; }

static uint64_t ns(struct timespec t) {
    return (uint64_t)t.tv_sec * 1000000000ULL + (uint64_t)t.tv_nsec;
}

static int configure(snd_pcm_t *pcm, snd_pcm_stream_t stream,
                     snd_pcm_uframes_t *buffer_out) {
    snd_pcm_hw_params_t *hw;
    snd_pcm_sw_params_t *sw;
    unsigned rate = RATE, periods = PERIODS;
    snd_pcm_uframes_t period = PERIOD, buffer = PERIOD * PERIODS;
    int dir = 0, err;
    snd_pcm_hw_params_alloca(&hw);
    snd_pcm_sw_params_alloca(&sw);
    if ((err = snd_pcm_hw_params_any(pcm, hw)) < 0 ||
        (err = snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED)) < 0 ||
        (err = snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_S16_LE)) < 0 ||
        (err = snd_pcm_hw_params_set_channels(pcm, hw, CHANNELS)) < 0 ||
        (err = snd_pcm_hw_params_set_rate_near(pcm, hw, &rate, &dir)) < 0 || rate != RATE ||
        (err = snd_pcm_hw_params_set_period_size_near(pcm, hw, &period, &dir)) < 0 || period != PERIOD ||
        (err = snd_pcm_hw_params_set_periods_near(pcm, hw, &periods, &dir)) < 0 || periods != PERIODS ||
        (err = snd_pcm_hw_params_set_buffer_size_near(pcm, hw, &buffer)) < 0 ||
        buffer != PERIOD * PERIODS ||
        (err = snd_pcm_hw_params(pcm, hw)) < 0)
        return err < 0 ? err : -EINVAL;
    if ((err = snd_pcm_sw_params_current(pcm, sw)) < 0 ||
        (err = snd_pcm_sw_params_set_tstamp_mode(pcm, sw, SND_PCM_TSTAMP_ENABLE)) < 0 ||
        (err = snd_pcm_sw_params_set_tstamp_type(pcm, sw, SND_PCM_TSTAMP_TYPE_MONOTONIC_RAW)) < 0 ||
        (err = snd_pcm_sw_params_set_avail_min(pcm, sw, PERIOD)) < 0)
        return err;
    if (stream == SND_PCM_STREAM_PLAYBACK &&
        (err = snd_pcm_sw_params_set_start_threshold(pcm, sw, buffer + 1)) < 0)
        return err;
    if ((err = snd_pcm_sw_params(pcm, sw)) < 0) return err;
    *buffer_out = buffer;
    return 0;
}

static void stimulus(int16_t *mono, size_t frames, double level) {
    const double amplitude = 32767.0 * pow(10.0, level / 20.0);
    memset(mono, 0, frames * sizeof(*mono));
    /* Four deterministic, tapered 5 ms clicks, leaving acoustic decay gaps. */
    for (size_t at = STIMULUS_FIRST_FRAME;
         at + STIMULUS_PULSE_FRAMES < frames; at += STIMULUS_INTERVAL_FRAMES) {
        for (size_t i = 0; i < STIMULUS_PULSE_FRAMES; ++i) {
            double envelope = 0.5 - 0.5 * cos(2.0 * M_PI * i / (STIMULUS_PULSE_FRAMES - 1));
            mono[at + i] = (int16_t)lrint(amplitude * envelope * sin(2.0 * M_PI * 1000.0 * i / RATE));
        }
    }
}

static int wav_at(int dirfd, const char *name, const int16_t *samples, uint32_t frames,
                  int *created) {
    int fd = openat(dirfd, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    FILE *f;
    uint32_t bytes = frames * 2, riff = bytes + 36, rate = RATE, byte_rate = RATE * 2;
    uint16_t one = 1, bits = 16, align = 2;
    if (fd < 0) return -1;
    *created = 1;
    if (fchmod(fd, 0600) < 0 || !(f = fdopen(fd, "wb"))) {
        close(fd);
        return -1;
    }
    int ok = !(fwrite("RIFF", 1, 4, f) != 4 || fwrite(&riff, 4, 1, f) != 1 ||
        fwrite("WAVEfmt ", 1, 8, f) != 8 ||
        fwrite(&(uint32_t){16}, 4, 1, f) != 1 || fwrite(&one, 2, 1, f) != 1 ||
        fwrite(&one, 2, 1, f) != 1 || fwrite(&rate, 4, 1, f) != 1 ||
        fwrite(&byte_rate, 4, 1, f) != 1 || fwrite(&align, 2, 1, f) != 1 ||
        fwrite(&bits, 2, 1, f) != 1 || fwrite("data", 1, 4, f) != 4 ||
        fwrite(&bytes, 4, 1, f) != 1 || fwrite(samples, 2, frames, f) != frames ||
        fflush(f) || fsync(fileno(f)));
    if (fclose(f)) ok = 0;
    return ok ? 0 : -1;
}

int main(int argc, char **argv) {
    snd_pcm_t *cap = NULL, *play = NULL;
    snd_pcm_status_t *cs, *ps;
    snd_pcm_uframes_t cb = 0, pb = 0;
    const size_t frames = RATE * SECONDS;
    int16_t *ref = NULL, *mic = NULL, *duplex = NULL;
    int outfd = -1, mic_created = 0, ref_created = 0, event_created = 0;
    struct stat outst;
    int linked = 0, sealed = 0, xruns = 0, rc = 1, capture_channel = 0;
    uint64_t start_ns = 0, cap_ht_ns = 0, play_ht_ns = 0, cap_trigger_ns = 0, play_trigger_ns = 0;
    uint64_t play_start_status_ns = 0, play_start_trigger_ns = 0;
    snd_pcm_sframes_t play_start_delay = -1;
    snd_pcm_uframes_t play_start_avail = 0;
    double level = -20.0;
    if (argc != 4 || strcmp(argv[1], "--out")) return 64;
    if (sscanf(argv[3], "%lf", &level) != 1 || !isfinite(level) ||
        level < -40.0 || level > -3.0) return 64;
    outfd = open(argv[2], O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (outfd < 0 || fstat(outfd, &outst) < 0 || !S_ISDIR(outst.st_mode)
            || outst.st_uid != geteuid() || (outst.st_mode & 0077) != 0) goto out;
    signal(SIGINT, stop_now); signal(SIGTERM, stop_now); signal(SIGHUP, stop_now);
    ref = calloc(frames, sizeof(*ref)); mic = calloc(frames * CHANNELS, sizeof(*mic));
    duplex = calloc(PERIOD * CHANNELS, sizeof(*duplex));
    if (!ref || !mic || !duplex) goto out;
    stimulus(ref, frames, level);
    if (snd_pcm_open(&cap, "hw:0,0", SND_PCM_STREAM_CAPTURE, 0) < 0 ||
        snd_pcm_open(&play, "hw:0,0", SND_PCM_STREAM_PLAYBACK, 0) < 0 ||
        configure(cap, SND_PCM_STREAM_CAPTURE, &cb) < 0 ||
        configure(play, SND_PCM_STREAM_PLAYBACK, &pb) < 0 || cb != pb ||
        snd_pcm_link(cap, play) < 0) goto out;
    linked = 1;
    if (snd_pcm_prepare(cap) < 0 || snd_pcm_prepare(play) < 0) goto out;
    size_t queued = 0;
    while (queued < pb) {
        size_t n = pb - queued > PERIOD ? PERIOD : pb - queued;
        for (size_t i = 0; i < n; ++i) duplex[2*i] = duplex[2*i+1] = ref[queued+i];
        snd_pcm_sframes_t got = snd_pcm_writei(play, duplex, n);
        if (got <= 0) goto out;
        queued += (size_t)got;
    }
    struct timespec began;
    clock_gettime(CLOCK_MONOTONIC_RAW, &began); start_ns = ns(began);
    if (snd_pcm_start(cap) < 0) goto out;
    snd_pcm_status_alloca(&cs); snd_pcm_status_alloca(&ps);
    if (snd_pcm_status(play, ps) < 0) goto out;
    snd_htimestamp_t start_ht, start_trigger;
    snd_pcm_status_get_htstamp(ps, &start_ht);
    snd_pcm_status_get_trigger_htstamp(ps, &start_trigger);
    play_start_status_ns = ns(start_ht);
    play_start_trigger_ns = ns(start_trigger);
    play_start_delay = snd_pcm_status_get_delay(ps);
    play_start_avail = snd_pcm_status_get_avail(ps);
    if (!play_start_status_ns || !play_start_trigger_ns || play_start_delay < 0
            || (snd_pcm_uframes_t)play_start_delay > pb || play_start_avail > pb) goto out;
    size_t captured = 0, written = queued;
    while (!stopping && captured < frames) {
        size_t want = frames - captured > PERIOD ? PERIOD : frames - captured;
        snd_pcm_sframes_t got = snd_pcm_readi(cap, duplex, want);
        if (got == -EPIPE) { ++xruns; goto out; }
        if (got < 0) goto out;
        for (snd_pcm_sframes_t i = 0; i < got; ++i) {
            mic[2 * (captured + i)] = duplex[2*i];
            mic[2 * (captured + i) + 1] = duplex[2*i + 1];
        }
        captured += (size_t)got;
        /* Refill to the same frame lead established by the initial buffer.
         * A short write must not silently shift or omit the known reference. */
        size_t target = captured + pb > frames ? frames : captured + pb;
        while (written < target) {
            size_t n = target - written > PERIOD ? PERIOD : target - written;
            for (size_t i = 0; i < n; ++i) duplex[2*i] = duplex[2*i+1] = ref[written+i];
            snd_pcm_sframes_t put = snd_pcm_writei(play, duplex, n);
            if (put == -EPIPE) { ++xruns; goto out; }
            if (put < 0) goto out;
            written += (size_t)put;
        }
    }
    if (stopping || captured != frames) goto out;
    if (snd_pcm_status(cap, cs) < 0 || snd_pcm_status(play, ps) < 0) goto out;
    snd_htimestamp_t cht, pht, ctrigger, ptrigger;
    snd_pcm_status_get_htstamp(cs, &cht); snd_pcm_status_get_htstamp(ps, &pht);
    snd_pcm_status_get_trigger_htstamp(cs, &ctrigger);
    snd_pcm_status_get_trigger_htstamp(ps, &ptrigger);
    cap_ht_ns = ns(cht); play_ht_ns = ns(pht);
    cap_trigger_ns = ns(ctrigger); play_trigger_ns = ns(ptrigger);
    if (!cap_trigger_ns || !play_trigger_ns) goto out;
    snd_pcm_drop(cap); snd_pcm_drop(play);
    long double energy[CHANNELS] = {0.0L, 0.0L};
    for (size_t i = 0; i < frames; ++i) {
        energy[0] += (long double)mic[2*i] * mic[2*i];
        energy[1] += (long double)mic[2*i+1] * mic[2*i+1];
    }
    capture_channel = energy[1] > energy[0] ? 1 : 0;
    /* Compact only after measuring both inputs; the retained WAV stays mono. */
    for (size_t i = 0; i < frames; ++i) mic[i] = mic[2*i + capture_channel];
    if (wav_at(outfd, "echo-mic.wav", mic, frames, &mic_created)
            || wav_at(outfd, "echo-reference.wav", ref, frames, &ref_created)) goto out;
    int eventfd = openat(outfd, "echo-events.json",
                         O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (eventfd < 0) goto out;
    event_created = 1;
    FILE *e = fdopen(eventfd, "w");
    if (!e) { close(eventfd); goto out; }
    if (fchmod(eventfd, 0600) < 0) { fclose(e); goto out; }
    int event_ok = fprintf(e, "{\"schema\":4,\"name\":\"pip-diag-native-echo-trace\",\"frame_ms\":10,"
               "\"max_lag_ms\":250,\"hold_ms\":150,\"residual_rms_threshold\":0.025,"
               "\"playback\":[{\"start_ms\":0,\"end_ms\":4000}],\"interruptions\":[],"
               "\"timing_basis\":\"alsa-linked-trigger-monotonic-raw\",\"native\":{"
               "\"device\":\"hw:0,0\",\"rate\":48000,\"channels\":2,\"capture_channel\":%d,"
               "\"period_frames\":480,\"buffer_frames\":%lu,\"linked\":true,"
               "\"start_monotonic_raw_ns\":%llu,\"capture_status_ns\":%llu,"
               "\"playback_status_ns\":%llu,\"capture_trigger_ns\":%llu,"
               "\"playback_trigger_ns\":%llu,\"trigger_delta_ns\":%llu,"
               "\"playback_start_status_ns\":%llu,\"playback_start_trigger_ns\":%llu,"
               "\"playback_start_delay_frames\":%lld,\"playback_start_avail_frames\":%lu,"
               "\"initial_queued_frames\":%zu,"
               "\"reference_first_signal_frame\":%u,\"stimulus_pulse_frames\":%u,"
               "\"stimulus_interval_frames\":%u,\"stimulus_pulses\":%u,"
               "\"captured_frames\":%zu,\"written_frames\":%zu,\"xruns\":%d}}\n",
            capture_channel, (unsigned long)cb, (unsigned long long)start_ns, (unsigned long long)cap_ht_ns,
            (unsigned long long)play_ht_ns, (unsigned long long)cap_trigger_ns,
            (unsigned long long)play_trigger_ns,
            (unsigned long long)(cap_trigger_ns > play_trigger_ns ? cap_trigger_ns - play_trigger_ns :
                                 play_trigger_ns - cap_trigger_ns),
            (unsigned long long)play_start_status_ns, (unsigned long long)play_start_trigger_ns,
            (long long)play_start_delay, (unsigned long)play_start_avail, queued,
            STIMULUS_FIRST_FRAME, STIMULUS_PULSE_FRAMES,
            STIMULUS_INTERVAL_FRAMES, STIMULUS_PULSES,
            captured, written, xruns) >= 0;
    if (event_ok && (fflush(e) || fsync(fileno(e)))) event_ok = 0;
    if (fclose(e)) event_ok = 0;
    if (!event_ok) goto out;
    sealed = 1; rc = 0;
out:
    if (linked && cap) snd_pcm_unlink(cap);
    if (cap) { snd_pcm_drop(cap); snd_pcm_close(cap); }
    if (play) { snd_pcm_drop(play); snd_pcm_close(play); }
    if (!sealed && outfd >= 0) {
        if (mic_created) unlinkat(outfd, "echo-mic.wav", 0);
        if (ref_created) unlinkat(outfd, "echo-reference.wav", 0);
        if (event_created) unlinkat(outfd, "echo-events.json", 0);
    }
    if (outfd >= 0) close(outfd);
    free(ref); free(mic); free(duplex);
    return rc;
}
