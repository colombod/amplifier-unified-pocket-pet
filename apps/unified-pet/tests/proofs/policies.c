/* Run with CBMC on the app's actual headers. Peripheral and NVS I/O are
 * symbolic inputs. The any_* functions supply unconstrained inputs. */
#include <assert.h>
#include "network_retry.h"
#include "pet_store.h"
#include "power_input.h"
#include "power_gate.h"
#include "power_axp2101.h"
#include "live_protocol.h"

static uint32_t any_tick(void) { uint32_t value; __CPROVER_havoc_object(&value); return value; }
static uint8_t any_byte(void) { uint8_t value; __CPROVER_havoc_object(&value); return value; }
static int any_error(void) { int value; __CPROVER_havoc_object(&value); return value; }
static bool any_flag(void) { bool value; __CPROVER_havoc_object(&value); return value; }

static void wifi(void)
{
    uint32_t now=any_tick();
    network_retry_t r={.delay_ms=any_tick()};
    __CPROVER_assume(r.delay_ms==0 || (r.delay_ms>=5000 && r.delay_ms<=60000));
    network_retry_failed(&r,now);
    assert(r.scheduled && r.delay_ms>=5000 && r.delay_ms<=60000);
    assert(r.deadline-now==r.delay_ms);
    assert(!network_retry_due(&r,r.deadline-1,false,false,false,false,0));
    assert(network_retry_due(&r,r.deadline,false,false,false,false,0));

    bool connected=any_flag(), joining=any_flag(), queued=any_flag(), setup=any_flag();
    uint32_t at=any_tick(), sample=any_tick();
    bool due=network_retry_due(&r,sample,connected,joining,queued,setup,at);
    assert(!due || (!connected && !joining && !queued && (!setup || sample-at>=60000)));
}

/* Model one linearized gate operation; atomic scheduling and lease pairing
 * at all production call sites are outside this harness. */
static void power_claims(void)
{
    unsigned active=any_tick();
    power_gate_t gate;
    atomic_init(&gate.users,active);
    bool claimed=power_claim(&gate);
    assert(claimed==(active==0));
    if(claimed){
        assert(!power_activity_begin(&gate));
        assert(!power_claim(&gate));
        power_release(&gate);
        assert(atomic_load(&gate.users)==0);
    } else assert(atomic_load(&gate.users)==active);
}

static void buttons(void)
{
    uint32_t now=any_tick();
    bool down=any_flag();
    boot_key_t boot;
    boot_key_init(&boot,down,now);
    assert(!boot_key_sample(&boot,down,any_tick())); /* arbitrary initial hold */
    assert(!boot_key_sample(&boot,false,now+100));
    assert(!boot_key_sample(&boot,false,now+150));
    assert(!boot_key_sample(&boot,true,now+200));
    assert(!boot_key_sample(&boot,true,now+250));
    assert(!boot_key_sample(&boot,false,now+300));
    assert(boot_key_sample(&boot,false,now+350));
    assert(!boot_key_sample(&boot,false,now+400));

    bool ok=any_flag(), short_press=any_flag(), clean=any_flag();
    uint32_t quiet=any_tick(), start=any_tick();
    pwr_key_t pwr={.quiet_since=quiet,.initialized_at=start,.clean_seen=clean};
    bool offered=pwr_key_sample(&pwr,ok,short_press,now);
    assert(!offered || (ok && short_press && clean && now-quiet>=250 && now-start>=3000));
    power_touch_t touch=POWER_TOUCH_WAIT;
    power_touch_sample(&touch,false);
    power_touch_press(&touch);
    assert(!power_touch_accept(&touch));
    power_touch_sample(&touch,true);
    power_touch_press(&touch);
    assert(power_touch_accept(&touch));
    assert(!power_touch_accept(&touch));
}

/* A failed read may dirty both the buffer and its size. */
static int own_result,legacy_result;
static unsigned fallback_reads;
esp_err_t nvs_get_str(nvs_handle_t handle,const char *key,char *out,size_t *n)
{
    (void)key;
    assert(*n==4);
    if(handle==2) fallback_reads++;
    else assert(handle==1);
    memset(out,42,4);
    *n=1;
    return handle==1 ? own_result : legacy_result;
}

static void config(void)
{
    pet_store_t stores={.own=1,.legacy=2,.legacy_ready=any_flag()};
    own_result=any_error();
    legacy_result=any_error();
    size_t capacity=4;
    char buf[4];
    bool use_legacy=own_result==ESP_ERR_NVS_NOT_FOUND && stores.legacy_ready;
    int result=pet_store_get_str(&stores,"test",buf,&capacity);
    assert(fallback_reads==(unsigned)use_legacy);
    assert(result==(use_legacy ? legacy_result : own_result));
    if(result!=ESP_OK)
        for(unsigned i=0;i<sizeof(buf);i++) assert(buf[i]==0);
}

static void power_and_audio(void)
{
    uint8_t a=any_byte(), b=any_byte();
    power_source_t power=axp2101_source(a,b);
    if(power==POWER_SOURCE_BATTERY)
        assert((a & 0x28)==0x08 && (b & 0x60)==0x40);
    if((a & 0x28)==0x20 && (b & 0x60)==0)
        assert(power==POWER_SOURCE_USB);
    uint8_t pcm[]={a,b};
    assert((live_pcm_peak(pcm,sizeof(pcm))==0) == (a==0 && b==0));
}

int main(void)
{
    wifi();
    buttons();
    power_claims();
    config();
    power_and_audio();
}
