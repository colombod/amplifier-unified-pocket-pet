/* Conversation mode, whole owner. run_bridge_converse.sh includes the complete
 * production bridge_voice.c body verbatim: the owner loop, BOTH audio tasks,
 * cleanup and reuse run exactly as built, with the real parser/model linked.
 *
 * Synthetic and therefore NOT proven here: RTOS scheduling, the clock, the
 * codec/amplifier, TLS/WebSocket I/O, Wi-Fi, the host and Unified. Tasks are
 * real threads, but exactly one runs at a time and they switch only where the
 * firmware would block, on one virtual clock: deterministic interleavings, not
 * a race or timing qualification of the device.
 *
 * The host is a script: golden frames are the exact bytes of the binding
 * design (conversation-mode.md). Device output is checked at the wire seam. */
#define _GNU_SOURCE
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "bridge_protocol.h"
#include "live_protocol.h"
#include "live_voice.h"
#include "assistant_config.h"
#include "mbedtls/base64.h"

#define G_BEGIN "{\"type\":\"pet.converse.begin\"}"
#define G_BEGAN "{\"type\":\"pet.converse.began\",\"safe_session_label\":\"Example conversation\",\"session_id_short\":\"a1b2c3d4\"}"
#define G_STATE(x) "{\"type\":\"pet.converse.state\",\"state\":\"" x "\"}"
#define G_NOTICE(x) "{\"type\":\"pet.converse.notice\",\"code\":\"" x "\"}"
#define STARTED "{\"type\":\"session.started\"}"
#define CLOSED "{\"type\":\"session.closed\"}"
#define PET_STOP "{\"type\":\"pet.stop\"}"
#define SESSION_CLOSE "{\"type\":\"session.close\"}"
#define APPEND "{\"type\":\"session.input_audio.append\",\"audio\":\""
#define TURN "123e4567-e89b-42d3-a456-426614174000"
/* Shape-valid fake only; it must never reach a frame, a status or a log line. */
#define TOKEN "petSECRETtokenABCDEFGHIJKLMNOPQRSTUVWXYZ012"
#define SESSION_US 660000000LL /* live_voice.c BRIDGE_SESSION_LIMIT_MS from the accepted tap */

typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL (-1)
#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#define pdMS_TO_TICKS(x) (x)
#define BIT0 1U
#define BIT1 2U
#define BIT2 4U
#define BIT3 8U
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
typedef unsigned EventBits_t;
typedef struct {unsigned bits;} *EventGroupHandle_t;
typedef struct stream {unsigned char *data;size_t cap,n,high;} *StreamBufferHandle_t;
typedef struct {unsigned unused;} StaticStreamBuffer_t;
typedef struct queue {char *items[64];unsigned n,cap;} *QueueHandle_t;
typedef void *SemaphoreHandle_t;
typedef void *esp_websocket_client_handle_t;
typedef struct audio_session {unsigned unused;} audio_session_t;
typedef struct bridge_transport {bool started,upgraded,cancelled,finished;int64_t deadline;} bridge_transport_t;
typedef struct {int unused;} setup_bridge_t;
typedef const char *esp_event_base_t;
typedef struct {int data_len,payload_len,payload_offset,op_code;bool fin;const char *data_ptr;} esp_websocket_event_data_t;
enum {WEBSOCKET_EVENT_ANY=-1,WEBSOCKET_EVENT_ERROR,WEBSOCKET_EVENT_CONNECTED,WEBSOCKET_EVENT_DISCONNECTED,
      WEBSOCKET_EVENT_DATA,WEBSOCKET_EVENT_CLOSED};
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(m) ((void)(m))
#define portEXIT_CRITICAL(m) ((void)(m))

/* ---- one-at-a-time scheduler on a virtual clock ---- */
#define MAX_ENTITIES 256
typedef struct {pthread_t thread;bool live,joined;int64_t wake;void (*fn)(void*);void *arg;} entity_t;
static entity_t entities[MAX_ENTITIES];
static unsigned entity_n=1; /* 0 is the owner: the thread that calls bridge_voice_run */
static int running;
static __thread int self;
static pthread_mutex_t baton=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t turn=PTHREAD_COND_INITIALIZER;
static int64_t now=5000000;
static bool guard_held;

typedef enum {EV_CONNECT,EV_TEXT,EV_WS_CLOSED,EV_WS_DROP,EV_WS_ERROR,EV_MIC_OFF,EV_PARENT_STOP,
              EV_REFUSED,EV_CHECK} ev_kind;
typedef struct {int64_t at;ev_kind kind;const char *text;void (*check)(void);bool fired;} event_t;
static event_t script[512];
static unsigned script_n;
static void fire(event_t *e);
static void host_tick(void);
static int64_t host_next; /* 0: no host audio in this run */

static event_t *next_event(void){
    event_t *best=NULL;
    for(unsigned i=0;i<script_n;i++)if(!script[i].fired&&(!best||script[i].at<best->at))best=&script[i];
    return best;
}
static int schedule(void){
    for(;;){
        int best=-1;
        for(unsigned i=0;i<entity_n;i++)
            if(entities[i].live&&(best<0||entities[i].wake<entities[best].wake))best=(int)i;
        assert(best>=0);
        int64_t limit=entities[best].wake;
        event_t *e=next_event();
        if(e&&e->at<=limit&&(!host_next||e->at<=host_next)){if(e->at>now)now=e->at;e->fired=true;fire(e);continue;}
        if(host_next&&host_next<=limit){if(host_next>now)now=host_next;host_tick();continue;}
        if(limit>now)now=limit;
        return best;
    }
}
/* Every place the firmware would block comes through here. Production never
 * holds its model guard across one (asserted), as on the device. */
static void yield_until(int64_t t){
    assert(!guard_held);
    if(t<now)t=now;
    entities[self].wake=t;
    int next=schedule();
    if(next==self)return;
    pthread_mutex_lock(&baton);running=next;pthread_cond_broadcast(&turn);
    while(running!=self)pthread_cond_wait(&turn,&baton);
    pthread_mutex_unlock(&baton);
}
static unsigned tasks_created,tasks_exited,task_fail_at,task_attempts;
static void *task_main(void *p){
    int id=(int)(intptr_t)p;
    pthread_mutex_lock(&baton);while(running!=id)pthread_cond_wait(&turn,&baton);pthread_mutex_unlock(&baton);
    self=id;entities[id].fn(entities[id].arg);
    assert(!"task returned without vTaskDelete");return NULL;
}
static int xTaskCreate(void (*fn)(void*),const char *name,unsigned stack,void *arg,unsigned prio,void *handle){
    (void)name;(void)stack;(void)prio;(void)handle;
    if(task_fail_at&&++task_attempts==task_fail_at)return 0;
    assert(entity_n<MAX_ENTITIES);int id=(int)entity_n++;
    entities[id]=(entity_t){.live=true,.wake=now,.fn=fn,.arg=arg};tasks_created++;
    assert(!pthread_create(&entities[id].thread,NULL,task_main,(void*)(intptr_t)id));
    return pdPASS;
}
static void vTaskDelete(void *p){
    assert(!p&&self!=0&&!guard_held);entities[self].live=false;tasks_exited++;
    int next=schedule();
    pthread_mutex_lock(&baton);running=next;pthread_cond_broadcast(&turn);pthread_mutex_unlock(&baton);
    pthread_exit(NULL);
}
static void vTaskDelay(unsigned ticks){yield_until(now+(int64_t)(ticks?ticks:1)*1000);}
static int64_t esp_timer_get_time(void){return now;}

