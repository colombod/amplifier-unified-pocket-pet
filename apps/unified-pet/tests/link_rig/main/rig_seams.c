/* Seams that are hardware or another module on the device: the codec, the
 * parent voice worker's status/stop latch, the stored token and esp_timer
 * (which has no linux-target implementation in the pinned SDK).
 *
 * Microphone and speaker run in real time. The microphone produces a counter
 * pattern that is never zero, so the host can tell every real sample from the
 * digital silence the device substitutes while its speaker is audible, and can
 * distinguish forward gaps from repeated or reordered input. */
#include <stdatomic.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "audio_test.h"
#include "live_voice.h"
#include "live_protocol.h"
#include "rig.h"

#define RIG_TOKEN "rigTOKENabcdefghijklmnopqrstuvwxyz012345678" /* shape-valid, not a secret */

BaseType_t rig_create_pinned(TaskFunction_t fn,const char *name,uint32_t stack,void *arg,
                            UBaseType_t priority,TaskHandle_t *out,BaseType_t core){
    if(!strcmp(name,"pet_live"))assert(priority==6&&core==1&&stack==12288);
    else{
        assert(!strcmp(name,"pet_bridge_mic")||!strcmp(name,"pet_bridge_out"));
        assert(priority==7&&core==0&&stack==4096);
    }
    printf("RIG task name=%s priority=%u requested_core=%d (single simulated CPU)\n",
           name,(unsigned)priority,(int)core);
    return xTaskCreate(fn,name,stack,arg,priority,out);
}

long rig_env(const char *name,long fallback){
    const char *v=getenv(name);
    return v&&*v?strtol(v,NULL,10):fallback;
}
const char *rig_env_text(const char *name){const char *v=getenv(name);return v?v:"";}

/* ---- esp_timer ---- */
static int64_t boot_us;
static int64_t monotonic_us(void){
    struct timespec ts;clock_gettime(CLOCK_MONOTONIC,&ts);
    return (int64_t)ts.tv_sec*1000000+ts.tv_nsec/1000;
}
int64_t esp_timer_get_time(void){return monotonic_us()-boot_us;}
struct esp_timer {esp_timer_cb_t cb;void *arg;int64_t due;bool armed,used;};
static struct esp_timer timers[8];
static SemaphoreHandle_t timer_lock;
esp_err_t esp_timer_create(const esp_timer_create_args_t *args,esp_timer_handle_t *out){
    esp_err_t e=ESP_ERR_NO_MEM;
    xSemaphoreTake(timer_lock,portMAX_DELAY);
    for(unsigned i=0;i<8;i++)if(!timers[i].used){
        timers[i]=(struct esp_timer){.cb=args->callback,.arg=args->arg,.used=true};*out=&timers[i];e=ESP_OK;break;
    }
    xSemaphoreGive(timer_lock);return e;
}
esp_err_t esp_timer_start_once(esp_timer_handle_t t,uint64_t us){
    xSemaphoreTake(timer_lock,portMAX_DELAY);
    esp_err_t e=t->armed?ESP_ERR_INVALID_STATE:ESP_OK;
    if(e==ESP_OK){t->due=esp_timer_get_time()+(int64_t)us;t->armed=true;}
    xSemaphoreGive(timer_lock);return e;
}
esp_err_t esp_timer_stop(esp_timer_handle_t t){
    xSemaphoreTake(timer_lock,portMAX_DELAY);
    esp_err_t e=t->armed?ESP_OK:ESP_ERR_INVALID_STATE;t->armed=false;
    xSemaphoreGive(timer_lock);return e;
}
esp_err_t esp_timer_delete(esp_timer_handle_t t){
    xSemaphoreTake(timer_lock,portMAX_DELAY);
    esp_err_t e=t->armed?ESP_ERR_INVALID_STATE:ESP_OK;
    if(e==ESP_OK)t->used=false;
    xSemaphoreGive(timer_lock);return e;
}
static void timer_task(void *arg){
    (void)arg;
    for(;;){
        esp_timer_cb_t cb=NULL;void *cb_arg=NULL;
        xSemaphoreTake(timer_lock,portMAX_DELAY);
        for(unsigned i=0;i<8;i++)if(timers[i].used&&timers[i].armed&&timers[i].due<=esp_timer_get_time()){
            timers[i].armed=false;cb=timers[i].cb;cb_arg=timers[i].arg;break;
        }
        xSemaphoreGive(timer_lock);
        if(cb)cb(cb_arg);else vTaskDelay(1);
    }
}

