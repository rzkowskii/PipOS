/* WP-E16 off-device witness for the pinned Qwen3.5-0.8B Q6_K reflex.
 *
 * The wrapper passes pinned input descriptors and a new private staging dir.
 * Each synthetic case provides a prompt and a fixed continuation as token IDs.
 * Replay scores each continuation position with the unrestricted original head.
 */
#include "ggml-backend.h"
#include "ggml.h"
#include "gguf.h"
#include "llama.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace {
constexpr int Width = 1024;
constexpr int Vocabulary = 248320;
constexpr int Top = 20;
constexpr int Context = 4096;
constexpr int BatchSize = 256;
constexpr int MaxCases = 1024;
constexpr int MaxSteps = 2048;
constexpr int MaxContinuation = 64;
constexpr std::size_t MaxCaseLine = Context * 8 + 80;
static_assert(sizeof(float) == 4 && std::numeric_limits<float>::is_iec559);

using File = std::unique_ptr<FILE, decltype(&fclose)>;

void require(bool condition, const char *message)
{
    if (!condition) {
        throw std::runtime_error(message);
    }
}

File create(int folder, const char *name)
{
    int fd = openat(folder, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    require(fd >= 0, "cannot create a new witness artifact");
    FILE *file = fdopen(fd, "wb");
    if (file == nullptr) {
        close(fd);
        throw std::runtime_error("cannot open witness artifact");
    }
    return File(file, fclose);
}

void finish(File &file)
{
    FILE *output = file.release();
    const bool synced = fflush(output) == 0 && fsync(fileno(output)) == 0;
    const bool closed = fclose(output) == 0;
    require(synced && closed, "could not finish witness artifact");
}

struct Case {
    std::vector<llama_token> prompt;
    std::vector<llama_token> continuation;
};

bool id_char(char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
        || (c >= '0' && c <= '9') || c == '-' || c == '_';
}

std::vector<llama_token> tokens(std::string_view field, std::size_t limit)
{
    std::vector<llama_token> result;
    while (!field.empty()) {
        std::size_t space = field.find(' ');
        std::string_view word = field.substr(0, space);
        llama_token id;
        auto parsed = std::from_chars(word.data(), word.data() + word.size(), id);
        require(parsed.ec == std::errc{} && parsed.ptr == word.data() + word.size()
                && id >= 0 && id < Vocabulary && result.size() < limit, "invalid witness token ID");
        result.push_back(id);
        if (space == std::string_view::npos) break;
        field.remove_prefix(space + 1);
        require(!field.empty(), "trailing witness token delimiter");
    }
    require(!result.empty(), "prompt and continuation each need tokens");
    return result;
}

std::vector<Case> read_cases(const char *path)
{
    std::ifstream input(path, std::ios::binary);
    require(input.is_open(), "cannot read the audition case file");
    std::unordered_set<std::string> ids;
    std::vector<Case> cases;
    std::size_t steps = 0;
    std::array<char, MaxCaseLine> line{};
    while (input.getline(line.data(), line.size())) {
        std::string_view record(line.data(), std::strlen(line.data()));
        const std::size_t bytes = static_cast<std::size_t>(input.gcount()) - (input.eof() ? 0 : 1);
        const std::size_t left = record.find('\t');
        require(record.size() == bytes && left != std::string_view::npos
                && left > 0 && left <= 64, "each case must have an opaque ID");
        const std::size_t right = record.find('\t', left + 1);
        require(right != std::string_view::npos && record.find('\t', right + 1) == std::string_view::npos,
                "expected ID, prompt IDs and continuation IDs");
        const auto id = record.substr(0, left);
        require(std::all_of(id.begin(), id.end(), id_char) && ids.emplace(id).second
                && cases.size() < MaxCases, "duplicate or invalid audition case ID");
        auto prompt = tokens(record.substr(left + 1, right - left - 1), Context - 1);
        auto continuation = tokens(record.substr(right + 1), MaxContinuation);
        steps += continuation.size();
        require(prompt.size() + continuation.size() <= Context && steps <= MaxSteps,
                "witness steps or context exceed their bound");
        cases.push_back({std::move(prompt), std::move(continuation)});
    }
    require(input.eof() && !input.bad() && !cases.empty(), "case file is empty, truncated or oversized");
    return cases;
}

struct Head {
    std::size_t offset;
    std::size_t size;
    std::size_t row_size;
};

Head head_tensor(FILE *model)
{
    using Gguf = std::unique_ptr<gguf_context, decltype(&gguf_free)>;
    Gguf info(gguf_init_from_file_ptr(model, {true, nullptr}), gguf_free);
    require(bool(info), "cannot read GGUF header");
    const int64_t architecture = gguf_find_key(info.get(), "general.architecture");
    require(architecture >= 0 && gguf_get_kv_type(info.get(), architecture) == GGUF_TYPE_STRING
            && std::strcmp(gguf_get_val_str(info.get(), architecture), "qwen35") == 0
            && gguf_find_key(info.get(), "prism.hadamard.version") < 0
            && gguf_find_tensor(info.get(), "output.weight") < 0,
            "the witness requires the unrotated Qwen3.5 tied output head");
    const int64_t tensor = gguf_find_tensor(info.get(), "token_embd.weight");
    require(tensor >= 0 && gguf_get_tensor_type(info.get(), tensor) == GGML_TYPE_Q6_K,
            "tied output head is not Q6_K");
    const int64_t *shape = gguf_get_tensor_ne(info.get(), tensor);
    require(shape[0] == Width && shape[1] == Vocabulary && shape[2] == 1 && shape[3] == 1,
            "unexpected tied output head shape");
    const auto row_size = ggml_row_size(GGML_TYPE_Q6_K, Width);
    const auto total_size = gguf_get_tensor_size(info.get(), tensor);
    const auto base = gguf_get_data_offset(info.get());
    const auto offset = gguf_get_tensor_offset(info.get(), tensor);
    struct stat metadata;
    require(fstat(fileno(model), &metadata) == 0 && S_ISREG(metadata.st_mode) && metadata.st_size > 0,
            "model must be a regular GGUF file");
    const auto bytes = static_cast<uintmax_t>(metadata.st_size);
    require(base <= bytes && offset <= bytes - base && total_size == row_size * Vocabulary
            && total_size <= bytes - base - offset, "tied head falls outside the GGUF");
    return {base + offset, total_size, row_size};
}

void export_head(FILE *model, const Head &head, int folder)
{
    auto output = create(folder, "head.f32le");
    require(fseeko(model, static_cast<off_t>(head.offset), SEEK_SET) == 0, "could not seek to the tied head");
    const auto *format = ggml_get_type_traits(GGML_TYPE_Q6_K);
    require(format != nullptr && format->to_float != nullptr, "pinned ggml Q6_K dequantizer is unavailable");
    std::vector<uint8_t> packed(head.row_size);
    std::array<float, Width> row{};
    for (int i = 0; i < Vocabulary; ++i) {
        require(fread(packed.data(), 1, packed.size(), model) == packed.size(), "short read of the tied output head");
        format->to_float(packed.data(), row.data(), Width);
        require(std::all_of(row.begin(), row.end(), [](float x) { return std::isfinite(x); }),
                "non-finite dequantized output weight");
        require(fwrite(row.data(), sizeof(float), row.size(), output.get()) == row.size(),
                "could not write dequantized output head");
    }
    finish(output);
}

struct Activation {
    std::array<float, Width> hidden{};
    unsigned received = 0;
    bool armed = false;
    bool valid = false;
};

bool observe(ggml_tensor *tensor, bool ask, void *opaque)
{
    auto &activation = *static_cast<Activation *>(opaque);
    if (ask) {
        return activation.armed && std::strcmp(ggml_get_name(tensor), "result_norm") == 0;
    }
    ++activation.received;
    activation.valid = activation.received == 1 && tensor->type == GGML_TYPE_F32
        && tensor->ne[0] == Width && tensor->ne[1] == 1 && tensor->ne[2] == 1 && tensor->ne[3] == 1
        && ggml_is_contiguous(tensor);
    if (!activation.valid) return false;
    ggml_backend_tensor_get(tensor, activation.hidden.data(), 0, Width * sizeof(float));
    activation.valid = std::all_of(activation.hidden.begin(), activation.hidden.end(),
                                   [](float x) { return std::isfinite(x); });
    return activation.valid;
}

void queue(llama_batch &batch, std::size_t index, llama_token token, std::size_t position, bool logits)
{
    batch.token[index] = token;
    batch.pos[index] = static_cast<llama_pos>(position);
    batch.n_seq_id[index] = 1;
    batch.seq_id[index][0] = 0;
    batch.logits[index] = logits;
}

class Batch {
public:
    Batch() : value(llama_batch_init(BatchSize, 0, 1)) {}
    ~Batch() { llama_batch_free(value); }
    Batch(const Batch &) = delete;
    Batch &operator=(const Batch &) = delete;

    llama_batch value;
};

class Backend {
public:
    Backend() { llama_backend_init(); }
    ~Backend() { llama_backend_free(); }
    Backend(const Backend &) = delete;
    Backend &operator=(const Backend &) = delete;
};

std::size_t replay(const char *path, const std::vector<Case> &cases, int folder)
{
    auto parameters = llama_model_default_params();
    parameters.n_gpu_layers = 0;
    parameters.use_extra_bufts = false;
    using Model = std::unique_ptr<llama_model, decltype(&llama_model_free)>;
    Model model(llama_model_load_from_file(path, parameters), llama_model_free);
    require(bool(model) && llama_vocab_n_tokens(llama_model_get_vocab(model.get())) == Vocabulary,
            "could not load the pinned reflex geometry");

    Activation phase;
    auto config = llama_context_default_params();
    config.n_ctx = Context;
    config.n_batch = BatchSize;
    config.n_ubatch = BatchSize;
    config.n_seq_max = 1;
    config.n_threads = 2;
    config.n_threads_batch = 2;
    config.embeddings = false;
    config.offload_kqv = false;
    config.op_offload = false;
    config.samplers = nullptr;
    config.n_samplers = 0;
    config.cb_eval = observe;
    config.cb_eval_user_data = &phase;
    using ContextHandle = std::unique_ptr<llama_context, decltype(&llama_free)>;
    ContextHandle context(llama_init_from_model(model.get(), config), llama_free);
    require(bool(context) && llama_get_memory(context.get()) != nullptr, "could not initialize witness context");

    auto hidden = create(folder, "hidden.f32le");
    auto truth = create(folder, "truth_top20.u32le");
    Batch batch;
    std::vector<uint32_t> ids(Vocabulary);
    std::size_t steps = 0;
    for (const auto &item : cases) {
        llama_memory_clear(llama_get_memory(context.get()), true);
        std::size_t position = 0;
        phase.armed = false;
        const std::size_t prefix = item.prompt.size() - 1;
        while (position < prefix) {
            const std::size_t length = std::min<std::size_t>(BatchSize, prefix - position);
            for (std::size_t i = 0; i < length; ++i) {
                queue(batch.value, i, item.prompt[position + i], position + i, false);
            }
            batch.value.n_tokens = static_cast<int>(length);
            require(llama_decode(context.get(), batch.value) == 0, "could not prefill the witness prefix");
            llama_synchronize(context.get());
            position += length;
        }
        llama_token previous = item.prompt.back();
        for (llama_token next : item.continuation) {
            phase.armed = true;
            phase.received = 0;
            phase.valid = false;
            queue(batch.value, 0, previous, position++, true);
            batch.value.n_tokens = 1;
            require(llama_decode(context.get(), batch.value) == 0, "witness decode failed");
            const float *logits = llama_get_logits_ith(context.get(), 0);
            require(phase.valid && phase.received == 1 && logits != nullptr,
                    "could not pair the exact final activation and logits");
            for (uint32_t id = 0; id < Vocabulary; ++id) {
                require(std::isfinite(logits[id]), "unrestricted head contains a non-finite logit");
                ids[id] = id;
            }
            std::partial_sort(ids.begin(), ids.begin() + Top, ids.end(), [logits](uint32_t a, uint32_t b) {
                if (logits[a] != logits[b]) return logits[a] > logits[b];
                return a < b;
            });
            require(fwrite(phase.hidden.data(), sizeof(float), Width, hidden.get()) == Width
                    && fwrite(ids.data(), sizeof(uint32_t), Top, truth.get()) == Top,
                    "could not write paired witness step");
            previous = next;
            ++steps;
        }
    }
    finish(hidden);
    finish(truth);
    return steps;
}
} // namespace