/* ---- RTOS objects ---- */
static unsigned queues_made,queues_deleted,streams_made,streams_deleted,groups_made,groups_deleted;
static SemaphoreHandle_t xSemaphoreCreateMutex(void){return (void*)1;}
static int xSemaphoreTake(SemaphoreHandle_t h,unsigned ticks){(void)h;(void)ticks;assert(!guard_held);guard_held=true;return pdTRUE;}
static void xSemaphoreGive(SemaphoreHandle_t h){(void)h;assert(guard_held);guard_held=false;}
static QueueHandle_t xQueueCreate(unsigned n,size_t size){
    assert(size==sizeof(char*)&&n<=64);QueueHandle_t q=calloc(1,sizeof(*q));assert(q);q->cap=n;queues_made++;return q;
}
/* A loop that is routinely late, as on the device (display task at the same
 * priority, TLS work, base64): every owner iteration loses a further
 * late_min..late_max of time, drawn from a fixed sequence. owner_stall_* is
 * one single longer stop of the owner. Neither touches production code. */
static int64_t late_min_us,late_max_us,owner_stall_at,owner_stall_us;
static uint32_t late_seed;
static unsigned frames_high;
static int xQueueSend(QueueHandle_t q,const void *p,unsigned ticks){
    assert(!ticks);if(q->n==q->cap)return pdFALSE;q->items[q->n++]=*(char *const*)p;
    if(q->n>frames_high)frames_high=q->n;
    return pdTRUE;
}
static int xQueueReceive(QueueHandle_t q,void *out,unsigned ticks){
    if(!q->n&&ticks)yield_until(now+(int64_t)ticks*1000);
    if(ticks&&self==0){
        if(late_max_us){
            late_seed=late_seed*1664525u+1013904223u;
            yield_until(now+late_min_us+(int64_t)((late_seed>>8)%(uint32_t)(late_max_us-late_min_us+1)));
        }
        if(owner_stall_at&&now>=owner_stall_at){owner_stall_at=0;yield_until(now+owner_stall_us);}
    }
    if(!q->n)return pdFALSE;
    *(char**)out=q->items[0];memmove(q->items,q->items+1,--q->n*sizeof(*q->items));return pdTRUE;
}
static unsigned uxQueueMessagesWaiting(QueueHandle_t q){return q->n;}
static void vQueueDelete(QueueHandle_t q){assert(!guard_held&&!q->n);queues_deleted++;free(q);}
static StreamBufferHandle_t xStreamBufferCreateStatic(size_t size,size_t trigger,unsigned char *memory,StaticStreamBuffer_t *meta){
    (void)trigger;(void)memory;(void)meta;StreamBufferHandle_t s=calloc(1,sizeof(*s));assert(s);
    s->cap=size-1;s->data=malloc(s->cap);assert(s->data);streams_made++;return s;
}
static size_t xStreamBufferBytesAvailable(StreamBufferHandle_t s){return s->n;}
static size_t mic_high,mic_waiting; /* microphone audio captured but not yet sent */
static size_t xStreamBufferSend(StreamBufferHandle_t s,const void *p,size_t n,unsigned ticks){
    assert(!ticks);if(n>s->cap-s->n)n=s->cap-s->n; /* zero-wait partial write, as the RTOS */
    memcpy(s->data+s->n,p,n);s->n+=n;if(s->n>s->high)s->high=s->n;
    if(s->cap==96000){mic_waiting=s->n;if(s->n>mic_high)mic_high=s->n;}
    return n;
}
static size_t xStreamBufferReceive(StreamBufferHandle_t s,void *p,size_t n,unsigned ticks){
    assert(!ticks);if(n>s->n)n=s->n;memcpy(p,s->data,n);memmove(s->data,s->data+n,s->n-n);s->n-=n;
    if(s->cap==96000)mic_waiting=s->n;
    return n;
}
static void vStreamBufferDelete(StreamBufferHandle_t s){assert(!guard_held);streams_deleted++;free(s->data);free(s);}
static EventGroupHandle_t xEventGroupCreate(void){groups_made++;return calloc(1,sizeof(struct {unsigned bits;}));}
static unsigned xEventGroupSetBits(EventGroupHandle_t e,unsigned bits){e->bits|=bits;return e->bits;}
static unsigned xEventGroupClearBits(EventGroupHandle_t e,unsigned bits){e->bits&=~bits;return e->bits;}
static unsigned xEventGroupGetBits(EventGroupHandle_t e){return e->bits;}
static unsigned xEventGroupWaitBits(EventGroupHandle_t e,unsigned bits,int clear,int all,unsigned ticks){
    (void)clear;(void)all;if((e->bits&bits)!=bits)yield_until(now+(int64_t)ticks*1000);return e->bits;
}
static void vEventGroupDelete(EventGroupHandle_t e){assert(!guard_held);groups_deleted++;free(e);}
static void *heap_caps_malloc(size_t n,unsigned flags){(void)flags;return malloc(n);}
static void *heap_caps_calloc(size_t n,size_t size,unsigned flags){(void)flags;return calloc(n,size);}
static void setup_bridge_wipe(void *p,size_t n){volatile unsigned char *b=p;while(n--)*b++=0;}

/* ---- what the user sees ---- */
static struct {live_state_t state;char text[112];} history[256];
static unsigned history_n;
static live_state_t last_state;
static char last_text[112];
void live_voice_bridge_status(live_state_t state,const char *text){
    assert(text&&!strstr(text,TOKEN)&&strlen(text)<sizeof(last_text));
    if(history_n&&last_state==state&&!strcmp(last_text,text))return;
    last_state=state;snprintf(last_text,sizeof(last_text),"%s",text);
    assert(history_n<256);history[history_n].state=state;snprintf(history[history_n++].text,sizeof(last_text),"%s",text);
}
static bool shown(const char *text){
    for(unsigned i=0;i<history_n;i++)if(!strcmp(history[i].text,text))return true;
    return false;
}

