#include "device_power.h"
#include "power_axp2101.h"
#include "power_input.h"
#include <stdatomic.h>
#include "bsp/esp-bsp.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* Waveshare board docs identify BOOT; the official board schematic (unversioned)
 * connects Key1 to GPIO0/GND with a physical pull-up. Runtime input only: don't
 * set an output/pull, change the bootloader, or take over the strap at reset. */
#define BOOT_PIN GPIO_NUM_0
enum { KEY_POLL_MS=20, PMIC_POLL_MS=80, PMIC_BUS_MS=20, OFF_GRACE_MS=1000 };
/* Low two bits: empty, one request, or blocked. The upper bits tag the
 * modal generation so an in-flight I2C sample cannot reopen a closed modal. */
enum { KEY_BLOCKED=3, KEY_MASK=3 };
static atomic_uint key_slot;
static atomic_int confirmed_action, result;
static atomic_bool shutdown_supported;

static bool read_reg(void *ctx,uint8_t reg,uint8_t *value)
{
    return i2c_master_transmit_receive(ctx,&reg,1,value,1,PMIC_BUS_MS)==ESP_OK;
}

static bool write_reg(void *ctx,uint8_t reg,uint8_t value)
{
    uint8_t data[]={reg,value};
    return i2c_master_transmit(ctx,data,sizeof(data),PMIC_BUS_MS)==ESP_OK;
}

static bool pmic_init(power_axp2101_t *pmic)
{
    /* BSP already initialized this bus for touch. Never create a second bus
     * or assume the unversioned schematic's IRQ routing is present on V2. */
    i2c_master_bus_handle_t bus=bsp_i2c_get_handle();
    i2c_device_config_t cfg={
        .dev_addr_length=I2C_ADDR_BIT_LEN_7,.device_address=AXP2101_ADDRESS,
        .scl_speed_hz=100000,
    };
    i2c_master_dev_handle_t dev;
    if(!bus || i2c_master_bus_add_device(bus,&cfg,&dev)!=ESP_OK) return false;
    *pmic=(power_axp2101_t){.read=read_reg,.write=write_reg,.ctx=dev};
    if(axp2101_identified(pmic)) return true;
    i2c_master_bus_rm_device(dev);
    return false;
}

static uint32_t now_ms(void) {return (uint32_t)(esp_timer_get_time()/1000);}

static void offer_key(power_action_t action,unsigned generation)
{
    unsigned empty=generation;
    atomic_compare_exchange_strong(&key_slot,&empty,generation|action);
}

void device_power_block_keys(bool block)
{
    unsigned next=(atomic_load(&key_slot)&~(unsigned)KEY_MASK)+(KEY_MASK+1);
    atomic_store(&key_slot,next|(block ? KEY_BLOCKED : POWER_NONE));
}

power_action_t device_power_take_key(void)
{
    unsigned slot=atomic_load(&key_slot), action=slot&KEY_MASK;
    if(action==POWER_NONE || action==KEY_BLOCKED) return POWER_NONE;
    return atomic_compare_exchange_strong(&key_slot,&slot,slot&~(unsigned)KEY_MASK) ?
        (power_action_t)action : POWER_NONE;
}
bool device_power_can_shutdown(void) {return atomic_load(&shutdown_supported);}
void device_power_confirmed(power_action_t action)
{
    atomic_store(&result,POWER_PENDING);
    atomic_store(&confirmed_action,action);
}
power_result_t device_power_result(void) {return atomic_load(&result);}

void device_power_run(void)
{
    bool boot_input=gpio_input_enable(BOOT_PIN)==ESP_OK;
    boot_key_t boot;
    boot_key_init(&boot,!boot_input || !gpio_get_level(BOOT_PIN),now_ms());
    power_axp2101_t pmic;
    bool have_pmic=pmic_init(&pmic);
    atomic_store(&shutdown_supported,have_pmic);
    bool pwr_input=have_pmic && axp2101_enable_short_key(&pmic);
    if(!pwr_input) ESP_LOGW("pet_power","PWR runtime input unavailable");
    pwr_key_t pwr={.initialized_at=now_ms()};
    uint32_t last_pmic_poll=pwr.initialized_at;
    unsigned generation=0;
    for(;;) {
        power_action_t action=atomic_exchange(&confirmed_action,POWER_NONE);
        if(action==POWER_RESTART) {
            if(boot_input && gpio_get_level(BOOT_PIN)) esp_restart();
            atomic_store(&result,boot_input ? POWER_BOOT_HELD : POWER_BOOT_UNAVAILABLE);
        } else if(action==POWER_OFF) {
            axp_off_result_t off=have_pmic ? axp2101_shutdown(&pmic) : AXP_OFF_UNAVAILABLE;
            if(off!=AXP_OFF_UNAVAILABLE) {
                /* Keep local activity blocked while a command may take effect.
                 * Still executing after the grace period means off not proven. */
                vTaskDelay(pdMS_TO_TICKS(OFF_GRACE_MS));
            }
            uint32_t after=now_ms();
            boot_key_init(&boot,!boot_input || !gpio_get_level(BOOT_PIN),after);
            pwr=(pwr_key_t){.initialized_at=after};
            ESP_LOGW("pet_power","Shutdown returned without stopping firmware (%d)",(int)off);
            atomic_store(&result,off==AXP_OFF_UNAVAILABLE ? POWER_UNAVAILABLE : POWER_OFF_UNCONFIRMED);
        }
        uint32_t now=now_ms();
        unsigned current=atomic_load(&key_slot)&~(unsigned)KEY_MASK;
        if(current!=generation) {
            generation=current;
            boot_key_init(&boot,!boot_input || !gpio_get_level(BOOT_PIN),now);
            pwr=(pwr_key_t){.initialized_at=now};
        }
        if(boot_input && boot_key_sample(&boot,!gpio_get_level(BOOT_PIN),now))
            offer_key(POWER_RESTART,generation);
        if(pwr_input && now-last_pmic_poll>=PMIC_POLL_MS) {
            last_pmic_poll=now;
            bool pressed;
            bool ok=axp2101_take_short_key(&pmic,&pressed);
            if(pwr_key_sample(&pwr,ok,pressed,now)) offer_key(POWER_OFF,generation);
        }
        vTaskDelay(pdMS_TO_TICKS(KEY_POLL_MS)+1);
    }
}