#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct {uint32_t deadline,delay_ms;bool scheduled;} network_retry_t;
static inline void network_retry_reset(network_retry_t *r){*r=(network_retry_t){0};}
static inline void network_retry_failed(network_retry_t *r,uint32_t now) {
 r->delay_ms=r->delay_ms?r->delay_ms*2:5000;
 if(r->delay_ms>60000)r->delay_ms=60000;
 r->deadline=now+r->delay_ms;r->scheduled=true;
}
static inline bool network_retry_due(const network_retry_t *r,uint32_t now,bool connected,bool joining,bool queued,bool setup_seen,uint32_t setup_at) {
 return r->scheduled && !connected && !joining && !queued &&
 (!setup_seen || (uint32_t)(now-setup_at)>=60000) && (int32_t)(now-r->deadline)>=0;
}
