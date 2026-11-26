#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
typedef enum {
    TAILNET_IDLE,TAILNET_PREFLIGHT,TAILNET_TIME,TAILNET_IDENTITY,TAILNET_JOIN,
    TAILNET_PEER,TAILNET_TCP,TAILNET_TLS,TAILNET_HTTP,TAILNET_CLEANUP,
    TAILNET_PASS,TAILNET_ERROR,TAILNET_CANCELLED
} tailnet_spike_state_t;
typedef struct {
    tailnet_spike_state_t state;
    bool active,key_present,identity_loaded,persisted,bootstrap,joined,peer,tcp,tls,http;
    bool cleaned_up,restart_required,listener_paused,clock_qualified;
    int error,error_stage,http_status,tls_error;
    uint32_t tls_verify_flags,elapsed_ms,stage_ms[10];
    uint32_t internal_min,largest_internal_min,psram_min,stack_min;
    char fingerprint[65];
} tailnet_spike_status_t;
/* Local-only init: creates idle worker and bounded USB command receiver.
 * No enrollment, key read, target probe or audio is started here. */
esp_err_t tailnet_spike_init(void);
esp_err_t tailnet_spike_start(void);
void tailnet_spike_cancel(void);
void tailnet_spike_snapshot(tailnet_spike_status_t *out);
const char *tailnet_spike_state_name(tailnet_spike_state_t state);
/* Same atomic admission gate used by local audio, voice, touch and USB. */
bool tailnet_spike_audio_acquire(void);
void tailnet_spike_audio_release(void);
/* Only claim from idle, after the normal audio/voice reapers release ownership. */
bool tailnet_spike_power_claim(void);
void tailnet_spike_power_release(void);