/* ---- codec: a microphone that hears the speaker, a speaker that takes time ---- */
static bool codec_open,open_fail;
static uint64_t mic_samples,wire_samples;
static unsigned opens,closes,inhibits,mic_reads,mic_fail_at,spk_writes,spk_fail_at;
static int64_t spk_busy,mic_off_at,first_audible_at;
static uint64_t played,played_audible,played_after_off;
static struct {int64_t from,to;} loud[4096];
static unsigned loud_n;
static void audio_stream_inhibit(void){inhibits++;}
static esp_err_t audio_stream_open(audio_session_t **out){
    opens++;*out=NULL;yield_until(now+130000); /* codec start: silent DMA prime + settle */
    if(open_fail)return ESP_FAIL;
    assert(!codec_open);codec_open=true;*out=calloc(1,sizeof(**out));assert(*out);return ESP_OK;
}
static void audio_stream_close(audio_session_t *a){assert(codec_open&&!guard_held);codec_open=false;closes++;free(a);}
static esp_err_t audio_stream_read(audio_session_t *a,void *p,unsigned n){
    assert(a&&codec_open&&n==960);
    int64_t from=now;yield_until(now+20000);
    if(mic_fail_at&&++mic_reads==mic_fail_at)return ESP_FAIL;
    /* Every sample carries its own position since the codec opened, so the wire
     * seam can prove nothing was dropped, repeated or reordered. 0x1000..0x3fff:
     * the user speaking. 0x7000..0x77ff: this 20 ms also contains the pet's own
     * audible speaker output; production must never put that on the wire. */
    bool echo=false;
    for(unsigned i=0;i<loud_n&&!echo;i++)echo=loud[i].from<now&&loud[i].to>from;
    unsigned char *bytes=p;
    for(unsigned i=0;i<n;i+=2,mic_samples++){
        unsigned v=echo?0x7000u+(unsigned)(mic_samples%0x800):0x1000u+(unsigned)(mic_samples%0x3000);
        bytes[i]=(unsigned char)v;bytes[i+1]=(unsigned char)(v>>8);
    }
    return ESP_OK;
}
static esp_err_t audio_stream_write(audio_session_t *a,void *p,unsigned n){
    assert(a&&codec_open&&n&&n<=960&&n%2==0);
    if(spk_fail_at&&++spk_writes==spk_fail_at)return ESP_FAIL;
    int64_t from=now>spk_busy?now:spk_busy,to=from+(int64_t)n*1000000/48000;
    played+=n;
    if(mic_off_at&&now>mic_off_at)played_after_off+=n;
    if(live_pcm_audible(p,n)){
        played_audible+=n;if(!first_audible_at)first_audible_at=from;
        if(loud_n&&loud[loud_n-1].to>=from)loud[loud_n-1].to=to;
        else{assert(loud_n<4096);loud[loud_n].from=from;loud[loud_n++].to=to;}
    }
    spk_busy=to;yield_until(from); /* returns once the previous chunk has left the DAC */
    return ESP_OK;
}

/* ---- transport and the scripted host ---- */
static bridge_transport_t transport;
static bool transport_live,ws_up,new_fail,start_fail,token_fail,dispose_ok=true,host_closes=true;
static unsigned new_calls,start_calls,cancels,dispose_calls,send_fail_at,audio_sends;
static bool send_failed;
static void (*handler)(void*,esp_event_base_t,int32_t,void*);
static void *handler_arg;
static char sent[32][1400];
static unsigned sent_n,appends,appends_before_begin;
static uint64_t mic_sent,echo_bytes,zero_bytes,speech_bytes;
/* Independent oracles at the wire: when microphone audio started and stopped
 * arriving, the longest silence between frames, and the fastest it ever came. */
static int64_t first_append_at,last_append_at,max_append_gap,rate_floor;
static bool rate_floor_set;
static int64_t append_at[80000];
static uint64_t append_before[80000],peak_second; /* most bytes whose sends began within any one second */
static unsigned window_from;
#define MIC_FASTEST 96000 /* bytes per second: twice real time */
static int64_t send_latency_us=2000,session_deadline;
static esp_err_t network_copy_bridge_token(char *out,size_t capacity){
    if(token_fail)return ESP_FAIL;
    assert(capacity==44);memcpy(out,TOKEN,44);return ESP_OK;
}
static bridge_transport_t *bridge_transport_new(const setup_bridge_t *cfg,const char *token,int64_t deadline){
    assert(cfg&&!strcmp(token,TOKEN));new_calls++;
    if(new_fail)return NULL;
    assert(!transport_live); /* one chain: a previous call must have been released */
    transport=(bridge_transport_t){.deadline=deadline};session_deadline=deadline;transport_live=true;return &transport;
}
static esp_websocket_client_handle_t bridge_transport_client(bridge_transport_t *t){return t;}
static esp_err_t esp_websocket_register_events(esp_websocket_client_handle_t ws,int id,
        void (*fn)(void*,esp_event_base_t,int32_t,void*),void *arg){
    assert(ws==&transport&&id==WEBSOCKET_EVENT_ANY);handler=fn;handler_arg=arg;return ESP_OK;
}
static esp_err_t bridge_transport_start(bridge_transport_t *t){
    assert(t==&transport&&!t->started);start_calls++;t->started=true;return start_fail?ESP_FAIL:ESP_OK;
}
static bool bridge_transport_upgraded(bridge_transport_t *t){return t&&t->upgraded&&!t->cancelled;}
static bool bridge_transport_finished(const bridge_transport_t *t){return t&&t->finished;}
static bool bridge_transport_started(const bridge_transport_t *t){return t&&t->started;}
static bool up_at_fence;
static void bridge_transport_cancel(bridge_transport_t *t){
    if(!t)return;
    if(!t->cancelled)up_at_fence=ws_up; /* was the host's side still open when the socket was fenced? */
    t->cancelled=true;cancels++;
}
/* The SDK-task exit proof has its own suite; its result is injected here. */
static bool bridge_transport_dispose(bridge_transport_t *t,int64_t until){
    (void)until;if(!t)return true;
    assert(t==&transport&&t->cancelled&&!guard_held);dispose_calls++;handler=NULL;ws_up=false;
    if(!dispose_ok)return false;
    transport_live=false;return true;
}
static void later(int64_t us,ev_kind kind,const char *text){
    assert(script_n<512);script[script_n++]=(event_t){.at=now+us,.kind=kind,.text=text};
}
static int esp_websocket_client_send_text(esp_websocket_client_handle_t h,const char *s,size_t n,unsigned ticks){
    assert(h==&transport&&!guard_held&&ticks&&ticks<=1000&&n==strlen(s));
    assert(transport.started&&!transport.cancelled); /* nothing leaves after the fence */
    assert(!strstr(s,TOKEN));
    assert(!strstr(s,"pet.turn.")&&!strstr(s,"send_confirm")); /* a conversation never confirms a send */
    if(!ws_up)return -1;
    if(!strncmp(s,APPEND,sizeof(APPEND)-1)){
        assert(!send_failed); /* an uncertain append is never followed by another: no replay */
        if(send_fail_at&&++audio_sends==send_fail_at){send_failed=true;return (int)n-1;}
        unsigned char pcm[4800];size_t bytes=0;const char *b64=s+sizeof(APPEND)-1,*end=strchr(b64,'"');
        assert(end&&!strcmp(end,"\"}"));
        assert(!mbedtls_base64_decode(pcm,sizeof(pcm),&bytes,(const unsigned char*)b64,(size_t)(end-b64)));
        assert(bytes&&bytes<=4800&&bytes%2==0);
        /* The first 4800 samples after the codec opens are discarded as stale;
         * from there every captured sample appears once, in order. */
        if(!appends)wire_samples=4800;
        for(size_t i=0;i<bytes;i+=2,wire_samples++){
            unsigned v=pcm[i]|(unsigned)pcm[i+1]<<8;
            if(!v)zero_bytes+=2;
            else if(v>=0x7000)echo_bytes+=2;
            else{
                if(v!=0x1000u+(unsigned)(wire_samples%0x3000))
                    fprintf(stderr,"microphone sample %llu out of sequence: got 0x%04x\n",(unsigned long long)wire_samples,v);
                assert(v==0x1000u+(unsigned)(wire_samples%0x3000));speech_bytes+=2;
            }
        }
        assert(wire_samples<=mic_samples);
        /* Over ANY span of sends, never more than twice real time (one 20 ms
         * chunk of slack): for all k<j, C(j)-C(k) <= 96000*(S(j)-S(k)). */
        int64_t lead=(int64_t)mic_sent*1000000-(int64_t)MIC_FASTEST*now;
        if(!rate_floor_set||lead<rate_floor){rate_floor=lead;rate_floor_set=true;}
        assert(lead-rate_floor<=960*1000000LL);
        assert(appends<80000);append_at[appends]=now;append_before[appends]=mic_sent;
        while(append_at[window_from]<=now-1000000)window_from++;
        if(mic_sent+bytes-append_before[window_from]>peak_second)peak_second=mic_sent+bytes-append_before[window_from];
        if(!first_append_at)first_append_at=now;
        else if(now-last_append_at>max_append_gap)max_append_gap=now-last_append_at;
        last_append_at=now;
        mic_sent+=bytes;appends++;
        if(sent_n<2)appends_before_begin++;
    }else{
        assert(sent_n<32&&n<sizeof(sent[0]));memcpy(sent[sent_n++],s,n+1);
        if(!strcmp(s,SESSION_CLOSE)&&host_closes){later(20000,EV_TEXT,CLOSED);later(25000,EV_WS_CLOSED,NULL);}
    }
    yield_until(now+send_latency_us);
    return (int)n;
}