int main(int argc, char **argv)
{
    if (argc != 4 || std::getenv("LLAMA_VOCAB_SHORTLIST") != nullptr) {
        std::fputs("usage: head_witness PINNED_GGUF PINNED_CASES PRIVATE_STAGING_DIR\n", stderr);
        return 2;
    }
    const uint32_t endian = 1;
    if (*reinterpret_cast<const uint8_t *>(&endian) != 1) {
        std::fputs("witness requires little-endian host\n", stderr);
        return 2;
    }
    int folder = open(argv[3], O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW);
    if (folder < 0) return 2;
    struct stat destination;
    int status = 1;
    try {
        require(fstat(folder, &destination) == 0 && destination.st_uid == geteuid()
                && (destination.st_mode & 077) == 0, "witness staging directory must be owner-only");
        const auto cases = read_cases(argv[2]);
        Backend backend;
        File source(fopen(argv[1], "rb"), fclose);
        require(bool(source), "could not open pinned GGUF");
        const Head head = head_tensor(source.get());
        const std::size_t steps = replay(argv[1], cases, folder);
        export_head(source.get(), head, folder);
        auto receipt = create(folder, "native-witness.json");
        require(fprintf(receipt.get(), "{\"schema\":1,\"steps\":%zu,\"head_offset\":%zu,\"head_bytes\":%zu}\n",
                        steps, head.offset, head.size) >= 0, "could not write witness receipt");
        finish(receipt);
        status = 0;
    } catch (const std::exception &error) {
        std::fprintf(stderr, "head_witness: %s\n", error.what());
    }
    close(folder);
    return status;
}