/* ---- parent voice worker: status, stop latch, audio-gate admission ---- */
static atomic_bool parent_stop;
static SemaphoreHandle_t status_lock;
static atomic_llong listening_at; /* 0: not yet */
static char last_text[112];
static live_state_t last_state=(live_state_t)-1;
bool rig_parent_stopped(void){return atomic_load(&parent_stop);}
void rig_parent_stop(void){atomic_store(&parent_stop,true);}
bool rig_listening_seen(void){return atomic_load(&listening_at)!=0;}
int64_t rig_since_listening_ms(void){
    long long at=atomic_load(&listening_at);
    return at?(esp_timer_get_time()-at)/1000:0;
}
void live_voice_bridge_status(live_state_t state,const char *message){
    xSemaphoreTake(status_lock,portMAX_DELAY);
    bool changed=state!=last_state||strcmp(last_text,message);
    if(changed){last_state=state;snprintf(last_text,sizeof(last_text),"%s",message);}
    xSemaphoreGive(status_lock);
    if(!changed)return;
    if(state==LIVE_LISTENING){long long none=0;atomic_compare_exchange_strong(&listening_at,&none,esp_timer_get_time());}
    printf("RIG status t=%lld since_listening_ms=%lld state=%d text=%s\n",
        (long long)(esp_timer_get_time()/1000),(long long)rig_since_listening_ms(),(int)state,message);
}
bool live_voice_bridge_audio_begin(void){
    xSemaphoreTake(status_lock,portMAX_DELAY);
    if(atomic_load(&parent_stop)){xSemaphoreGive(status_lock);return false;}
    return true;
}
void live_voice_bridge_audio_end(void){xSemaphoreGive(status_lock);}

esp_err_t network_copy_bridge_token(char *out,size_t capacity){
    if(!out||capacity<sizeof(RIG_TOKEN))return ESP_ERR_INVALID_SIZE;
    memcpy(out,RIG_TOKEN,sizeof(RIG_TOKEN));return ESP_OK;
}

/* ---- codec ---- */
struct audio_session {int64_t mic_next,speaker_next;};
static struct audio_session codec;
static atomic_bool amp_off,codec_open;
static atomic_ullong mic_bytes,speaker_bytes,speaker_audible_bytes;
static uint64_t mic_index;
void audio_stream_prepare(void){atomic_store(&amp_off,false);}
void audio_stream_inhibit(void){atomic_store(&amp_off,true);}
esp_err_t audio_stream_open(audio_session_t **out){
    if(atomic_load(&amp_off)||atomic_exchange(&codec_open,true))return ESP_ERR_INVALID_STATE;
    codec.mic_next=codec.speaker_next=esp_timer_get_time();*out=&codec;return ESP_OK;
}
static void until(int64_t at){
    for(;;){
        int64_t left=at-esp_timer_get_time();
        if(left<=0)return;
        vTaskDelay(left>=1000?pdMS_TO_TICKS(left/1000):1);
    }
}
esp_err_t audio_stream_read(audio_session_t *s,void *pcm,unsigned bytes){
    /* I2S delivers at the sample rate; a late reader finds the next samples waiting. */
    s->mic_next+=(int64_t)bytes*1000000/48000;
    until(s->mic_next);
    uint8_t *b=pcm;
    /* Two nonzero 16-bit digits encode an absolute sample-pair index. Unlike
     * the former 8192-sample cycle, a multi-second drop cannot alias replay.
     * Whole 960-byte drops and silence substitution preserve pair alignment. */
    for(unsigned i=0;i+3<bytes;i+=4){
        uint64_t pair=mic_index/2;mic_index+=2;
        unsigned low=1+(unsigned)(pair%65535),high=1+(unsigned)(pair/65535);
        b[i]=(uint8_t)low;b[i+1]=(uint8_t)(low>>8);
        b[i+2]=(uint8_t)high;b[i+3]=(uint8_t)(high>>8);
    }
    atomic_fetch_add(&mic_bytes,bytes);
    return ESP_OK;
}
esp_err_t audio_stream_write(audio_session_t *s,void *pcm,unsigned bytes){
    int64_t now=esp_timer_get_time();
    if(s->speaker_next<now)s->speaker_next=now; /* an underrun does not bank time */
    s->speaker_next+=(int64_t)bytes*1000000/48000;
    atomic_fetch_add(&speaker_bytes,bytes);
    if(!atomic_load(&amp_off)&&live_pcm_audible(pcm,bytes))atomic_fetch_add(&speaker_audible_bytes,bytes);
    until(s->speaker_next);
    return ESP_OK;
}
void audio_stream_close(audio_session_t *s){(void)s;atomic_store(&codec_open,false);}

void rig_report(void){
    printf("RIG codec mic_bytes=%llu speaker_bytes=%llu speaker_audible_bytes=%llu\n",
        (unsigned long long)atomic_load(&mic_bytes),(unsigned long long)atomic_load(&speaker_bytes),
        (unsigned long long)atomic_load(&speaker_audible_bytes));
    printf("RIG final state=%d text=%s\n",(int)last_state,last_text);
}
void rig_seams_init(void){
    boot_us=monotonic_us();
    timer_lock=xSemaphoreCreateMutex();status_lock=xSemaphoreCreateMutex();
    xTaskCreate(timer_task,"rig_timer",4096,NULL,22,NULL); /* esp_timer task priority */
}