#include "bridge-converse-production.inc"
_Static_assert(BV_MIC==96000,"the harness identifies the microphone buffer by its size");

/* ---- script ---- */
static setup_bridge_t cfg;
static bool parent_stop;
static int64_t run_start,run_end;
static bool parent_stopped(void){return parent_stop;}
static void deliver(const char *json){
    if(!handler||!ws_up)return;
    int n=(int)strlen(json);
    esp_websocket_event_data_t d={.data_len=n,.payload_len=n,.op_code=1,.fin=true,.data_ptr=json};
    handler(handler_arg,NULL,WEBSOCKET_EVENT_DATA,&d);
}
static void fire(event_t *e){
    switch(e->kind){
    case EV_CONNECT:
        if(handler&&!transport.cancelled){transport.upgraded=true;ws_up=true;handler(handler_arg,NULL,WEBSOCKET_EVENT_CONNECTED,NULL);}
        break;
    case EV_TEXT:deliver(e->text);break;
    case EV_WS_CLOSED:if(handler&&ws_up){ws_up=false;handler(handler_arg,NULL,WEBSOCKET_EVENT_CLOSED,NULL);}break;
    case EV_WS_DROP:if(handler&&ws_up){ws_up=false;handler(handler_arg,NULL,WEBSOCKET_EVENT_DISCONNECTED,NULL);}break;
    case EV_WS_ERROR:
        if(handler&&ws_up){
            handler(handler_arg,NULL,WEBSOCKET_EVENT_ERROR,NULL);ws_up=false;
            handler(handler_arg,NULL,WEBSOCKET_EVENT_DISCONNECTED,NULL);
        }
        break;
    case EV_REFUSED:transport.finished=true;break;
    case EV_PARENT_STOP:mic_off_at=now;parent_stop=true;break;
    case EV_MIC_OFF:{
        /* The tap, as live_voice_stop: parent latch, then the bridge. */
        mic_off_at=now;parent_stop=true;
        bool live=current&&atomic_load(&current->active);
        bridge_voice_stop();
        if(live){
            /* Mic off is immediate: both gates shut and the amplifier inhibited
             * before the owner has even noticed; the socket is left for its stop. */
            assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate)&&inhibits);
            assert(atomic_load(&current->stop)&&!transport.cancelled);
        }
        break;}
    case EV_CHECK:e->check();break;
    }
}
/* Provider audio through the host's pacer: 4800 bytes each 100 ms, audible
 * inside the given windows, otherwise digital silence (or nothing, if the
 * host omits silence). A stall delivers its backlog in one burst afterwards. */
static struct {int64_t from,to;} speech[32];
static unsigned speech_n;
static bool omit_silence;
static int64_t stall_from,stall_to;
static unsigned stalled;
static uint64_t host_audible,host_silent;
static void host_frame(int64_t at){
    bool audible=false;
    for(unsigned i=0;i<speech_n;i++)if(at>=speech[i].from&&at<speech[i].to)audible=true;
    if(!audible&&omit_silence)return;
    if(!handler||!ws_up)return;
    static unsigned char pcm[4800];static char json[7000];size_t n=0;
    for(unsigned i=0;i<sizeof(pcm);i+=2){pcm[i]=audible?0xd0:0;pcm[i+1]=audible?(i%4?0x07:0xf8):0;} /* +-2000 */
    int at_=snprintf(json,sizeof(json),"{\"type\":\"session.output_audio.delta\",\"delta\":\"");
    assert(!mbedtls_base64_encode((unsigned char*)json+at_,sizeof(json)-(size_t)at_-3,&n,pcm,sizeof(pcm)));
    memcpy(json+at_+n,"\"}",3);
    if(audible)host_audible+=sizeof(pcm);else host_silent+=sizeof(pcm);
    deliver(json);
}
static void host_tick(void){
    int64_t at=host_next;host_next+=100000;
    if(stall_to&&at>=stall_from&&at<stall_to){stalled++;return;}
    for(;stalled;stalled--)host_frame(at-(int64_t)stalled*100000);
    host_frame(at);
}
static void at(int64_t ms,ev_kind kind,const char *text){
    assert(script_n<512);script[script_n++]=(event_t){.at=run_start+ms*1000,.kind=kind,.text=text};
}
static void check(int64_t ms,void (*fn)(void)){
    assert(script_n<512);script[script_n++]=(event_t){.at=run_start+ms*1000,.kind=EV_CHECK,.check=fn};
}
static void host_audio(int64_t from_ms){host_next=run_start+from_ms*1000;}
static void says(int64_t from_ms,int64_t to_ms){
    assert(speech_n<32);speech[speech_n].from=run_start+from_ms*1000;speech[speech_n++].to=run_start+to_ms*1000;
}
/* Fixture state only. Production statics are NEVER reset between calls: every
 * later call in a process is a conversation started after an earlier one ended. */
