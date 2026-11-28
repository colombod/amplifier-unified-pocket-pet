#include <assert.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include "power_axp2101.h"
#include "live_voice.h"

/* Substitutes only the I2C transaction and LVGL label storage. check.sh
 * includes the production reader and label-update bodies without replacement. */
typedef int *i2c_master_dev_handle_t;
static _Atomic(i2c_master_dev_handle_t) source_device;
static int controller, transactions, bus_result;
static uint8_t registers[2];
enum { ESP_OK=0 };
static int i2c_master_transmit_receive(i2c_master_dev_handle_t dev,const uint8_t *reg,
    size_t write_size,uint8_t *data,size_t read_size,int timeout_ms)
{
    assert(dev==&controller && write_size==1 && *reg==0x00 && read_size==2);
    assert(timeout_ms>0 && timeout_ms<=20);
    transactions++;
    memcpy(data,registers,2);
    return bus_result;
}

typedef struct { const char *text; unsigned updates; } lv_obj_t;
static const char *lv_label_get_text(lv_obj_t *obj) {return obj->text;}
static void lv_label_set_text(lv_obj_t *obj,const char *text)
{
    obj->updates++;
    obj->text=text;
}

typedef struct { bool joining; } link_fixture_t;
static unsigned frames[73],tick_count,wave_ticks,image_updates;
static const unsigned *pet=&frames[0];
static const unsigned *lv_image_get_src(const unsigned *obj) {return obj;}
static void lv_image_set_src(const unsigned *obj,const unsigned *frame)
{
    assert(obj==pet);
    pet=frame;
    image_updates++;
}

#include "firmware-policy.inc"

_Static_assert(BV_MIC==244800 && BV_CHUNK==960 && BV_APPEND==4800,"capture assumptions");
_Static_assert(BV_PREBUFFER==15360 && BV_CATCHUP==24000,"playback assumptions");
_Static_assert(BV_AUDIO_CORE==1 && VOICE_OWNER_CORE==1,"core placement");
_Static_assert(BV_AUDIO_PRIORITY==7 && VOICE_OWNER_PRIORITY==6,"audio scheduling");

int main(void)
{
    /* Begin immediately after frame zero was first presented. */
    tick_count=1;
    for(unsigned tick=1;tick<=180;tick++){
        frame_tick((link_fixture_t){0},(live_status_t){.state=LIVE_OFF});
        assert(pet==&frames[(tick/3)%6]);
    }
    assert(image_updates==60);
    for(unsigned tick=181;tick<=192;tick++){
        frame_tick((link_fixture_t){.joining=true},(live_status_t){0});
        assert(pet==&frames[45+(tick/2)%6]);
    }
    wave_ticks=24;
    for(unsigned tick=0;tick<24;tick++){
        frame_tick((link_fixture_t){0},(live_status_t){.state=LIVE_RECONNECTING});
        assert(pet==&frames[22+tick/6]);
    }

    lv_obj_t label={.text="Mic off"};
    update_label(&label,"Connecting");
    char equal_text[]="Connecting";
    update_label(&label,equal_text);
    assert(label.updates==1 && !strcmp(label.text,"Connecting"));
    update_label(&label,"Listening");
    assert(label.updates==2 && !strcmp(label.text,"Listening"));
    label.text=NULL; /* previous allocation failed */
    update_label(&label,"Listening");
    assert(label.updates==3 && !strcmp(label.text,"Listening"));

    assert(device_power_read_source()==POWER_SOURCE_UNKNOWN && transactions==0);
    atomic_store(&source_device,&controller);
    for(unsigned status1=0;status1<256;status1++){
        for(unsigned status2=0;status2<256;status2++){
            registers[0]=status1;
            registers[1]=status2;
            putchar('0'+device_power_read_source());
        }
        putchar('\n');
    }
    assert(transactions==65536);
    registers[0]=0x20;
    registers[1]=0;
    assert(device_power_read_source()==POWER_SOURCE_USB);
    bus_result=-1; /* driver may leave valid-looking bytes in the buffer */
    assert(device_power_read_source()==POWER_SOURCE_UNKNOWN);
}
