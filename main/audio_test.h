#pragma once
#include <stdbool.h>
#include "esp_err.h"
typedef struct { bool busy; int volume; char message[112]; } audio_test_status_t;
esp_err_t audio_hardware_quiet(void);
void audio_tests_init(void);
void audio_test_request(bool microphone);
void audio_test_snapshot(audio_test_status_t *out);

void audio_test_set_volume(int percent);
void audio_test_save_volume(void);

/* One codec owner at a time. These are device-only, 24 kHz PCM16 mono streams.
 * Opening captures audio, so callers must first receive an explicit Talk action. */
typedef struct audio_session audio_session_t;
esp_err_t audio_stream_open(audio_session_t **out);
esp_err_t audio_stream_read(audio_session_t *session, void *pcm, unsigned bytes);
esp_err_t audio_stream_write(audio_session_t *session, void *pcm, unsigned bytes);
void audio_stream_close(audio_session_t *session);