static void fresh(void){
    script_n=0;host_next=0;speech_n=0;omit_silence=false;stall_from=stall_to=0;stalled=0;host_audible=host_silent=0;
    parent_stop=false;mic_off_at=0;first_audible_at=0;played=played_audible=played_after_off=0;loud_n=0;spk_busy=0;
    open_fail=false;mic_reads=mic_fail_at=spk_writes=spk_fail_at=0;opens=closes=inhibits=0;
    tasks_created=tasks_exited=task_fail_at=task_attempts=0;
    new_fail=start_fail=token_fail=false;dispose_ok=true;host_closes=true;
    new_calls=start_calls=cancels=dispose_calls=send_fail_at=audio_sends=0;send_failed=false;up_at_fence=false;
    sent_n=appends=appends_before_begin=0;mic_sent=echo_bytes=zero_bytes=speech_bytes=0;send_latency_us=2000;
    mic_samples=wire_samples=0;mic_high=mic_waiting=0;frames_high=0;
    late_min_us=late_max_us=owner_stall_at=owner_stall_us=0;late_seed=20261115u;
    first_append_at=last_append_at=max_append_gap=rate_floor=0;rate_floor_set=false;peak_second=0;window_from=0;
    queues_made=queues_deleted=streams_made=streams_deleted=groups_made=groups_deleted=0;
    history_n=0;last_text[0]=0;now+=3000000;run_start=now;
}
static void opened(void){ /* the design's sequence up to "listening" */
    at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);at(80,EV_TEXT,G_BEGAN);at(300,EV_TEXT,G_STATE("listening"));
}
/* One tap on the microphone button with a bridge configured. */
static void call(bool retained){
    entities[0].live=true;
    bridge_voice_run(&cfg,now+SESSION_US,parent_stopped);
    run_end=now;
    for(unsigned i=1;i<entity_n;i++)if(!entities[i].joined){
        assert(!entities[i].live);assert(!pthread_join(entities[i].thread,NULL));entities[i].joined=true;
    }
    assert(!guard_held&&tasks_created==tasks_exited);
    assert(new_calls<=1&&start_calls<=1);       /* one connection per tap: never a reconnect */
    assert(echo_bytes==0);                      /* the pet's own voice is never sent as the user's */
    assert(mic_sent==echo_bytes+zero_bytes+speech_bytes);
    if(appends)assert(wire_samples*2==9600+mic_sent&&wire_samples<=mic_samples);
    assert(appends_before_begin==0);            /* microphone audio only after pet.converse.begin */
    for(unsigned i=0;i<history_n;i++)assert(!strstr(history[i].text,TOKEN));
    for(unsigned i=0;i<sent_n;i++){
        const char *known=i==0?PET_LIVE_START:i==1&&strcmp(sent[1],PET_STOP)?G_BEGIN:NULL;
        if(known)assert(!strcmp(sent[i],known));
        else assert(!strcmp(sent[i],PET_STOP)||!strcmp(sent[i],SESSION_CLOSE));
    }
    if(retained)return;
    /* Idle again and reusable: everything released, nothing held for a reboot. */
    assert(current==NULL&&!bridge_voice_restart_required()&&!codec_open&&!transport_live);
    assert(closes==opens-(open_fail?1:0));
    assert(queues_made==queues_deleted&&streams_made==streams_deleted&&groups_made==groups_deleted);
    assert(last_state==LIVE_OFF||last_state==LIVE_ERROR);
    bridge_view_t view;assert(bridge_voice_snapshot(&view)&&view.phase==BR_FINISHED&&!view.can_stop&&!view.can_send);
    assert(!strcmp(view.status,last_text));
}
static void ended(live_state_t state,const char *text){
    if(last_state!=state||strcmp(last_text,text))
        fprintf(stderr,"ended with state=%d \"%s\", expected state=%d \"%s\"\n",last_state,last_text,state,text);
    assert(last_state==state&&!strcmp(last_text,text));
}
static void stopped_cleanly(unsigned frames_before_stop){
    assert(sent_n==frames_before_stop+2);
    assert(!strcmp(sent[sent_n-2],PET_STOP)&&!strcmp(sent[sent_n-1],SESSION_CLOSE));
    ended(LIVE_OFF,"Mic off");
    assert(run_end-mic_off_at<=2000000); /* mic off returns to idle promptly */
    /* The stop was not cut off by our own fence: the host closed its side first,
     * or (a host that never answers) the bounded wait ran out. */
    if(host_closes)assert(!up_at_fence&&run_end-mic_off_at<200000);
    else assert(up_at_fence&&run_end-mic_off_at>=1500000);
}

/* ---- checks made while a call is running ---- */
static void is_conversing(void){
    assert(current&&current->model.phase==BR_CONVERSING&&codec_open&&tasks_created==2);
    assert(atomic_load(&current->capture_gate)&&atomic_load(&current->play_gate));
}
static void see(live_state_t state,const char *text){is_conversing();ended(state,text);}
static void sees_listening(void){see(LIVE_LISTENING,"Listening");}
static void sees_asking(void){see(LIVE_THINKING,"Asking Unified...");}
static void sees_speaking(void){see(LIVE_SPEAKING,"Speaking");}
static void sees_not_sent(void){see(LIVE_LISTENING,"Not sent");}
static void sees_connecting(void){ended(LIVE_CONNECTING,"Connecting...");assert(!codec_open&&!opens);}
static uint64_t mark_zero,mark_speech;
static void mark(void){mark_zero=zero_bytes;mark_speech=speech_bytes;}
static void spoke_muted(void){
    /* While the pet was audibly speaking, only zeroed microphone PCM went out. */
    is_conversing();assert(played_audible&&zero_bytes>mark_zero+40000&&speech_bytes==mark_speech);
}
static void heard_again(void){is_conversing();assert(speech_bytes>mark_speech+20000);}
static void ignored_two(void){is_conversing();assert(current->model.ignored>=2);}
static void all_played(void){is_conversing();assert(host_audible&&played_audible==host_audible);}
static void backlog_seen(void){is_conversing();assert(mic_waiting>=48000);}
static void backlog_gone(void){is_conversing();assert(mic_waiting<=9600);}
static void lag_shed(void){
    is_conversing();
    assert(xStreamBufferBytesAvailable(current->playback)<=BV_CATCHUP+4800+960);
}

