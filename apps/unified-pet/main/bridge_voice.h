#pragma once
#include "nvs.h"
#include "cJSON.h"
#include "setup_bridge.h"
#include "bridge_protocol.h"
/* Adapter invoked only inside the existing explicit-Talk live worker. */
void bridge_voice_init(void);
/* One mic-on. first_deadline_us bounds the first call; a call re-established
 * after a lost connection is bounded by call_limit_us from its own start.
 * Returns when the mic-on is over: the user's mic off, an ordinary or
 * non-retryable end, or the reconnect budget running out. */
void bridge_voice_run(const setup_bridge_t *cfg,int64_t first_deadline_us,int64_t call_limit_us,bool (*parent_stopped)(void));
/* Mic off. Ends the pet's call only; Unified work already asked for continues. */
void bridge_voice_stop(void);
bool bridge_voice_snapshot(bridge_view_t *view);
/* One-turn preview/Send entry points. bridge_voice_run starts conversation mode
 * only and no screen calls these; they remain for the one-turn lifecycle tests
 * and act only on a one-turn model, which the firmware no longer creates. */
void bridge_voice_finish_capture(void);
void bridge_voice_confirm(uint32_t revision);
bool bridge_voice_copy_preview(char *display,size_t capacity,uint32_t *revision);
bool bridge_voice_restart_required(void);
/* Owner already suspended; zero-wait late cleanup, never connects or sends. */
void bridge_voice_reap_finished(void);
#if defined(PET_PRIVATE_MIC_DIAG)
/* Snapshotted once per call; never retunes an already running call. */
bool bridge_voice_bench_jitter(unsigned ms);
void bridge_voice_bench_audio(unsigned priority,int core);
void bridge_voice_bench_last(unsigned back);
#endif
