/* Link rig: hardware seams only. The voice owner, the transport wrapper, the
 * protocol model, the WebSocket client, tcp_transport, esp-tls, mbedTLS and
 * lwIP are the pinned production sources. See ../README.md for what this does
 * and does not prove. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

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