/* ---- cases ---- */
static void normal(void){
    fresh();opened();host_audio(100);says(3000,5000);says(9000,10500);
    check(20,sees_connecting);check(450,sees_listening);
    at(2500,EV_TEXT,G_STATE("asking"));check(2700,sees_asking);
    at(3000,EV_TEXT,G_STATE("answering"));check(3600,sees_speaking);check(3700,mark);check(4900,spoke_muted);
    at(5200,EV_TEXT,G_STATE("listening"));check(5500,sees_listening);check(5600,mark);check(6500,heard_again);
    at(6000,EV_TEXT,G_NOTICE("not_sent"));check(6200,sees_not_sent);
    at(7000,EV_TEXT,G_STATE("dreaming"));at(7100,EV_TEXT,G_NOTICE("brand_new"));check(7300,ignored_two);
    check(7400,sees_not_sent);
    at(8500,EV_TEXT,G_STATE("asking"));at(9000,EV_TEXT,G_STATE("answering"));check(10200,sees_speaking);
    at(10700,EV_TEXT,G_STATE("listening"));check(11000,sees_listening);
    at(12000,EV_MIC_OFF,NULL);
    call(false);stopped_cleanly(2);
    assert(opens==1&&closes==1&&tasks_created==2&&dispose_calls==1&&session_deadline==run_start+SESSION_US);
    assert(shown("Connecting...")&&shown("Listening")&&shown("Asking Unified...")&&shown("Speaking")&&shown("Not sent"));
    assert(played_audible==host_audible&&host_audible==(2000+1500)*48);     /* every spoken byte reached the speaker */
    assert(speech_bytes>300000&&zero_bytes>150000&&zero_bytes<230000);      /* muted only around its own speech */
    assert(mic_sent>48000*11&&mic_sent<=48000*12);                          /* streamed for the whole conversation */
    printf("normal: mic_sent=%llu speech=%llu zeroed=%llu echo=%llu appends=%u played=%llu audible=%llu close_us=%lld\n",
        (unsigned long long)mic_sent,(unsigned long long)speech_bytes,(unsigned long long)zero_bytes,
        (unsigned long long)echo_bytes,appends,(unsigned long long)played,(unsigned long long)played_audible,
        (long long)(run_end-mic_off_at));
}
static void off_stage(const char *name){
    fresh();
    if(!strcmp(name,"off-connecting")){
        at(10,EV_MIC_OFF,NULL);at(40,EV_CONNECT,NULL);call(false);
        assert(sent_n==0&&!opens);ended(LIVE_OFF,"Mic off");
    }else if(!strcmp(name,"off-connected")){
        at(40,EV_CONNECT,NULL);at(100,EV_MIC_OFF,NULL);call(false);stopped_cleanly(1);assert(!opens);
    }else if(!strcmp(name,"off-started")){
        at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);at(75,EV_MIC_OFF,NULL);at(80,EV_TEXT,G_BEGAN);
        call(false);stopped_cleanly(2);assert(!opens&&!tasks_created);
    }else if(!strcmp(name,"off-opening")){
        opened();at(150,EV_MIC_OFF,NULL);call(false);stopped_cleanly(2);
        assert(opens==1&&closes==1&&!tasks_created&&!appends&&!played);
    }else{
        opened();host_audio(100);
        if(!strcmp(name,"off-asking"))at(500,EV_TEXT,G_STATE("asking"));
        if(!strcmp(name,"off-answering")){at(500,EV_TEXT,G_STATE("answering"));says(600,5000);}
        if(!strcmp(name,"off-no-reply"))host_closes=false; /* the host never answers our close */
        at(1500,strcmp(name,"off-parent")?EV_MIC_OFF:EV_PARENT_STOP,NULL);
        call(false);stopped_cleanly(2);
        assert(opens==1&&closes==1&&tasks_created==2&&appends);
        assert(played_after_off<=960); /* at most the chunk already in flight */
        if(!strcmp(name,"off-answering"))assert(played_audible>20000&&inhibits);
    }
}
static void host_end(const char *name){
    fresh();opened();host_audio(100);
    if(!strcmp(name,"host-quiet")||!strcmp(name,"host-time-limit")){
        bool quiet_=!strcmp(name,"host-quiet");
        at(2000,EV_TEXT,quiet_?G_NOTICE("idle_timeout"):G_NOTICE("time_limit"));
        at(2600,EV_TEXT,CLOSED);at(2610,EV_WS_CLOSED,NULL);
        call(false);ended(LIVE_OFF,quiet_?"Mic off (quiet)":"Mic off (time limit)");
    }else if(!strcmp(name,"host-close")){
        at(2000,EV_TEXT,CLOSED);at(2010,EV_WS_CLOSED,NULL);
        call(false);ended(LIVE_OFF,"Mic off (host ended the call)");
    }else if(!strcmp(name,"host-drop")){
        at(2000,EV_WS_DROP,NULL);call(false);ended(LIVE_ERROR,"Host disconnected.");
    }else if(!strcmp(name,"host-burst-error")){
        /* The host said why and closed, and the socket failed in the same
         * instant, with those frames still queued behind another. Its reason is kept. */
        at(2000,EV_TEXT,G_STATE("asking"));at(2000,EV_TEXT,G_NOTICE("idle_timeout"));
        at(2000,EV_TEXT,CLOSED);at(2000,EV_WS_ERROR,NULL);
        call(false);ended(LIVE_OFF,"Mic off (quiet)");
    }else{
        at(2000,EV_WS_ERROR,NULL);call(false);ended(LIVE_ERROR,"Host connection interrupted.");
    }
    assert(opens==1&&closes==1&&tasks_created==2&&run_end-run_start<5000000);
}
static void host_error(bool mid){
    fresh();
    if(mid){opened();host_audio(100);at(2000,EV_TEXT,"{\"type\":\"pet.error\",\"code\":\"reply_unavailable\"}");}
    else{at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);at(80,EV_TEXT,"{\"type\":\"pet.error\",\"code\":\"busy\"}");}
    call(false);
    ended(LIVE_ERROR,mid?"Host error: reply_unavailable":"That conversation is busy");
    assert(opens==(mid?1U:0U)&&!strcmp(sent[1],G_BEGIN));
}
static void malformed(const char *name){
    static const char *const frames[]={
        "{\"type\":\"pet.converse.state\"}",
        "{\"type\":\"pet.converse.state\",\"state\":5}",
        "{\"type\":\"pet.converse.state\",\"state\":\"asking\",\"more\":\"x\"}",
        "{\"type\":\"pet.converse.notice\"}",
        "{\"type\":\"pet.converse.notice\",\"code\":3}",
        "{\"type\":\"pet.converse.began\",\"safe_session_label\":\"Example conversation\"}",
        "{\"type\":\"session.output_audio.delta\",\"delta\":\"AQ==\"}",
        "{\"type\":\"pet.converse.state\",\"state\":\"asking\"",
        "not json"};
    static const char *const turn[]={
        "{\"type\":\"pet.turn.began\",\"turn_id\":\"" TURN "\",\"safe_session_label\":\"Existing\",\"session_id_short\":\"23133f96\"}",
        "{\"type\":\"pet.turn.send_result\",\"turn_id\":\"" TURN "\",\"state\":\"accepted\"}",
        "{\"type\":\"pet.reply.started\",\"turn_id\":\"" TURN "\",\"rendering\":\"spoken_rendering_not_verbatim\"}"};
    static const char *const unknown[]={"{\"type\":\"session.input_transcript.delta\",\"delta\":\"words\"}"};
    const char *const *list=!strcmp(name,"malformed")?frames:!strcmp(name,"turn-frame")?turn:unknown;
    unsigned n=!strcmp(name,"malformed")?9:!strcmp(name,"turn-frame")?3:1;
    for(unsigned i=0;i<n;i++){
        fresh();opened();host_audio(100);at(1000,EV_TEXT,list[i]);call(false);
        ended(LIVE_ERROR,!strcmp(name,"turn-frame")?"Host sent an unexpected message":"Host sent an invalid message.");
        assert(opens==1&&closes==1&&run_end-run_start<4000000);
    }
}
static void timeouts(const char *name){
    fresh();
    if(!strcmp(name,"no-connect")){
        call(false);ended(LIVE_ERROR,"Could not reach the host.");
        assert(sent_n==0&&!opens&&run_end-run_start>=25000000&&run_end-run_start<26000000);
        fresh();at(700,EV_REFUSED,NULL);call(false);ended(LIVE_ERROR,"Could not reach the host."); /* refused at once */
        assert(sent_n==0&&run_end-run_start<1000000);
    }else if(!strcmp(name,"no-begin")){
        at(40,EV_CONNECT,NULL);call(false);ended(LIVE_ERROR,"Host did not start the call.");
        assert(sent_n==3&&!opens&&run_end-run_start>=30000000&&run_end-run_start<32000000);
        fresh();at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);call(false);
        ended(LIVE_ERROR,"Host did not start the call.");assert(sent_n==4&&!strcmp(sent[1],G_BEGIN)&&!opens);
    }else{
        /* A host that never closes: the device's own backstop, as a normal end. */
        opened();host_audio(100);call(false);ended(LIVE_OFF,"Mic off (time limit)");
        assert(run_end-run_start>=SESSION_US-2000000&&run_end-run_start<=SESSION_US);
        assert(mic_sent>(uint64_t)48000*655&&current==NULL);
        assert(!strcmp(sent[sent_n-2],PET_STOP)&&!strcmp(sent[sent_n-1],SESSION_CLOSE));
        printf("deadline: seconds=%lld mic_sent=%llu appends=%u played=%llu\n",(long long)((run_end-run_start)/1000000),
            (unsigned long long)mic_sent,appends,(unsigned long long)played);
    }
}
static void failures(const char *name){
    fresh();opened();host_audio(100);
    const char *text=NULL;
    if(!strcmp(name,"audio-open")){open_fail=true;text="Microphone or speaker unavailable.";}
    else if(!strcmp(name,"mic-read")){mic_fail_at=100;text="Microphone read failed.";}
    else if(!strcmp(name,"speaker-write")){spk_fail_at=40;text="Speaker playback failed.";}
    else if(!strcmp(name,"send-short")){send_fail_at=30;text="Voice send failed. No replay.";}
    else if(!strcmp(name,"setup-token")){token_fail=true;text="Could not start the host connection.";}
    else if(!strcmp(name,"setup-start")){start_fail=true;text="Could not start the host connection.";}
    else if(!strcmp(name,"stall")){send_latency_us=900000;text="Microphone stream interrupted.";}
    else if(!strcmp(name,"flood")){for(unsigned i=0;i<40;i++)at(2000,EV_TEXT,G_STATE("asking"));text="Host connection interrupted.";}
    call(false);ended(LIVE_ERROR,text);
    assert(run_end-run_start<20000000);
    if(!strcmp(name,"setup-token"))assert(!new_calls&&!sent_n);
    if(!strcmp(name,"setup-start"))assert(new_calls==1&&dispose_calls==1&&!sent_n);
    if(!strcmp(name,"send-short"))assert(send_failed&&audio_sends==30);
    if(!strcmp(name,"setup-token")){
        fresh();new_fail=true;call(false);ended(LIVE_ERROR,"Could not start the host connection.");
        fresh();opened();task_fail_at=2;call(false);ended(LIVE_ERROR,"Microphone or speaker unavailable.");
        assert(tasks_created==1&&opens==1&&closes==1);
    }
}
static void burst_ok(void){
    fresh();opened();host_audio(100);
    for(unsigned i=0;i<20;i++)at(2000,EV_TEXT,i%2?G_STATE("asking"):G_STATE("listening"));
    check(2600,sees_asking);at(3000,EV_MIC_OFF,NULL);call(false);stopped_cleanly(2);
}
static void gappy(void){
    /* A host that omits digital silence: audio arrives only while it speaks. */
    fresh();opened();omit_silence=true;host_audio(100);says(1000,2000);says(4000,4500);says(5500,5700);says(7000,9000);
    /* Each utterance is fully played soon after it arrives, including one shorter
     * than the jitter buffer: it is not held back until the next one. */
    check(3000,all_played);check(5300,all_played);check(6500,all_played);check(10000,all_played);
    at(11000,EV_MIC_OFF,NULL);call(false);stopped_cleanly(2);
    assert(host_silent==0&&host_audible==(1000+500+200+2000)*48&&played_audible==host_audible);
    assert(first_audible_at-run_start>=1000000&&first_audible_at-run_start<=1600000);
    /* After a gap the jitter buffer is rebuilt before speech restarts. */
    int64_t second=0;
    for(unsigned i=0;i<loud_n&&!second;i++)if(loud[i].from>=run_start+3900000)second=loud[i].from-run_start;
    assert(second>=4250000&&second<=4600000);
    assert(speech_bytes>200000&&zero_bytes>150000);
}
static void lag(void){
    /* Two seconds of network stall, then the backlog at once: without shedding,
     * every later word would be played two seconds late for the rest of the call. */
    fresh();opened();host_audio(100);stall_from=run_start+2000000;stall_to=run_start+4000000;
    check(7000,lag_shed);says(9000,10000);at(11500,EV_MIC_OFF,NULL);
    call(false);stopped_cleanly(2);
    assert(played_audible==host_audible&&host_audible==1000*48);       /* speech is never what gets dropped */
    assert(first_audible_at-run_start<=9700000);
    assert(played<host_silent+host_audible);                           /* only digital silence was skipped */
}
/* The condition of the real calls (sittings 2 and 2b, build 10): the host
 * streams output audio continuously at about 10 frames a second and the owner
 * loop is routinely late. Build 10 delivered the microphone at about 94% of
 * real time and ended every call itself, after 31-35 s, when the shortfall
 * reached its 2 s buffer. A 600 s call must instead be ended by the HOST. */
