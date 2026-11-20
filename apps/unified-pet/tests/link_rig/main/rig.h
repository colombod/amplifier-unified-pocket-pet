/* Link rig: hardware seams only. The voice owner, the transport wrapper, the
 * protocol model, the WebSocket client, tcp_transport, esp-tls, mbedTLS and
 * lwIP are the pinned production sources. See ../README.md for what this does
 * and does not prove. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* The Linux SDK port has one simulated CPU and no pinned-create API.
 * Check and record the requested affinity, then use its real task scheduler.
 * This seam does NOT qualify ESP32 dual-core scheduling or codec deadlines. */
#ifndef tskNO_AFFINITY
#define tskNO_AFFINITY 0x7fffffff
#endif
BaseType_t rig_create_pinned(TaskFunction_t fn,const char *name,uint32_t stack,void *arg,
                            UBaseType_t priority,TaskHandle_t *out,BaseType_t core);
#define xTaskCreatePinnedToCore rig_create_pinned

/* Milliseconds since the device first showed "listening" (0 before that). */
int64_t rig_since_listening_ms(void);
bool rig_listening_seen(void);
long rig_env(const char *name,long fallback);
const char *rig_env_text(const char *name);

void rig_seams_init(void);
bool rig_parent_stopped(void);
void rig_parent_stop(void);
void rig_report(void);

void rig_link_start(void);
void rig_link_report(void);
void rig_probe_report(void);
