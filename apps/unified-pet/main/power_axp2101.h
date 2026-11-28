#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "device_power.h"

/* Waveshare ESP32-S3-Touch-AMOLED-1.8 @ 01fd3200370a4417b257e8a4c185a04ef931342a:
 * examples/arduino-v2/libraries/Mylibrary/pin_config.h selects AXP2101.
 * See examples/arduino/libraries/XPowersLib/src/{REG/AXP2101Constants.h,
 * XPowersParams.hpp,XPowersAXP2101.tpp} and AXP2101 SWcharge V1.0 pp. 26, 34–36.
 * No voltage, charging, wake, long-press timing or recovery configuration. */
enum {
    AXP2101_ADDRESS=0x34, AXP2101_ID_REG=0x03, AXP2101_ID=0x4a,
    AXP2101_STATUS1=0x00, AXP2101_VBUS_GOOD=0x20, AXP2101_BAT_PRESENT=0x08,
    AXP2101_CURRENT_MASK=0x60,
    AXP2101_STANDBY=0x00, AXP2101_CHARGING=0x20, AXP2101_DISCHARGING=0x40,
    AXP2101_COMMON=0x10, AXP2101_OFF=0x01, AXP2101_RESET=0x02,
    AXP2101_IRQ2_ENABLE=0x41, AXP2101_IRQ2_STATUS=0x49, AXP2101_KEY_SHORT=0x08,
};

/* STATUS1 and STATUS2 are adjacent. A battery can remain connected while
 * USB supplies the device. During a transition, conflicting flags cannot
 * establish a single source. REG01[3] is input voltage regulation status
 * in the X-Powers datasheet, so it cannot establish battery power. */
static inline power_source_t axp2101_source(uint8_t status1,uint8_t status2)
{
    bool good=(status1 & AXP2101_VBUS_GOOD)!=0;
    bool battery=(status1 & AXP2101_BAT_PRESENT)!=0;
    uint8_t current=status2 & AXP2101_CURRENT_MASK;
    if(good && (current==AXP2101_STANDBY || (battery && current==AXP2101_CHARGING)))
        return POWER_SOURCE_USB;
    if(!good && battery && current==AXP2101_DISCHARGING)
        return POWER_SOURCE_BATTERY;
    return POWER_SOURCE_UNKNOWN;
}

typedef struct {
    bool (*read)(void *ctx,uint8_t reg,uint8_t *value);
    bool (*write)(void *ctx,uint8_t reg,uint8_t value);
    void *ctx;
} power_axp2101_t;

static inline bool axp2101_identified(const power_axp2101_t *pmic)
{
    uint8_t id;
    return pmic->read(pmic->ctx,AXP2101_ID_REG,&id) && id==AXP2101_ID;
}

static inline bool axp2101_enable_short_key(const power_axp2101_t *pmic)
{
    uint8_t enabled;
    return axp2101_identified(pmic) &&
        pmic->read(pmic->ctx,AXP2101_IRQ2_ENABLE,&enabled) &&
        ((enabled&AXP2101_KEY_SHORT) ||
         pmic->write(pmic->ctx,AXP2101_IRQ2_ENABLE,enabled|AXP2101_KEY_SHORT));
}

static inline bool axp2101_take_short_key(const power_axp2101_t *pmic,bool *pressed)
{
    uint8_t status;
    *pressed=false;
    if(!pmic->read(pmic->ctx,AXP2101_IRQ2_STATUS,&status)) return false;
    if(!(status&AXP2101_KEY_SHORT)) return true;
    /* RW1C: only acknowledge the short press. Never echo unrelated status. */
    if(!pmic->write(pmic->ctx,AXP2101_IRQ2_STATUS,AXP2101_KEY_SHORT)) return false;
    *pressed=true;
    return true;
}

typedef enum { AXP_OFF_UNAVAILABLE, AXP_OFF_UNCERTAIN, AXP_OFF_SENT } axp_off_result_t;

static inline axp_off_result_t axp2101_shutdown(const power_axp2101_t *pmic)
{
    uint8_t config;
    if(!axp2101_identified(pmic) ||
       !pmic->read(pmic->ctx,AXP2101_COMMON,&config) ||
       (config&(AXP2101_RESET|AXP2101_OFF))) return AXP_OFF_UNAVAILABLE;
    /* ACK proves a bus transaction, not that the board powered off. A failed
     * write may also have reached the device. Never blindly retry either. */
    return pmic->write(pmic->ctx,AXP2101_COMMON,config|AXP2101_OFF) ?
        AXP_OFF_SENT : AXP_OFF_UNCERTAIN;
}