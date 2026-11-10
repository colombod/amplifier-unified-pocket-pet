#pragma once
#include <stdbool.h>
typedef enum { LIVE_OFF,LIVE_CONNECTING,LIVE_LISTENING,LIVE_THINKING,LIVE_SPEAKING,LIVE_CLOSING,LIVE_ERROR } live_state_t;
typedef struct { bool active; live_state_t state; unsigned seconds; char message[112]; } live_status_t;
void live_voice_init(void);
void live_voice_start(void); /* Explicit Talk button only. */
void live_voice_stop(void);
void live_voice_snapshot(live_status_t *out);