static double mic_rate(void){
    /* Microphone audio delivered, against the time over which it was delivered. */
    return last_append_at>first_append_at?
        100.0*(double)mic_sent/48000.0/((double)(last_append_at-first_append_at)/1e6):0;
}
static void late_report(const char *name){
    printf("%s: ended_s=%.2f status=\"%s\" mic_delivered_s=%.2f over_s=%.2f rate=%.2f%% "
           "mic_backlog_high_ms=%zu host_frames_queued_high=%u appends=%u max_gap_ms=%lld fastest_second=%.2fx\n",
        name,(double)(run_end-run_start)/1e6,last_text,(double)mic_sent/48000.0,
        (double)(last_append_at-first_append_at)/1e6,mic_rate(),mic_high/48,frames_high,appends,
        (long long)(max_append_gap/1000),(double)peak_second/48000.0);
}
static void late_call(const char *name,int64_t min_ms,int64_t max_ms){
    fresh();opened();host_audio(100);
    late_min_us=min_ms*1000;late_max_us=max_ms*1000;
    /* The host speaks an answer every minute; otherwise it streams silence. */
    for(int64_t t=20000;t<590000;t+=60000){
        at(t-3000,EV_TEXT,G_STATE("asking"));at(t,EV_TEXT,G_STATE("answering"));says(t,t+6000);
        at(t+6200,EV_TEXT,G_STATE("listening"));
    }
    /* The gateway's own limit ends a healthy call: notice, close, socket close. */
    at(600000,EV_TEXT,G_NOTICE("time_limit"));at(600100,EV_TEXT,CLOSED);at(600110,EV_WS_CLOSED,NULL);
    call(false);late_report(name);
    ended(LIVE_OFF,"Mic off (time limit)");
    assert(run_end-run_start>=600000000&&run_end-run_start<603000000);
    assert(mic_rate()>99.5&&mic_rate()<100.5);            /* real time, neither behind nor ahead */
    assert(mic_sent>(uint64_t)48000*598);                 /* the whole call's microphone audio arrived */
    assert(mic_high<=24000);                              /* never more than 0.5 s waiting, of a 2 s buffer */
    assert(frames_high<=4);                               /* one host frame per iteration keeps up */
    assert(played_audible==host_audible&&host_audible==10*6000*48); /* every spoken byte was played */
    assert(speech_bytes>(uint64_t)48000*400&&zero_bytes>(uint64_t)48000*55);
    assert(max_append_gap<=250000);
    assert(peak_second<=96000+4800);                      /* never beyond twice real time in any second */
}
static void recover(void){
    /* One 1.5 s stop of the owner (a Wi-Fi hiccup, say): 1.5 s of microphone
     * audio waits, then is delivered complete and in order, no faster than
     * twice real time, and the call goes on. */
    fresh();opened();host_audio(100);says(9000,10000);
    owner_stall_at=run_start+3000000;owner_stall_us=1500000;
    check(4600,backlog_seen);check(8000,backlog_gone);at(12000,EV_MIC_OFF,NULL);
    call(false);late_report("recover");stopped_cleanly(2);
    assert(mic_high>=72000&&mic_high<96000);
    assert(peak_second>=72000&&peak_second<=96000+4800);  /* it did catch up: 1.5x to 2x real time ... */
    assert(mic_sent>(uint64_t)48000*11);                  /* ... and nothing was left behind */
    assert(played_audible==host_audible&&frames_high>=14&&frames_high<=20);
}
static void stall_long(void){
    /* A genuine stall longer than the 2 s buffer still ends the call honestly
     * rather than dropping the user's words. Catching up does not hide it. */
    fresh();opened();host_audio(100);
    owner_stall_at=run_start+3000000;owner_stall_us=2600000;
    call(false);late_report("stall-long");ended(LIVE_ERROR,"Microphone stream interrupted.");
    assert(mic_high==96000&&run_end-run_start<7000000);
}
static void retained(void){
    /* Release proof missing: nothing is freed or reused, and nothing reconnects. */
    fresh();opened();host_audio(100);dispose_ok=false;at(1000,EV_MIC_OFF,NULL);
    call(true);
    ended(LIVE_CLOSING,"Releasing voice connection...");
    assert(current&&bridge_voice_restart_required()&&codec_open&&transport_live&&!queues_deleted);
    unsigned kept_opens=opens;
    fresh();call(true);ended(LIVE_ERROR,"Voice cleanup incomplete. Connection held.");
    assert(!new_calls&&!sent_n&&current&&codec_open);
    /* Proof arrives later: the zero-wait reaper releases it, with no reboot. */
    dispose_ok=true;bridge_voice_reap_finished();
    assert(current==NULL&&!bridge_voice_restart_required()&&!codec_open&&!transport_live&&kept_opens==1);
    ended(LIVE_OFF,"Mic off");
    fresh();opened();host_audio(100);at(1000,EV_MIC_OFF,NULL);call(false);stopped_cleanly(2);
}
static void sequence(void){
    /* One process, no reset of the firmware's state: each call follows an
     * earlier one that ended a different way. */
    for(unsigned round=0;round<2;round++){
        fresh();opened();host_audio(100);at(1500,EV_MIC_OFF,NULL);call(false);stopped_cleanly(2);
        fresh();opened();host_audio(100);at(1500,EV_TEXT,G_NOTICE("time_limit"));at(1600,EV_TEXT,CLOSED);
        at(1610,EV_WS_CLOSED,NULL);call(false);ended(LIVE_OFF,"Mic off (time limit)");
        fresh();opened();at(900,EV_TEXT,"{\"type\":\"pet.error\",\"code\":\"busy\"}");call(false);
        ended(LIVE_ERROR,"That conversation is busy");
        fresh();opened();host_audio(100);at(900,EV_TEXT,"{\"type\":\"pet.converse.state\"}");call(false);
        ended(LIVE_ERROR,"Host sent an invalid message.");
        fresh();opened();host_audio(100);at(900,EV_WS_ERROR,NULL);call(false);
        ended(LIVE_ERROR,"Host connection interrupted.");
        fresh();at(300,EV_REFUSED,NULL);call(false);ended(LIVE_ERROR,"Could not reach the host.");
        fresh();opened();host_audio(100);mic_fail_at=60;call(false);ended(LIVE_ERROR,"Microphone read failed.");
    }
    normal();
}
int main(int argc,char **argv){
    setbuf(stdout,NULL);assert(argc==2);const char *name=argv[1];
    printf("CASE %s (whole production owner + audio tasks; synthetic clock/RTOS/codec/transport/host)\n",name);
    bridge_voice_init();
    if(!strcmp(name,"normal"))normal();
    else if(!strncmp(name,"off-",4))off_stage(name);
    else if(!strcmp(name,"host-error"))host_end(name);
    else if(!strncmp(name,"host-",5))host_end(name);
    else if(!strcmp(name,"error-busy"))host_error(false);
    else if(!strcmp(name,"error-mid"))host_error(true);
    else if(!strcmp(name,"malformed")||!strcmp(name,"turn-frame")||!strcmp(name,"unknown-type"))malformed(name);
    else if(!strcmp(name,"no-connect")||!strcmp(name,"no-begin")||!strcmp(name,"deadline"))timeouts(name);
    else if(!strcmp(name,"burst-ok"))burst_ok();
    else if(!strcmp(name,"gappy"))gappy();
    else if(!strcmp(name,"lag"))lag();
    else if(!strcmp(name,"retained"))retained();
    else if(!strcmp(name,"late-600"))late_call(name,8,20);
    else if(!strcmp(name,"late-600-heavy"))late_call(name,20,40);
    else if(!strcmp(name,"recover"))recover();
    else if(!strcmp(name,"stall-long"))stall_long();
    else if(!strcmp(name,"sequence"))sequence();
    else if(!strcmp(name,"audio-open")||!strcmp(name,"mic-read")||!strcmp(name,"speaker-write")||!strcmp(name,"send-short")||
            !strcmp(name,"setup-token")||!strcmp(name,"setup-start")||!strcmp(name,"stall")||!strcmp(name,"flood"))failures(name);
    else{fprintf(stderr,"unknown case: %s\n",name);return 2;}
    printf("PASS %s\n",name);return 0;
}
