#pragma once
#include <stdbool.h>

typedef enum { POWER_NONE, POWER_RESTART, POWER_OFF } power_action_t;
typedef enum { POWER_PENDING, POWER_BOOT_HELD, POWER_BOOT_UNAVAILABLE, POWER_UNAVAILABLE, POWER_OFF_UNCONFIRMED } power_result_t;

/* LVGL owns modal state; app_main owns GPIO/I2C and executes confirmed actions. */
void device_power_run(void);
power_action_t device_power_take_key(void);
void device_power_block_keys(bool block);
bool device_power_can_shutdown(void);
/* Precondition: both activity gates are exclusively held, and no other confirmed
 * action is outstanding. The UI retains both gates until a failure is reported. */
void device_power_confirmed(power_action_t action);
power_result_t device_power_result(void);