#pragma once
#include <assert.h>
#include <limits.h>
#include <stdbool.h>
#include <stdatomic.h>

/* An exclusive power claim succeeds only when no admitted activity remains.
 * A queued operation takes its own lease before enqueue and transfers it to
 * the worker; checking the queue length would miss dequeued, running work. */
typedef struct { atomic_uint users; } power_gate_t;

static inline bool power_activity_begin(power_gate_t *gate)
{
    unsigned users=atomic_load(&gate->users);
    do {
        if(users>=UINT_MAX-1) return false;
    } while(!atomic_compare_exchange_weak(&gate->users,&users,users+1));
    return true;
}

static inline void power_activity_end(power_gate_t *gate)
{
    unsigned previous=atomic_fetch_sub(&gate->users,1);
    assert(previous>0 && previous!=UINT_MAX);
    (void)previous;
}

static inline bool power_claim(power_gate_t *gate)
{
    unsigned idle=0;
    return atomic_compare_exchange_strong(&gate->users,&idle,UINT_MAX);
}

static inline void power_release(power_gate_t *gate)
{
    unsigned expected=UINT_MAX;
    bool released=atomic_compare_exchange_strong(&gate->users,&expected,0);
    assert(released);
    (void)released;
}