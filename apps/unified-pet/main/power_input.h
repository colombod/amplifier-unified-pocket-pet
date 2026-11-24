#pragma once
#include <stdbool.h>
#include <stdint.h>

enum { BOOT_DEBOUNCE_MS=35, PWR_QUIET_MS=250, PWR_STARTUP_MS=3000 };

typedef struct {
    bool raw_down, stable_down, armed, pressed;
    uint32_t changed_at;
} boot_key_t;

/* A released level must first be observed for the full debounce period.
 * A held strap at startup is NOT a runtime press. Emit only on release. */
static inline void boot_key_init(boot_key_t *key,bool down,uint32_t now)
{
    *key=(boot_key_t){.raw_down=down,.stable_down=true,.changed_at=now};
}

static inline bool boot_key_sample(boot_key_t *key,bool down,uint32_t now)
{
    if(down!=key->raw_down) {
        key->raw_down=down;
        key->changed_at=now;
    }
    if(down==key->stable_down || now-key->changed_at<BOOT_DEBOUNCE_MS) return false;
    key->stable_down=down;
    if(down) {
        key->pressed=key->armed;
        return false;
    }
    bool event=key->pressed;
    key->armed=true;
    key->pressed=false;
    return event;
}

typedef struct {
    uint32_t quiet_since, initialized_at;
    bool clean_seen;
} pwr_key_t;

/* The PMIC classifies short presses in hardware. Establish a quiet, clean
 * baseline after startup or a bus fault before accepting another event. */
static inline bool pwr_key_sample(pwr_key_t *key,bool ok,bool short_press,uint32_t now)
{
    if(!ok) {
        key->clean_seen=false;
        return false;
    }
    if(!short_press) {
        if(!key->clean_seen) {
            key->quiet_since=now;
            key->clean_seen=true;
        }
        return false;
    }
    bool event=key->clean_seen && now-key->quiet_since>=PWR_QUIET_MS &&
        now-key->initialized_at>=PWR_STARTUP_MS;
    key->clean_seen=false;
    return event;
}

/* Confirmation is a touch gesture, never the key that opened the modal.
 * Only a fresh released sample can arm it, followed by a new button press. */
typedef enum { POWER_TOUCH_WAIT, POWER_TOUCH_READY, POWER_TOUCH_PRESSED } power_touch_t;

static inline void power_touch_sample(power_touch_t *touch,bool released)
{
    if(*touch==POWER_TOUCH_WAIT && released) *touch=POWER_TOUCH_READY;
}

static inline void power_touch_press(power_touch_t *touch)
{
    if(*touch==POWER_TOUCH_READY) *touch=POWER_TOUCH_PRESSED;
}

static inline bool power_touch_accept(power_touch_t *touch)
{
    bool accepted=*touch==POWER_TOUCH_PRESSED;
    *touch=POWER_TOUCH_WAIT;
    return accepted;
}