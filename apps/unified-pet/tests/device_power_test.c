#include <assert.h>
#include <stdio.h>
#include "power_axp2101.h"
#include "power_gate.h"
#include "power_input.h"

static void test_boot(void)
{
    boot_key_t key;
    boot_key_init(&key,true,0);
    assert(!boot_key_sample(&key,true,2000));
    assert(!boot_key_sample(&key,false,2010));
    assert(!boot_key_sample(&key,false,2050)); /* releasing the boot strap */
    assert(!boot_key_sample(&key,true,2100));
    assert(!boot_key_sample(&key,false,2120)); /* bounce, not a press */
    assert(!boot_key_sample(&key,true,2200));
    assert(!boot_key_sample(&key,true,2240));
    assert(!boot_key_sample(&key,true,2400));
    assert(!boot_key_sample(&key,false,2420));
    assert(!boot_key_sample(&key,true,2440)); /* release bounce */
    assert(!boot_key_sample(&key,false,2460));
    assert(boot_key_sample(&key,false,2500));
    assert(!boot_key_sample(&key,false,2600));

    boot_key_init(&key,false,UINT32_MAX-100);
    assert(!boot_key_sample(&key,false,UINT32_MAX-60));
    assert(!boot_key_sample(&key,true,UINT32_MAX-20));
    assert(!boot_key_sample(&key,true,20)); /* wraps during debounce */
    assert(!boot_key_sample(&key,false,30));
    assert(boot_key_sample(&key,false,70));
}

static void test_pwr(void)
{
    pwr_key_t key={.initialized_at=10};
    assert(!pwr_key_sample(&key,true,true,90)); /* initial latched press */
    assert(!pwr_key_sample(&key,true,false,170));
    assert(!pwr_key_sample(&key,true,true,500)); /* still in startup window */
    assert(!pwr_key_sample(&key,true,false,2800));
    assert(pwr_key_sample(&key,true,true,3200));
    assert(!pwr_key_sample(&key,true,true,3280)); /* repeated status */
    assert(!pwr_key_sample(&key,true,false,3400));
    assert(!pwr_key_sample(&key,true,true,3500)); /* bounce inside quiet window */
    assert(!pwr_key_sample(&key,true,false,4000));
    assert(!pwr_key_sample(&key,false,false,4300)); /* read/ACK failed */
    assert(!pwr_key_sample(&key,true,true,4380)); /* discard accumulated status */
    assert(!pwr_key_sample(&key,true,false,4460));
    assert(pwr_key_sample(&key,true,true,4800));
}

static void test_confirmation(void)
{
    power_touch_t touch=POWER_TOUCH_WAIT;
    power_touch_sample(&touch,false);   /* stale DOWN when modal opens */
    power_touch_press(&touch);
    power_touch_sample(&touch,true);
    assert(!power_touch_accept(&touch)); /* its matching click cannot confirm */
    power_touch_sample(&touch,true);
    assert(!power_touch_accept(&touch)); /* no press */
    power_touch_sample(&touch,true);
    power_touch_sample(&touch,false);
    power_touch_press(&touch);
    power_touch_sample(&touch,true);
    assert(power_touch_accept(&touch));
    assert(!power_touch_accept(&touch)); /* never twice */
}

typedef struct {
    uint8_t registers[0x100],last_reg,last_value;
    int fail_read,fail_write;
    unsigned writes;
} pmic_fixture_t;

static bool fake_read(void *ctx,uint8_t reg,uint8_t *value)
{
    pmic_fixture_t *fake=ctx;
    if(reg==fake->fail_read)return false;
    *value=fake->registers[reg];
    return true;
}
static bool fake_write(void *ctx,uint8_t reg,uint8_t value)
{
    pmic_fixture_t *fake=ctx;
    fake->writes++;
    fake->last_reg=reg;
    fake->last_value=value;
    if(reg==fake->fail_write)return false;
    if(reg==0x49)fake->registers[reg]&=(uint8_t)~value;
    else fake->registers[reg]=value;
    return true;
}

static void test_pmic(void)
{
    pmic_fixture_t fake={.fail_read=-1,.fail_write=-1};
    power_axp2101_t pmic={.read=fake_read,.write=fake_write,.ctx=&fake};
    assert(!axp2101_enable_short_key(&pmic));
    assert(axp2101_shutdown(&pmic)==AXP_OFF_UNAVAILABLE && fake.writes==0);
    fake.registers[0x03]=0x4a;
    fake.registers[0x41]=0x85;
    assert(axp2101_enable_short_key(&pmic));
    assert(fake.last_reg==0x41 && fake.last_value==0x8d);
    assert(axp2101_enable_short_key(&pmic) && fake.writes==1);

    fake.registers[0x49]=0x9f;
    bool pressed=false;
    fake.fail_write=0x49;
    assert(!axp2101_take_short_key(&pmic,&pressed) && !pressed);
    assert(fake.registers[0x49]==0x9f);
    fake.fail_write=-1;
    assert(axp2101_take_short_key(&pmic,&pressed) && pressed);
    assert(fake.last_reg==0x49 && fake.last_value==0x08 && fake.registers[0x49]==0x97);
    unsigned writes=fake.writes;
    assert(axp2101_take_short_key(&pmic,&pressed) && !pressed && fake.writes==writes);

    fake.fail_read=0x03;
    assert(axp2101_shutdown(&pmic)==AXP_OFF_UNAVAILABLE && fake.writes==writes);
    fake.fail_read=0x10;
    assert(axp2101_shutdown(&pmic)==AXP_OFF_UNAVAILABLE && fake.writes==writes);
    fake.fail_read=-1;
    fake.registers[0x10]=0x0e; /* stale reset command must not be echoed */
    assert(axp2101_shutdown(&pmic)==AXP_OFF_UNAVAILABLE && fake.writes==writes);
    fake.registers[0x10]=0x0c;
    fake.fail_write=0x10;
    assert(axp2101_shutdown(&pmic)==AXP_OFF_UNCERTAIN && fake.writes==++writes);
    fake.fail_write=-1;
    assert(axp2101_shutdown(&pmic)==AXP_OFF_SENT && fake.writes==++writes);
    assert(fake.last_reg==0x10 && fake.last_value==0x0d);
    assert(axp2101_shutdown(&pmic)==AXP_OFF_UNAVAILABLE && fake.writes==writes);
}

static void test_gate(void)
{
    power_gate_t gate={0};
    /* Request displayed while idle; HTTP save starts before confirmation. */
    assert(power_activity_begin(&gate));
    assert(!power_claim(&gate));
    assert(power_activity_begin(&gate)); /* queued command has its own lease */
    power_activity_end(&gate);          /* HTTP request replied */
    assert(!power_claim(&gate));         /* dequeued, commit still running */
    power_activity_end(&gate);
    assert(power_claim(&gate));
    assert(!power_claim(&gate));
    assert(!power_activity_begin(&gate));
    power_release(&gate);                /* command failed; resume normal use */
    assert(power_activity_begin(&gate));
    power_activity_end(&gate);
}

int main(void)
{
    test_boot();
    test_pwr();
    test_confirmation();
    test_pmic();
    test_gate();
    puts("device power: GPIO debounce, PMIC W1C/failures, and persistence gate PASS");
}