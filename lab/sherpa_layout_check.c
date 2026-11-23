/*
 * Print the C layout of every sherpa-onnx struct that mind/sherpa.py mirrors
 * in ctypes: one "struct <Name> <sizeof>" line, then one
 * "field <Name>.<member> <offsetof>" line for each member.
 *
 * tests/test_sherpa_layout.py compiles this on the host against the c-api.h
 * of each downloaded release and compares every number with ctypes. The
 * structs hold only pointers, 32-bit integers, floats and one 64-bit integer,
 * so an LP64 host has the laptop's x86-64 layout.
 *
 *   cc -I <release>/include -o layout lab/sherpa_layout_check.c && ./layout
 *
 * A member named here that the header lacks fails the compile; a member the
 * header has and this list lacks shows up as a size or offset difference.
 */
#include <stddef.h>
#include <stdio.h>

#include "sherpa-onnx/c-api/c-api.h"

#define S(T) printf("struct %s %zu\n", #T, sizeof(SherpaOnnx##T))
#define F(T, m) printf("field %s.%s %zu\n", #T, #m, offsetof(SherpaOnnx##T, m))

int main(void) {
  S(SileroVadModelConfig);
  F(SileroVadModelConfig, model); F(SileroVadModelConfig, threshold);
  F(SileroVadModelConfig, min_silence_duration);
  F(SileroVadModelConfig, min_speech_duration);
  F(SileroVadModelConfig, window_size);
  F(SileroVadModelConfig, max_speech_duration);
  S(TenVadModelConfig);
  F(TenVadModelConfig, model); F(TenVadModelConfig, threshold);
  F(TenVadModelConfig, min_silence_duration);
  F(TenVadModelConfig, min_speech_duration); F(TenVadModelConfig, window_size);
  F(TenVadModelConfig, max_speech_duration);
  S(VadModelConfig);
  F(VadModelConfig, silero_vad); F(VadModelConfig, sample_rate);
  F(VadModelConfig, num_threads); F(VadModelConfig, provider);
  F(VadModelConfig, debug); F(VadModelConfig, ten_vad);
  S(SpeechSegment);
  F(SpeechSegment, start); F(SpeechSegment, samples); F(SpeechSegment, n);
  S(FeatureConfig);
  F(FeatureConfig, sample_rate); F(FeatureConfig, feature_dim);
  S(OfflineTransducerModelConfig);
  F(OfflineTransducerModelConfig, encoder);
  F(OfflineTransducerModelConfig, decoder);
  F(OfflineTransducerModelConfig, joiner);
  S(OfflineParaformerModelConfig);
  F(OfflineParaformerModelConfig, model);
  S(OfflineNemoEncDecCtcModelConfig);
  F(OfflineNemoEncDecCtcModelConfig, model);
  S(OfflineWhisperModelConfig);
  F(OfflineWhisperModelConfig, encoder); F(OfflineWhisperModelConfig, decoder);
  F(OfflineWhisperModelConfig, language); F(OfflineWhisperModelConfig, task);
  F(OfflineWhisperModelConfig, tail_paddings);
  F(OfflineWhisperModelConfig, enable_token_timestamps);
  F(OfflineWhisperModelConfig, enable_segment_timestamps);
  S(OfflineCanaryModelConfig);
  F(OfflineCanaryModelConfig, encoder); F(OfflineCanaryModelConfig, decoder);
  F(OfflineCanaryModelConfig, src_lang); F(OfflineCanaryModelConfig, tgt_lang);
  F(OfflineCanaryModelConfig, use_pnc);
  S(OfflineCohereTranscribeModelConfig);
  F(OfflineCohereTranscribeModelConfig, encoder);
  F(OfflineCohereTranscribeModelConfig, decoder);
  F(OfflineCohereTranscribeModelConfig, language);
  F(OfflineCohereTranscribeModelConfig, use_punct);
  F(OfflineCohereTranscribeModelConfig, use_itn);
  S(OfflineFireRedAsrModelConfig);
  F(OfflineFireRedAsrModelConfig, encoder);
  F(OfflineFireRedAsrModelConfig, decoder);
  S(OfflineFireRedAsrCtcModelConfig);
  F(OfflineFireRedAsrCtcModelConfig, model);
  S(OfflineMoonshineModelConfig);
  F(OfflineMoonshineModelConfig, preprocessor);
  F(OfflineMoonshineModelConfig, encoder);
  F(OfflineMoonshineModelConfig, uncached_decoder);
  F(OfflineMoonshineModelConfig, cached_decoder);
  F(OfflineMoonshineModelConfig, merged_decoder);
  S(OfflineTdnnModelConfig);
  F(OfflineTdnnModelConfig, model);
  S(OfflineLMConfig);
  F(OfflineLMConfig, model); F(OfflineLMConfig, scale);
  S(OfflineSenseVoiceModelConfig);
  F(OfflineSenseVoiceModelConfig, model);
  F(OfflineSenseVoiceModelConfig, language);
  F(OfflineSenseVoiceModelConfig, use_itn);
  S(OfflineDolphinModelConfig);
  F(OfflineDolphinModelConfig, model);
  S(OfflineZipformerCtcModelConfig);
  F(OfflineZipformerCtcModelConfig, model);
  S(OfflineWenetCtcModelConfig);
  F(OfflineWenetCtcModelConfig, model);
  S(OfflineOmnilingualAsrCtcModelConfig);
  F(OfflineOmnilingualAsrCtcModelConfig, model);
  S(OfflineFunASRNanoModelConfig);
  F(OfflineFunASRNanoModelConfig, encoder_adaptor);
  F(OfflineFunASRNanoModelConfig, llm);
  F(OfflineFunASRNanoModelConfig, embedding);
  F(OfflineFunASRNanoModelConfig, tokenizer);
  F(OfflineFunASRNanoModelConfig, system_prompt);
  F(OfflineFunASRNanoModelConfig, user_prompt);
  F(OfflineFunASRNanoModelConfig, max_new_tokens);
  F(OfflineFunASRNanoModelConfig, temperature);
  F(OfflineFunASRNanoModelConfig, top_p);
  F(OfflineFunASRNanoModelConfig, seed);
  F(OfflineFunASRNanoModelConfig, language);
  F(OfflineFunASRNanoModelConfig, itn);
  F(OfflineFunASRNanoModelConfig, hotwords);
  S(OfflineQwen3ASRModelConfig);
  F(OfflineQwen3ASRModelConfig, conv_frontend);
  F(OfflineQwen3ASRModelConfig, encoder);
  F(OfflineQwen3ASRModelConfig, decoder);
  F(OfflineQwen3ASRModelConfig, tokenizer);
  F(OfflineQwen3ASRModelConfig, max_total_len);
  F(OfflineQwen3ASRModelConfig, max_new_tokens);
  F(OfflineQwen3ASRModelConfig, temperature);
  F(OfflineQwen3ASRModelConfig, top_p); F(OfflineQwen3ASRModelConfig, seed);
  F(OfflineQwen3ASRModelConfig, hotwords);
  S(OfflineMedAsrCtcModelConfig);
  F(OfflineMedAsrCtcModelConfig, model);
  S(OfflineModelConfig);
  F(OfflineModelConfig, transducer); F(OfflineModelConfig, paraformer);
  F(OfflineModelConfig, nemo_ctc); F(OfflineModelConfig, whisper);
  F(OfflineModelConfig, tdnn); F(OfflineModelConfig, tokens);
  F(OfflineModelConfig, num_threads); F(OfflineModelConfig, debug);
  F(OfflineModelConfig, provider); F(OfflineModelConfig, model_type);
  F(OfflineModelConfig, modeling_unit); F(OfflineModelConfig, bpe_vocab);
  F(OfflineModelConfig, telespeech_ctc); F(OfflineModelConfig, sense_voice);
  F(OfflineModelConfig, moonshine); F(OfflineModelConfig, fire_red_asr);
  F(OfflineModelConfig, dolphin); F(OfflineModelConfig, zipformer_ctc);
  F(OfflineModelConfig, canary); F(OfflineModelConfig, wenet_ctc);
  F(OfflineModelConfig, omnilingual); F(OfflineModelConfig, medasr);
  F(OfflineModelConfig, funasr_nano); F(OfflineModelConfig, fire_red_asr_ctc);
  F(OfflineModelConfig, qwen3_asr); F(OfflineModelConfig, cohere_transcribe);
  S(HomophoneReplacerConfig);
  F(HomophoneReplacerConfig, dict_dir); F(HomophoneReplacerConfig, lexicon);
  F(HomophoneReplacerConfig, rule_fsts);
  S(OfflineCtcFstDecoderConfig);
  F(OfflineCtcFstDecoderConfig, graph);
  F(OfflineCtcFstDecoderConfig, max_active);
  S(OfflineRecognizerConfig);
  F(OfflineRecognizerConfig, feat_config);
  F(OfflineRecognizerConfig, model_config);
  F(OfflineRecognizerConfig, lm_config);
  F(OfflineRecognizerConfig, decoding_method);
  F(OfflineRecognizerConfig, max_active_paths);
  F(OfflineRecognizerConfig, hotwords_file);
  F(OfflineRecognizerConfig, hotwords_score);
  F(OfflineRecognizerConfig, rule_fsts); F(OfflineRecognizerConfig, rule_fars);
  F(OfflineRecognizerConfig, blank_penalty); F(OfflineRecognizerConfig, hr);
  F(OfflineRecognizerConfig, ctc_fst_decoder_config);
  S(OfflineRecognizerResult);
  F(OfflineRecognizerResult, text); F(OfflineRecognizerResult, timestamps);
  F(OfflineRecognizerResult, count); F(OfflineRecognizerResult, tokens);
  F(OfflineRecognizerResult, tokens_arr); F(OfflineRecognizerResult, json);
  F(OfflineRecognizerResult, lang); F(OfflineRecognizerResult, emotion);
  F(OfflineRecognizerResult, event); F(OfflineRecognizerResult, durations);
  F(OfflineRecognizerResult, ys_log_probs);
  F(OfflineRecognizerResult, segment_timestamps);
  F(OfflineRecognizerResult, segment_durations);
  F(OfflineRecognizerResult, segment_texts);
  F(OfflineRecognizerResult, segment_texts_arr);
  F(OfflineRecognizerResult, segment_count);
  S(OfflineTtsVitsModelConfig);
  F(OfflineTtsVitsModelConfig, model); F(OfflineTtsVitsModelConfig, lexicon);
  F(OfflineTtsVitsModelConfig, tokens); F(OfflineTtsVitsModelConfig, data_dir);
  F(OfflineTtsVitsModelConfig, noise_scale);
  F(OfflineTtsVitsModelConfig, noise_scale_w);
  F(OfflineTtsVitsModelConfig, length_scale);
  F(OfflineTtsVitsModelConfig, dict_dir);
  S(OfflineTtsMatchaModelConfig);
  F(OfflineTtsMatchaModelConfig, acoustic_model);
  F(OfflineTtsMatchaModelConfig, vocoder);
  F(OfflineTtsMatchaModelConfig, lexicon);
  F(OfflineTtsMatchaModelConfig, tokens);
  F(OfflineTtsMatchaModelConfig, data_dir);
  F(OfflineTtsMatchaModelConfig, noise_scale);
  F(OfflineTtsMatchaModelConfig, length_scale);
  F(OfflineTtsMatchaModelConfig, dict_dir);
  S(OfflineTtsKokoroModelConfig);
  F(OfflineTtsKokoroModelConfig, model);
  F(OfflineTtsKokoroModelConfig, voices);
  F(OfflineTtsKokoroModelConfig, tokens);
  F(OfflineTtsKokoroModelConfig, data_dir);
  F(OfflineTtsKokoroModelConfig, length_scale);
  F(OfflineTtsKokoroModelConfig, dict_dir);
  F(OfflineTtsKokoroModelConfig, lexicon);
  F(OfflineTtsKokoroModelConfig, lang);
  S(OfflineTtsKittenModelConfig);
  F(OfflineTtsKittenModelConfig, model);
  F(OfflineTtsKittenModelConfig, voices);
  F(OfflineTtsKittenModelConfig, tokens);
  F(OfflineTtsKittenModelConfig, data_dir);
  F(OfflineTtsKittenModelConfig, length_scale);
  F(OfflineTtsKittenModelConfig, lexicon);
  S(OfflineTtsZipvoiceModelConfig);
  F(OfflineTtsZipvoiceModelConfig, tokens);
  F(OfflineTtsZipvoiceModelConfig, encoder);
  F(OfflineTtsZipvoiceModelConfig, decoder);
  F(OfflineTtsZipvoiceModelConfig, vocoder);
  F(OfflineTtsZipvoiceModelConfig, data_dir);
  F(OfflineTtsZipvoiceModelConfig, lexicon);
  F(OfflineTtsZipvoiceModelConfig, feat_scale);
  F(OfflineTtsZipvoiceModelConfig, t_shift);
  F(OfflineTtsZipvoiceModelConfig, target_rms);
  F(OfflineTtsZipvoiceModelConfig, guidance_scale);
  S(OfflineTtsPocketModelConfig);
  F(OfflineTtsPocketModelConfig, lm_flow);
  F(OfflineTtsPocketModelConfig, lm_main);
  F(OfflineTtsPocketModelConfig, encoder);
  F(OfflineTtsPocketModelConfig, decoder);
  F(OfflineTtsPocketModelConfig, text_conditioner);
  F(OfflineTtsPocketModelConfig, vocab_json);
  F(OfflineTtsPocketModelConfig, token_scores_json);
  F(OfflineTtsPocketModelConfig, voice_embedding_cache_capacity);
  S(OfflineTtsSupertonicModelConfig);
  F(OfflineTtsSupertonicModelConfig, duration_predictor);
  F(OfflineTtsSupertonicModelConfig, text_encoder);
  F(OfflineTtsSupertonicModelConfig, vector_estimator);
  F(OfflineTtsSupertonicModelConfig, vocoder);
  F(OfflineTtsSupertonicModelConfig, tts_json);
  F(OfflineTtsSupertonicModelConfig, unicode_indexer);
  F(OfflineTtsSupertonicModelConfig, voice_style);
  S(OfflineTtsModelConfig);
  F(OfflineTtsModelConfig, vits); F(OfflineTtsModelConfig, num_threads);
  F(OfflineTtsModelConfig, debug); F(OfflineTtsModelConfig, provider);
  F(OfflineTtsModelConfig, matcha); F(OfflineTtsModelConfig, kokoro);
  F(OfflineTtsModelConfig, kitten); F(OfflineTtsModelConfig, zipvoice);
  F(OfflineTtsModelConfig, pocket); F(OfflineTtsModelConfig, supertonic);
  S(OfflineTtsConfig);
  F(OfflineTtsConfig, model); F(OfflineTtsConfig, rule_fsts);
  F(OfflineTtsConfig, max_num_sentences); F(OfflineTtsConfig, rule_fars);
  F(OfflineTtsConfig, silence_scale);
  S(GeneratedAudio);
  F(GeneratedAudio, samples); F(GeneratedAudio, n);
  F(GeneratedAudio, sample_rate);
  S(GenerationConfig);
  F(GenerationConfig, silence_scale); F(GenerationConfig, speed);
  F(GenerationConfig, sid); F(GenerationConfig, reference_audio);
  F(GenerationConfig, reference_audio_len);
  F(GenerationConfig, reference_sample_rate);
  F(GenerationConfig, reference_text); F(GenerationConfig, num_steps);
  F(GenerationConfig, extra); F(GenerationConfig, phoneme_codepoints);
  F(GenerationConfig, phoneme_codepoints_lens);
  F(GenerationConfig, phoneme_codepoints_num_sentences);
  F(GenerationConfig, tokens); F(GenerationConfig, tokens_lens);
  F(GenerationConfig, tokens_num_sentences);
  return 0;
}
