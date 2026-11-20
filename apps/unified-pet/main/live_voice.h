#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
/* LIVE_RECONNECTING: the mic is still on, but the host connection was lost and
 * is being re-established; no microphone capture or speaker output meanwhile. */
typedef enum { LIVE_OFF,LIVE_CONNECTING,LIVE_LISTENING,LIVE_THINKING,LIVE_SPEAKING,LIVE_CLOSING,LIVE_ERROR,
    LIVE_RECONNECTING } live_state_t;
typedef struct { bool active; live_state_t state; unsigned seconds; char message[112]; } live_status_t;
void live_voice_init(void);
/* The one microphone button only: mic on starts a call, mic off ends it.
 * Nothing here starts a call by itself. While the mic is on, a host-bridge
 * conversation whose CONNECTION was lost is re-established (user decision,
 * 15 November 2026); mic off always ends that at once. */
void live_voice_start(void);
void live_voice_stop(void);
#if defined(PET_PRIVATE_MIC_DIAG)
/* Bench only: core for the next call's owner task; negative leaves it unpinned. */
void live_voice_bench_core(int core);
#endif
/* Reclaim only a finished, suspended worker before releasing shared admission. */
void live_voice_reap_finished(void);
void live_voice_snapshot(live_status_t *out);
/* Fixed metadata only; internal adapter seam, never provider/transcript text. */
void live_voice_bridge_status(live_state_t state,const char *message);
/* Short audio-gate admission. A true result holds the same lock as mic-off;
 * the caller must release it immediately after publishing gate state. */
bool live_voice_bridge_audio_begin(void);
void live_voice_bridge_audio_end(void);
