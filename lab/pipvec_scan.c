/* Disposable WP-E9 arena scanner for little-endian x86-64 Linux:
 *   cc -O3 -msse4.1 -mpopcnt pipvec_scan.c -lcrypto -o pipvec_scan
 * Hamming distance selects the shortlist. The final sort uses scaled int8
 * dot products within that shortlist. No live database or loader uses it. */
#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <math.h>
#include <openssl/sha.h>
#include <smmintrin.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#if !defined(__x86_64__) || !defined(__linux__) || !defined(__SSE4_1__) \
    || !defined(__POPCNT__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "pipvec_scan requires little-endian x86-64 Linux, SSE4.1 and POPCNT"
#endif

#define MAX_ARENA (2ULL * 1024 * 1024 * 1024)
#define MAX_ROWS 2000000ULL
#define MAX_DIM 4096U
#define MAX_K 4096U
#define TOMBSTONE 1U

struct __attribute__((packed)) arena_header {
    uint8_t magic[8];
    uint32_t version, dim, bit_bytes, record_size;
    uint64_t count;
    uint8_t model[32], recipe[32], source[32], payload[32];
    char shard[8];
    uint8_t reserved[24];
};

struct __attribute__((packed)) record_header {
    uint64_t id;
    uint8_t kind, flags;
    uint16_t reserved;
    float scale;
};

struct __attribute__((packed)) query_header {
    uint8_t magic[8];
    uint32_t version, dim;
    float scale;
};

struct mapped_file {
    const uint8_t *data;
    size_t length;
    int fd;
    struct stat before;
};

struct candidate {
    uint32_t distance;
    uint64_t id, index;
    double score;
    uint8_t kind;
};

_Static_assert(sizeof(size_t) == 8 && sizeof(off_t) == 8, "64-bit arena offsets required");
_Static_assert(sizeof(struct arena_header) == 192 && sizeof(struct record_header) == 16
               && sizeof(struct query_header) == 20, "unexpected pipvec layout");

static int hex_nibble(char digit)
{
    if (digit >= '0' && digit <= '9') return digit - '0';
    if (digit >= 'a' && digit <= 'f') return digit - 'a' + 10;
    if (digit >= 'A' && digit <= 'F') return digit - 'A' + 10;
    return -1;
}

static bool hex32(const char *text, uint8_t out[32])
{
    if (strlen(text) != 64) return false;
    for (size_t i = 0; i < 32; ++i) {
        int upper = hex_nibble(text[2 * i]);
        int lower = hex_nibble(text[2 * i + 1]);
        if (upper < 0 || lower < 0) return false;
        out[i] = (uint8_t)((upper << 4) | lower);
    }
    return true;
}

static bool valid_shard(const char *shard)
{
    if (strlen(shard) != 7 || shard[4] != '-') return false;
    for (size_t i = 0; i < 7; ++i) {
        if (i != 4 && (shard[i] < '0' || shard[i] > '9')) return false;
    }
    int month = (shard[5] - '0') * 10 + shard[6] - '0';
    return month >= 1 && month <= 12;
}

static bool all_zero(const uint8_t *data, size_t length)
{
    for (size_t i = 0; i < length; ++i) {
        if (data[i]) return false;
    }
    return true;
}

static int open_lab_file(const char *path)
{
    struct stat original;
    if (lstat(path, &original) != 0 || !S_ISREG(original.st_mode)) return -1;
    char *resolved = realpath(path, NULL);
    if (resolved == NULL) return -1;
    bool live = strcmp(resolved, "/mind") == 0 || strncmp(resolved, "/mind/", 6) == 0;
    int folder = live ? -1 : open("/", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY);
    char *part = resolved + 1;
    char *slash;
    while (folder >= 0 && (slash = strchr(part, '/')) != NULL) {
        *slash = '\0';
        int next = openat(folder, part, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY);
        close(folder);
        folder = next;
        part = slash + 1;
    }
    int fd = -1;
    if (folder >= 0) {
        struct stat parent;
        if (fstat(folder, &parent) == 0 && parent.st_uid == geteuid()
                && (parent.st_mode & 077) == 0) {
            fd = openat(folder, part, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
        }
        close(folder);
    }
    free(resolved);
    struct stat opened;
    if (fd >= 0 && (fstat(fd, &opened) != 0 || opened.st_dev != original.st_dev
                    || opened.st_ino != original.st_ino)) {
        close(fd);
        fd = -1;
    }
    return fd;
}

static bool map_ro(const char *path, size_t maximum, struct mapped_file *file)
{
    file->fd = open_lab_file(path);
    if (file->fd < 0 || fstat(file->fd, &file->before) != 0) return false;
    const struct stat *info = &file->before;
    if (!S_ISREG(info->st_mode) || info->st_nlink != 1 || info->st_uid != geteuid()
            || (info->st_mode & 077) != 0 || info->st_size <= 0
            || (uint64_t)info->st_size > maximum) return false;
    file->length = (size_t)info->st_size;
    void *data = mmap(NULL, file->length, PROT_READ, MAP_PRIVATE, file->fd, 0);
    if (data == MAP_FAILED) return false;
    file->data = data;
    return true;
}

static void close_mapping(struct mapped_file *file)
{
    if (file->data != NULL) munmap((void *)file->data, file->length);
    if (file->fd >= 0) close(file->fd);
}

static bool same_file(const struct mapped_file *file)
{
    struct stat after;
    const struct stat *before = &file->before;
    return fstat(file->fd, &after) == 0
        && before->st_dev == after.st_dev && before->st_ino == after.st_ino
        && before->st_size == after.st_size
        && before->st_mtim.tv_sec == after.st_mtim.tv_sec
        && before->st_mtim.tv_nsec == after.st_mtim.tv_nsec
        && before->st_ctim.tv_sec == after.st_ctim.tv_sec
        && before->st_ctim.tv_nsec == after.st_ctim.tv_nsec;
}

static uint64_t monotonic_ns(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return (uint64_t)now.tv_sec * 1000000000 + (uint64_t)now.tv_nsec;
}

static bool worse(const struct candidate *left, const struct candidate *right)
{
    if (left->distance != right->distance) return left->distance > right->distance;
    if (left->kind != right->kind) return left->kind > right->kind;
    return left->id > right->id;
}

static void swap(struct candidate *left, struct candidate *right)
{
    struct candidate saved = *left;
    *left = *right;
    *right = saved;
}

static void heap_up(struct candidate *heap, size_t index)
{
    while (index) {
        size_t parent = (index - 1) / 2;
        if (!worse(&heap[index], &heap[parent])) break;
        swap(&heap[index], &heap[parent]);
        index = parent;
    }
}

static void heap_down(struct candidate *heap, size_t length)
{
    size_t index = 0;
    for (;;) {
        size_t left = 2 * index + 1;
        size_t right = left + 1;
        size_t worst = index;
        if (left < length && worse(&heap[left], &heap[worst])) worst = left;
        if (right < length && worse(&heap[right], &heap[worst])) worst = right;
        if (worst == index) break;
        swap(&heap[index], &heap[worst]);
        index = worst;
    }
}

static uint32_t hamming(const uint8_t *left, const uint8_t *right, uint32_t length)
{
    uint32_t total = 0;
    uint32_t i = 0;
    for (; i + 8 <= length; i += 8) {
        uint64_t a, b;
        memcpy(&a, left + i, sizeof(a));
        memcpy(&b, right + i, sizeof(b));
        total += (uint32_t)__builtin_popcountll(a ^ b);
    }
    for (; i < length; ++i) {
        total += (uint32_t)__builtin_popcount((unsigned)(left[i] ^ right[i]));
    }
    return total;
}

static int64_t dot_i8(const int8_t *left, const int8_t *right, uint32_t length)
{
    int64_t total = 0;
    uint32_t i = 0;
    for (; i + 16 <= length; i += 16) {
        __m128i a = _mm_loadu_si128((const __m128i *)(left + i));
        __m128i b = _mm_loadu_si128((const __m128i *)(right + i));
        __m128i alo = _mm_cvtepi8_epi16(a);
        __m128i blo = _mm_cvtepi8_epi16(b);
        __m128i ahi = _mm_cvtepi8_epi16(_mm_srli_si128(a, 8));
        __m128i bhi = _mm_cvtepi8_epi16(_mm_srli_si128(b, 8));
        __m128i sum = _mm_add_epi32(_mm_madd_epi16(alo, blo), _mm_madd_epi16(ahi, bhi));
        int32_t lanes[4];
        _mm_storeu_si128((__m128i *)lanes, sum);
        total += (int64_t)lanes[0] + lanes[1] + lanes[2] + lanes[3];
    }
    for (; i < length; ++i) total += (int64_t)left[i] * right[i];
    return total;
}

static int score_order(const void *first, const void *second)
{
    const struct candidate *left = first;
    const struct candidate *right = second;
    if (left->score != right->score) return left->score > right->score ? -1 : 1;
    if (left->kind != right->kind) return left->kind < right->kind ? -1 : 1;
    return (left->id > right->id) - (left->id < right->id);
}

static bool verify_headers(const struct mapped_file *arena, const struct mapped_file *query,
                           const char *shard, const uint8_t expected[5][32],
                           struct arena_header *header, struct query_header *ask)
{
    if (arena->length < sizeof(*header) || query->length < sizeof(*ask)) return false;
    memcpy(header, arena->data, sizeof(*header));
    memcpy(ask, query->data, sizeof(*ask));
    if (memcmp(header->magic, "PIPVEC1", 8) || memcmp(ask->magic, "PIPQRY1", 8)
            || header->version != 1 || ask->version != 1 || header->dim != ask->dim
            || header->dim == 0 || header->dim > MAX_DIM || header->count == 0
            || header->count > MAX_ROWS || !isfinite(ask->scale) || ask->scale <= 0) return false;
    size_t bits = (header->dim + 7) / 8;
    size_t used = sizeof(struct record_header) + bits + header->dim;
    size_t record_size = (used + 63) & ~(size_t)63;
    if (header->bit_bytes != bits || header->record_size != record_size
            || header->count > (MAX_ARENA - sizeof(*header)) / record_size
            || arena->length != sizeof(*header) + record_size * header->count
            || query->length != sizeof(*ask) + bits + header->dim
            || memcmp(header->shard, shard, sizeof(header->shard))
            || !all_zero(header->reserved, sizeof(header->reserved))
            || memcmp(header->model, expected[0], 32) || memcmp(header->recipe, expected[1], 32)
            || memcmp(header->source, expected[2], 32) || memcmp(header->payload, expected[3], 32)) {
        return false;
    }
    uint8_t digest[32];
    if (SHA256(query->data, query->length, digest) == NULL || memcmp(digest, expected[4], 32)) {
        return false;
    }
    const uint8_t *payload = arena->data + sizeof(*header);
    return SHA256(payload, arena->length - sizeof(*header), digest) != NULL
        && memcmp(digest, header->payload, 32) == 0;
}

static bool zero_high_bits(const uint8_t *bits, const struct arena_header *header)
{
    unsigned remainder = header->dim % 8;
    return !remainder || (bits[header->bit_bytes - 1] >> remainder) == 0;
}

static bool collect(const uint8_t *payload, const struct arena_header *header,
                    const uint8_t *query_bits, struct candidate *heap, size_t k, size_t *found)
{
    uint64_t previous_id = 0;
    uint8_t previous_kind = 0;
    size_t vector_bytes = header->bit_bytes + header->dim;
    size_t used = sizeof(struct record_header) + vector_bytes;
    for (uint64_t i = 0; i < header->count; ++i) {
        const uint8_t *row = payload + i * header->record_size;
        struct record_header record;
        memcpy(&record, row, sizeof(record));
        if (record.reserved || record.kind > 1 || record.id > INT64_MAX
                || (record.flags & ~TOMBSTONE) || !isfinite(record.scale)
                || !all_zero(row + used, header->record_size - used)
                || (i != 0 && (record.kind < previous_kind
                    || (record.kind == previous_kind && record.id <= previous_id)))) return false;
        previous_kind = record.kind;
        previous_id = record.id;
        const uint8_t *body = row + sizeof(record);
        if (!zero_high_bits(body, header)) return false;
        if (record.flags & TOMBSTONE) {
            if (record.scale != 0 || signbit(record.scale) || !all_zero(body, vector_bytes)) return false;
            continue;
        }
        if (record.scale <= 0) return false;
        struct candidate item = { .distance = hamming(body, query_bits, header->bit_bytes),
                                  .id = record.id, .index = i, .kind = record.kind };
        if (*found < k) {
            heap[*found] = item;
            heap_up(heap, *found);
            ++*found;
        } else if (worse(&heap[0], &item)) {
            heap[0] = item;
            heap_down(heap, *found);
        }
    }
    return true;
}

int main(int argc, char **argv)
{
    if (argc != 10) {
        fputs("usage: pipvec_scan ARENA QUERY K YYYY-MM MODEL_SHA RECIPE_SHA SOURCE_SHA PAYLOAD_SHA QUERY_SHA\n",
              stderr);
        return 2;
    }
    char *end;
    errno = 0;
    unsigned long requested = strtoul(argv[3], &end, 10);
    if (errno || end == argv[3] || *end || requested < 1 || requested > MAX_K
            || !valid_shard(argv[4])) return 2;
    uint8_t expected[5][32];
    for (size_t i = 0; i < 5; ++i) {
        if (!hex32(argv[i + 5], expected[i])) return 2;
    }
    if (!__builtin_cpu_supports("sse4.1") || !__builtin_cpu_supports("popcnt")) {
        fputs("CPU must support SSE4.1 and POPCNT\n", stderr);
        return 2;
    }
    struct mapped_file arena = { .fd = -1 };
    struct mapped_file query = { .fd = -1 };
    struct candidate *heap = NULL;
    int result = 1;
    uint64_t start = monotonic_ns();
    if (!map_ro(argv[1], MAX_ARENA, &arena)
            || !map_ro(argv[2], sizeof(struct query_header) + (MAX_DIM + 7) / 8 + MAX_DIM, &query)) {
        fputs("could not map private arena and query files\n", stderr);
        goto done;
    }
    struct arena_header header;
    struct query_header ask;
    if (!verify_headers(&arena, &query, argv[4], expected, &header, &ask)) {
        fputs("arena or query provenance/layout mismatch\n", stderr);
        goto done;
    }
    const uint8_t *query_bits = query.data + sizeof(ask);
    if (!zero_high_bits(query_bits, &header)) {
        fputs("invalid high query sign bits\n", stderr);
        goto done;
    }
    const int8_t *query_values = (const int8_t *)(query_bits + header.bit_bytes);
    const uint8_t *payload = arena.data + sizeof(header);
    uint64_t verified = monotonic_ns();
    size_t found = 0;
    heap = calloc(requested, sizeof(*heap));
    if (heap == NULL) goto done;
    if (!collect(payload, &header, query_bits, heap, requested, &found)) {
        fputs("invalid arena record, sign bits, or padding\n", stderr);
        goto done;
    }
    uint64_t selected = monotonic_ns();
    for (size_t i = 0; i < found; ++i) {
        const uint8_t *row = payload + heap[i].index * header.record_size;
        struct record_header record;
        memcpy(&record, row, sizeof(record));
        const int8_t *values = (const int8_t *)(row + sizeof(record) + header.bit_bytes);
        heap[i].score = (double)dot_i8(query_values, values, header.dim) * ask.scale * record.scale;
    }
    qsort(heap, found, sizeof(*heap), score_order);
    uint64_t ranked = monotonic_ns();
    if (!same_file(&arena) || !same_file(&query)) {
        fputs("arena or query changed during scan\n", stderr);
        goto done;
    }
    for (size_t i = 0; i < found; ++i) {
        if (printf("{\"kind\":\"%s\",\"id\":%" PRIu64 ",\"hamming\":%u,\"score\":%.17g}\n",
                   heap[i].kind ? "fact" : "episode", heap[i].id,
                   heap[i].distance, heap[i].score) < 0) goto done;
    }
    result = fflush(stdout) == 0 ? 0 : 1;
    if (result == 0 && start && verified >= start && selected >= verified && ranked >= selected) {
        fprintf(stderr, "{\"map_and_digest_ns\":%" PRIu64 ",\"validate_and_shortlist_ns\":%" PRIu64
                        ",\"rerank_ns\":%" PRIu64 ",\"rows\":%" PRIu64 ",\"shortlisted\":%zu}\n",
                verified - start, selected - verified, ranked - selected, header.count, found);
    }

done:
    free(heap);
    close_mapping(&query);
    close_mapping(&arena);
    return result;
}
