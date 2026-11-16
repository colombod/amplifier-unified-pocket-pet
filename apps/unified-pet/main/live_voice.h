#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
typedef enum { LIVE_OFF,LIVE_CONNECTING,LIVE_LISTENING,LIVE_THINKING,LIVE_SPEAKING,LIVE_CLOSING,LIVE_ERROR } live_state_t;
typedef struct { bool active; live_state_t state; unsigned seconds; char message[112]; } live_status_t;
void live_voice_init(void);
/* The one microphone button only: mic on starts a call, mic off ends it.
 * Nothing here ever starts, reconnects or resumes a call by itself. */
void live_voice_start(void);
void live_voice_stop(void);
/* Reclaim only a finished, suspended worker before releasing shared admission. */
void live_voice_reap_finished(void);
void live_voice_snapshot(live_status_t *out);
/* Fixed metadata only; internal adapter seam, never provider/transcript text. */
void live_voice_bridge_status(live_state_t state,const char *message);
