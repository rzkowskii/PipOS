// Expose the selected OpenJev hidden states without serialising every token
// embedding or requiring the Python caller to mirror llama.cpp's ABI structs.
#include "llama.h"

#include <algorithm>
#include <cstdio>
#include <cstdint>
#include <memory>

struct OpenJevBackbone {
    std::unique_ptr<llama_model, decltype(&llama_model_free)> model{nullptr, llama_model_free};
    std::unique_ptr<llama_context, decltype(&llama_free)> context{nullptr, llama_free};
    int32_t max_tokens;
    int32_t max_questions;
};

extern "C" OpenJevBackbone * openjev_load(
    const char * path, int32_t threads, int32_t max_tokens, int32_t max_questions
) noexcept {
    if (!path || threads < 1 || max_tokens < 1 || max_questions < 1) {
        return nullptr;
    }
    try {
        static const bool backend_ready = [] {
            llama_log_set([](ggml_log_level level, const char * message, void *) {
                if (level == GGML_LOG_LEVEL_WARN || level == GGML_LOG_LEVEL_ERROR) {
                    std::fputs(message, stderr);
                }
            }, nullptr);
            llama_backend_init();
            return true;
        }();
        (void) backend_ready;
        auto result = std::make_unique<OpenJevBackbone>();
        auto model_params = llama_model_default_params();
        model_params.n_gpu_layers = 0;
        result->model.reset(llama_model_load_from_file(path, model_params));
        if (!result->model) {
            return nullptr;
        }

        auto params = llama_context_default_params();
        params.n_ctx = max_tokens;
        params.n_batch = max_tokens;
        params.n_ubatch = max_tokens;
        params.n_seq_max = max_questions;
        params.n_threads = threads;
        params.n_threads_batch = threads;
        params.embeddings = true;
        params.pooling_type = LLAMA_POOLING_TYPE_NONE;
        params.attention_type = LLAMA_ATTENTION_TYPE_CAUSAL;
        params.offload_kqv = false;
        params.op_offload = false;
        params.kv_unified = true;
        result->context.reset(llama_init_from_model(result->model.get(), params));
        if (!result->context) {
            return nullptr;
        }
        result->max_tokens = max_tokens;
        result->max_questions = max_questions;
        return result.release();
    } catch (...) {
        return nullptr;
    }
}

extern "C" void openjev_free(OpenJevBackbone * model) noexcept {
    delete model;
}

extern "C" int32_t openjev_width(const OpenJevBackbone * model) noexcept {
    return model ? llama_model_n_embd_out(model->model.get()) : 0;
}

extern "C" int openjev_hidden(
    OpenJevBackbone * model, int32_t n_tokens, int32_t questions,
    const int32_t * tokens, const int32_t * positions, const int32_t * segments,
    int32_t n_readouts, const int32_t * readouts, float * output
) noexcept {
    if (!model || !tokens || !positions || !segments || !readouts || !output ||
        n_tokens < 1 || n_tokens > model->max_tokens ||
        questions < 1 || questions > model->max_questions || n_readouts < 1 || n_readouts > n_tokens) {
        return 1;
    }
    for (int32_t i = 0; i < n_tokens; ++i) {
        if (positions[i] < 0 || positions[i] >= model->max_tokens || segments[i] < 0 || segments[i] > questions) {
            return 1;
        }
    }
    for (int32_t i = 0; i < n_readouts; ++i) {
        if (readouts[i] < 0 || readouts[i] >= n_tokens) {
            return 1;
        }
    }

    auto * context = model->context.get();
    llama_memory_clear(llama_get_memory(context), false);
    auto batch = llama_batch_init(n_tokens, 0, questions);
    batch.n_tokens = n_tokens;
    for (int32_t i = 0; i < n_tokens; ++i) {
        batch.token[i] = tokens[i];
        batch.pos[i] = positions[i];
        batch.logits[i] = 0;
        // A state token belongs to every question. A question token can
        // attend its own causal branch and the shared state, once per request.
        batch.n_seq_id[i] = segments[i] == 0 ? questions : 1;
        for (int32_t q = 0; q < batch.n_seq_id[i]; ++q) {
            batch.seq_id[i][q] = segments[i] == 0 ? q : segments[i] - 1;
        }
    }
    for (int32_t i = 0; i < n_readouts; ++i) {
        batch.logits[readouts[i]] = 1;
    }

    const int status = llama_decode(context, batch);
    llama_batch_free(batch);
    if (status != 0) {
        return 2;
    }
    const int32_t width = openjev_width(model);
    for (int32_t i = 0; i < n_readouts; ++i) {
        const float * row = llama_get_embeddings_ith(context, readouts[i]);
        if (!row) {
            return 3;
        }
        std::copy_n(row, width, output + i * width);
    }
    return 0;
}
