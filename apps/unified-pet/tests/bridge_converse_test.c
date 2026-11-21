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
#ifdef BRIDGE_JOIN_REAL_PARENT
#include <errno.h>
#include <time.h>
#endif

#define G_BEGIN "{\"type\":\"pet.converse.begin\"}"
#define G_BEGAN "{\"type\":\"pet.converse.began\",\"safe_session_label\":\"Example conversation\",\"session_id_short\":\"a1b2c3d4\"}"
#define G_STATE(x) "{\"type\":\"pet.converse.state\",\"state\":\"" x "\"}"
#define G_NOTICE(x) "{\"type\":\"pet.converse.notice\",\"code\":\"" x "\"}"
#define PENDING_TEXT "Send unconfirmed"
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
typedef int BaseType_t;
#define tskNO_AFFINITY 0x7fffffff
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
typedef struct {char *text;int64_t received_at;bool after_local_control;} fixture_frame_t;
typedef struct queue {fixture_frame_t items[64];unsigned n,cap;} *QueueHandle_t;
typedef void *SemaphoreHandle_t;
typedef void *esp_websocket_client_handle_t;
typedef struct audio_session {unsigned unused;} audio_session_t;
typedef enum {BR_TRANSPORT_NONE,BR_TRANSPORT_PROTOCOL,BR_TRANSPORT_POLICY,BR_TRANSPORT_EVENT_LOSS,BR_TRANSPORT_PEER_NORMAL,BR_TRANSPORT_UNSUPPORTED,BR_TRANSPORT_PAYLOAD_REJECTED,BR_TRANSPORT_CERT_VERIFY,BR_TRANSPORT_TLS_UNKNOWN} bridge_transport_terminal_t;
typedef struct bridge_transport {bool started,upgraded,cancelled,finished,local_control;int http;int64_t deadline;bridge_transport_terminal_t terminal;bool (*stop_check)(void *);void *stop_context;} bridge_transport_t;
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
static int64_t retry_prepare_delay_us,registration_delay_us,retry_start_delay_us;
#ifdef BRIDGE_JOIN_REAL_PARENT
static _Thread_local bool guard_held;
#else
static bool guard_held;
#endif
static bool fail_next_guard,fail_tail_guard,fail_message_alloc,delay_message_alloc;

typedef enum {EV_CONNECT,EV_TEXT,EV_PARTIAL_TEXT,EV_WS_CLOSED,EV_WS_DROP,EV_WS_ERROR,EV_TLS_FAILURE,EV_RAW_CLOSE,EV_CLOSE_1011,EV_CLOSE_1000,EV_PROTOCOL_CLOSE,EV_POLICY_CLOSE,EV_MIC_OFF,EV_PARENT_STOP,
              EV_REFUSED,EV_CHECK,EV_HTTP} ev_kind;
/* call: the connection (1, 2, ... within one mic-on) a host event belongs to.
 * The host of an ended call says nothing more; mic off and checks are global. */
typedef struct {int64_t at;ev_kind kind;const char *text;void (*check)(void);bool fired;unsigned call;int http;} event_t;
static event_t script[1024];
static unsigned script_n;
static unsigned call_no;      /* connections made in this mic-on */
static int64_t base;          /* start of the connection being scripted */
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
static void (*task_create_hook)(const char *);
static void *task_main(void *p){
    int id=(int)(intptr_t)p;
    pthread_mutex_lock(&baton);while(running!=id)pthread_cond_wait(&turn,&baton);pthread_mutex_unlock(&baton);
    self=id;entities[id].fn(entities[id].arg);
    assert(!"task returned without vTaskDelete");return NULL;
}
static int xTaskCreatePinnedToCore(void (*fn)(void*),const char *name,unsigned stack,void *arg,unsigned prio,void *handle,int core){
    assert(prio==7&&core==0&&stack==4096&&!handle);
    assert(!strcmp(name,"pet_bridge_mic")||!strcmp(name,"pet_bridge_out"));
    (void)name;(void)stack;(void)prio;(void)handle;
    if(task_fail_at&&++task_attempts==task_fail_at)return 0;
    assert(entity_n<MAX_ENTITIES);int id=(int)entity_n++;
    entities[id]=(entity_t){.live=true,.wake=now,.fn=fn,.arg=arg};tasks_created++;
    assert(!pthread_create(&entities[id].thread,NULL,task_main,(void*)(intptr_t)id));
    if(task_create_hook)task_create_hook(name);
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
static bool parent_stop;
#ifdef BRIDGE_JOIN_REAL_PARENT
typedef struct {pthread_mutex_t mutex;} joined_lock_t;
#include "joined-parent-globals.inc"
static pthread_mutex_t join_sync=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t join_changed=PTHREAD_COND_INITIALIZER;
static atomic_int join_stage;
static atomic_bool join_done;
static int join_order;
static bool join_gate_checked,join_post_checked;
static void joined_before_status_take(void);
static void joined_after_status_take(void);
static void joined_before_status_give(void);
static void joined_before_guard_give(void);
static void joined_after_guard_give(void);
static SemaphoreHandle_t xSemaphoreCreateMutex(void){
    joined_lock_t *lock=calloc(1,sizeof(*lock));assert(lock);
    assert(!pthread_mutex_init(&lock->mutex,NULL));return lock;
}
static void (*guard_take_hook)(void),(*guard_give_hook)(void),(*parent_stop_hook)(void);
static bool hook_only_task;
static int xSemaphoreTake(SemaphoreHandle_t h,unsigned ticks){
    (void)ticks;assert(h);
    if(h==status_lock)joined_before_status_take();
    if(h!=status_lock)assert(!guard_held);
    assert(!pthread_mutex_lock(&((joined_lock_t*)h)->mutex));
    if(h==status_lock)joined_after_status_take();
    else guard_held=true;
    return pdTRUE;
}
static void xSemaphoreGive(SemaphoreHandle_t h){
    assert(h);
    if(h==status_lock)joined_before_status_give();
    else{assert(guard_held);joined_before_guard_give();guard_held=false;}
    assert(!pthread_mutex_unlock(&((joined_lock_t*)h)->mutex));
    if(h!=status_lock)joined_after_guard_give();
}
#define portMAX_DELAY 0U
void bridge_voice_stop(void);
#include "joined-parent-production.inc"
#else
static SemaphoreHandle_t xSemaphoreCreateMutex(void){return (void*)1;}
static void (*guard_take_hook)(void),(*guard_give_hook)(void),(*parent_stop_hook)(void);
static bool hook_only_task;
bool live_voice_bridge_audio_begin(void){assert(guard_held);return !parent_stop;}
void live_voice_bridge_audio_end(void){assert(guard_held);}
static int xSemaphoreTake(SemaphoreHandle_t h,unsigned ticks){
    (void)h;(void)ticks;assert(!guard_held);
    if(guard_take_hook&&(!hook_only_task||self!=0)){void (*hook)(void)=guard_take_hook;guard_take_hook=NULL;hook_only_task=false;hook();}
    if(fail_next_guard){fail_next_guard=false;return pdFALSE;}
    guard_held=true;return pdTRUE;
}
static void xSemaphoreGive(SemaphoreHandle_t h){
    (void)h;assert(guard_held);guard_held=false;
    if(guard_give_hook){void (*hook)(void)=guard_give_hook;guard_give_hook=NULL;hook();}
}
#endif
static QueueHandle_t xQueueCreate(unsigned n,size_t size){
    assert(size==sizeof(fixture_frame_t)&&n<=64);QueueHandle_t q=calloc(1,sizeof(*q));assert(q);q->cap=n;queues_made++;return q;
}
/* A loop that is routinely late, as on the device (display task at the same
 * priority, TLS work, base64): every owner iteration loses a further
 * late_min..late_max of time, drawn from a fixed sequence. owner_stall_* is
 * one single longer stop of the owner. Neither touches production code. */
static int64_t late_min_us,late_max_us,owner_stall_at,owner_stall_us;
static uint32_t late_seed;
static unsigned frames_high;
/* Ending-evidence regressions inject a lying capacity probe to exercise the
 * defensive short-enqueue failure even though ordinary conversation overflow
 * now drops. They do not claim a real FIFO overflow still ends a conversation. */
static bool force_capture_short;
static StreamBufferHandle_t fixture_mic;
static void queue_fault_stall(void){
    force_capture_short=true;owner_stall_at=now+3000000;owner_stall_us=2600000;
}
static uint64_t capture_positions[40000];
static unsigned capture_positions_n,capture_positions_sent,capture_position_offset;
static uint64_t fixture_capture_position;
static int xQueueSend(QueueHandle_t q,const void *p,unsigned ticks){
    /* Event scripts deliver callbacks synchronously, not on a reader task.
     * Saturation here stays full for the requested interval; focused runtime
     * tests separately interleave owner drain/Stop/failure with queue waits. */
    if(q->n==q->cap){now+=(int64_t)ticks*1000;return pdFALSE;}
    memcpy(&q->items[q->n++],p,sizeof(q->items[0]));
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
    if(!ticks&&fail_tail_guard){fail_tail_guard=false;fail_next_guard=true;}
    memcpy(out,&q->items[0],sizeof(q->items[0]));memmove(q->items,q->items+1,--q->n*sizeof(*q->items));return pdTRUE;
}
static unsigned uxQueueMessagesWaiting(QueueHandle_t q){return q->n;}
static void vQueueDelete(QueueHandle_t q){assert(!guard_held&&!q->n);queues_deleted++;free(q);}
static StreamBufferHandle_t xStreamBufferCreateStatic(size_t size,size_t trigger,unsigned char *memory,StaticStreamBuffer_t *meta){
    (void)trigger;(void)memory;(void)meta;StreamBufferHandle_t s=calloc(1,sizeof(*s));assert(s);
    s->cap=size-1;
    /* Production creates capture first on each call. The explicitly injected
     * short-write cases reproduce the original fault at the same instant;
     * every ordinary case uses the exact production allocation. */
    if(!fixture_mic){fixture_mic=s;if(force_capture_short)s->cap=96000;}
    s->data=malloc(s->cap);assert(s->data);streams_made++;return s;
}
static size_t xStreamBufferBytesAvailable(StreamBufferHandle_t s){return s->n;}
static size_t xStreamBufferSpacesAvailable(StreamBufferHandle_t s){
    if(force_capture_short&&s==fixture_mic&&s->n==s->cap)return 960; /* injected probe fault */
    return s->cap-s->n;
}
static size_t mic_high,mic_waiting; /* microphone audio captured but not yet sent */
static unsigned mic_short_writes;
static size_t xStreamBufferSend(StreamBufferHandle_t s,const void *p,size_t n,unsigned ticks){
    assert(!ticks);
    if(n>s->cap-s->n){ /* zero-wait partial write, as the RTOS */
        if(s==fixture_mic)mic_short_writes++;
        n=s->cap-s->n;
    }
    if(s==fixture_mic&&n){
        assert(n==960&&capture_positions_n<40000);
        if(capture_positions_n)assert(fixture_capture_position>capture_positions[capture_positions_n-1]);
        capture_positions[capture_positions_n++]=fixture_capture_position;
    }
    memcpy(s->data+s->n,p,n);s->n+=n;if(s->n>s->high)s->high=s->n;
    if(s==fixture_mic){mic_waiting=s->n;if(s->n>mic_high)mic_high=s->n;}
    return n;
}
static size_t xStreamBufferReceive(StreamBufferHandle_t s,void *p,size_t n,unsigned ticks){
    assert(!ticks);if(n>s->n)n=s->n;memcpy(p,s->data,n);memmove(s->data,s->data+n,s->n-n);s->n-=n;
    if(s==fixture_mic)mic_waiting=s->n;
    return n;
}
static void vStreamBufferDelete(StreamBufferHandle_t s){assert(!guard_held);if(s==fixture_mic)fixture_mic=NULL;streams_deleted++;free(s->data);free(s);}
static EventGroupHandle_t xEventGroupCreate(void){groups_made++;return calloc(1,sizeof(struct {unsigned bits;}));}
static unsigned xEventGroupSetBits(EventGroupHandle_t e,unsigned bits){e->bits|=bits;return e->bits;}
static unsigned xEventGroupClearBits(EventGroupHandle_t e,unsigned bits){e->bits&=~bits;return e->bits;}
static unsigned xEventGroupGetBits(EventGroupHandle_t e){return e->bits;}
static unsigned xEventGroupWaitBits(EventGroupHandle_t e,unsigned bits,int clear,int all,unsigned ticks){
    (void)clear;(void)all;if((e->bits&bits)!=bits)yield_until(now+(int64_t)ticks*1000);return e->bits;
}
static void vEventGroupDelete(EventGroupHandle_t e){assert(!guard_held);groups_deleted++;free(e);}
static void *heap_caps_malloc(size_t n,unsigned flags){
    (void)flags;if(fail_message_alloc){fail_message_alloc=false;return NULL;}
    if(delay_message_alloc){delay_message_alloc=false;now+=500000;}
    return malloc(n);
}
static void *heap_caps_calloc(size_t n,size_t size,unsigned flags){(void)flags;return calloc(n,size);}
static void setup_bridge_wipe(void *p,size_t n){volatile unsigned char *b=p;while(n--)*b++=0;}

/* ---- what the user sees ---- */
static struct {live_state_t state;char text[112];} history[256];
static unsigned history_n;
static live_state_t last_state;
static char last_text[112];
static void check_status_publication(live_state_t state);
void live_voice_bridge_status(live_state_t state,const char *text){
    check_status_publication(state);
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
static bool codec_open,open_fail,open_committed_before_wait;
static uint64_t mic_samples,wire_samples,open_samples;
static unsigned mic_call,open_failures;
static bool mark_phrase;
static unsigned marker_received[3];
static unsigned mic_stamp(unsigned call,uint64_t position){
    if(mark_phrase){
        /* Three distinct 200 ms PCM words, before/during/after a 2.6 s
         * owner stall. Test the bytes in the outgoing frame, not the queue's
         * idea of what should have arrived. Not a transcription test. */
        const unsigned at[]={24000,120000,168000};
        for(unsigned i=0;i<3;i++)
            if(position>=at[i]&&position<at[i]+4800)return 0x5801+i;
    }
    return 0x1000u+(call%4)*0x1000u+(unsigned)(position%0x1000);
}
static unsigned opens,closes,inhibits,mic_reads,mic_fail_at,mic_read_attempts,spk_writes,spk_fail_at;
static void (*audio_close_hook)(void);
static int64_t spk_busy,mic_off_at,first_audible_at;
static uint64_t played,played_audible,played_after_off;
static struct {int64_t from,to;} loud[4096];
static unsigned loud_n;
/* The amplifier latch of audio_test.c: once inhibited it cannot be enabled
 * until it is prepared again, and the codec cannot open without it. */
static bool amp_latched_off;
static unsigned prepares;
static void audio_stream_inhibit(void){inhibits++;amp_latched_off=true;}
static void audio_stream_prepare(void){prepares++;amp_latched_off=false;now+=retry_prepare_delay_us;}
static esp_err_t audio_stream_open(audio_session_t **out){
    opens++;*out=NULL;
    if(open_committed_before_wait){
        /* Success already passed amplifier admission; Stop can inhibit it
         * before this already-admitted open returns successfully. */
        assert(!codec_open&&!open_fail&&!amp_latched_off);
        codec_open=true;mic_call=call_no;open_samples=mic_samples;
        audio_session_t *opened=calloc(1,sizeof(*opened));assert(opened);
        yield_until(now+130000);
        *out=opened;return ESP_OK;
    }
    yield_until(now+130000); /* codec start: silent DMA prime + settle */
    if(open_fail||amp_latched_off){open_failures++;return ESP_FAIL;}
    assert(!codec_open);codec_open=true;mic_call=call_no;open_samples=mic_samples;
    *out=calloc(1,sizeof(**out));assert(*out);return ESP_OK;
}
static void audio_stream_close(audio_session_t *a){
    assert(codec_open&&!guard_held);
    if(audio_close_hook){void (*fn)(void)=audio_close_hook;audio_close_hook=NULL;fn();}
    codec_open=false;closes++;free(a);
}
static esp_err_t audio_stream_read(audio_session_t *a,void *p,unsigned n){
    assert(a&&codec_open&&n==960);
    mic_read_attempts++;
    int64_t from=now;yield_until(now+20000);
    if(mic_fail_at&&++mic_reads==mic_fail_at)return ESP_FAIL;
    /* Every sample carries the connection it was captured in and its position,
     * so the wire seam can prove nothing was dropped, repeated or reordered and
     * that nothing captured before a reconnect is ever sent after it.
     * 0x1000..0x4fff: the user speaking. 0x7000..0x77ff: this 20 ms also
     * contains the pet's own audible speaker output; production must never put
     * that on the wire. */
    bool echo=false;
    for(unsigned i=0;i<loud_n&&!echo;i++)echo=loud[i].from<now&&loud[i].to>from;
    unsigned char *bytes=p;
    for(unsigned i=0;i<n;i+=2,mic_samples++){
        unsigned v=echo?0x7000u+(unsigned)(mic_samples%0x800):mic_stamp(mic_call,mic_samples);
        bytes[i]=(unsigned char)v;bytes[i+1]=(unsigned char)(v>>8);
    }
    fixture_capture_position=mic_samples-480;
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
static bridge_transport_terminal_t during_dispose;
static bridge_transport_terminal_t tls_failure_result;
static const char *text_during_dispose;
/* One-shot fixture observers/injections at real owner I/O boundaries. They do
 * not call owner/model helpers or change production state. */
static void (*before_control_hook)(void),(*stop_send_hook)(void),(*dispose_hook)(void),(*register_hook)(void);
static void (*before_start_send_hook)(void);
static const char *fragment_message;
static bool finish_during_dispose,fragment_rfc6455,fragment_reply_on_stop,drop_fragment_completion,inject_before_control,reply_during_stop,short_stop;
static unsigned injected_pre_control,stop_replies,short_stops;
/* Additional send outcomes for cleanup-evidence precedence. The existing
 * short_stop/session.closed scenarios remain unchanged. */
enum {STOP_FULL,STOP_SHORT,STOP_FAILED,STOP_CANCELLED,STOP_CANCELLED_FULL};
static unsigned stop_fault,stop_faults,normal_close_frames;
static bool override_ticket_stamp;
static int64_t ticket_stamp;
static unsigned fragment_starts_before,fragment_starts_after,fragment_finishes_after,fragment_controls_after;
static void deliver(const char *json);
static void deliver_fragment(bool finish);
static void deliver_fragment_control(void);
static int64_t dispose_delay_us;
static unsigned dispose_delay_call;
static unsigned new_calls,start_calls,cancels,dispose_calls,send_fail_at,audio_sends,send_limit_high;
static int64_t send_wait_at,send_wait_us; /* once, from this time: the socket takes no microphone frame for this long */
static bool send_failed;
static void (*handler)(void*,esp_event_base_t,int32_t,void*);
static void *handler_arg;
static char sent[32][1400];               /* the current (last) connection's non-audio frames */
static unsigned sent_n,appends,appends_before_begin,call_appends,frames_total;
static uint64_t mic_sent,echo_bytes,zero_bytes,speech_bytes,call_mic_sent;
static unsigned max_calls;                /* connections this mic-on may make: 1 unless a case expects reconnects */
static int64_t call_at[64];               /* when each connection was started */
static void (*plans[32])(void),(*plan_rest)(void); /* what the host does on connection 1, 2, ...; then on any later one */
static bool began_sent;
static void call_done(void);
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
    /* A reconnect is a completely new call: everything of the previous one is
     * gone before this one exists (this call's own queue, buffers and event
     * group were just created), and its own time limit starts now. */
    assert(!codec_open&&tasks_created==tasks_exited&&closes==opens-open_failures);
    assert(queues_made==queues_deleted+1&&streams_made==streams_deleted+2&&groups_made==groups_deleted+1);
    if(call_no)call_done();
    assert(call_no<63);call_at[++call_no]=now;base=now;
    assert(deadline==now+SESSION_US);
    sent_n=0;call_appends=0;call_mic_sent=0;send_failed=false;began_sent=false;
    capture_positions_n=capture_positions_sent=capture_position_offset=0;
    transport=(bridge_transport_t){.deadline=deadline};session_deadline=deadline;transport_live=true;
    void (*plan)(void)=call_no<=32&&plans[call_no-1]?plans[call_no-1]:call_no>1||plans[0]?plan_rest:NULL;
    if(plan)plan();
    return &transport;
}
static int bridge_transport_http_status(const bridge_transport_t *t){return t?t->http:0;}
static bool bridge_transport_take_data(bridge_transport_t *t,bool *after,int64_t *at){
    assert(t&&after&&at);*after=t->local_control;*at=override_ticket_stamp?ticket_stamp:now;
    override_ticket_stamp=false;return true;
}
static int64_t bridge_transport_end_at(bridge_transport_t *t){(void)t;return 0;}
static bool bridge_transport_admit_control(bridge_transport_t *t,int64_t deadline){
    assert(t);if(t->cancelled||now>=deadline)return false;
    if(before_control_hook){void (*fn)(void)=before_control_hook;before_control_hook=NULL;fn();}
    t->local_control=true;return true;
}
static esp_websocket_client_handle_t bridge_transport_client(bridge_transport_t *t){return t;}
static bool bridge_transport_set_stop_check(bridge_transport_t *t,bool (*stopped)(void *),void *context){
    assert(t&&!t->started&&stopped);t->stop_check=stopped;t->stop_context=context;return true;
}
static esp_err_t esp_websocket_register_events(esp_websocket_client_handle_t ws,int id,
        void (*fn)(void*,esp_event_base_t,int32_t,void*),void *arg){
    assert(ws==&transport&&id==WEBSOCKET_EVENT_ANY);handler=fn;handler_arg=arg;
    if(register_hook){void (*hook)(void)=register_hook;register_hook=NULL;hook();}
    if(call_no>1)now+=registration_delay_us;
    return ESP_OK;
}
static esp_err_t bridge_transport_start_before(bridge_transport_t *t,int64_t start_by){
    if(t->stop_check&&t->stop_check(t->stop_context)){t->cancelled=true;return ESP_FAIL;}
    assert(t==&transport&&!t->started);t->started=true;
    if(start_by)now+=retry_start_delay_us;
    if(start_by&&now>start_by){t->cancelled=true;return ESP_FAIL;}
    start_calls++;return start_fail?ESP_FAIL:ESP_OK;
}
static bool bridge_transport_upgraded(bridge_transport_t *t){
    if(inject_before_control){
        inject_before_control=false;injected_pre_control++;deliver(CLOSED);
    }
    return t&&t->upgraded&&!t->cancelled;
}
static bool bridge_transport_finished(const bridge_transport_t *t){return t&&t->finished;}
static bool bridge_transport_started(const bridge_transport_t *t){return t&&t->started;}
static bool up_at_fence;
static void bridge_transport_cancel(bridge_transport_t *t){
    if(!t)return;
    if(!t->cancelled)up_at_fence=ws_up; /* was the host's side still open when the socket was fenced? */
    t->cancelled=true;cancels++;
}
/* The SDK-task exit proof has its own suite; its result is injected here. */
static bool bridge_transport_dispose_result(bridge_transport_t *t,int64_t until,bridge_transport_terminal_t *out){
    if(!t)return true;
    assert(t==&transport&&t->cancelled&&!guard_held);dispose_calls++;
    if(dispose_hook){void (*fn)(void)=dispose_hook;dispose_hook=NULL;fn();}
    if(text_during_dispose){deliver(text_during_dispose);text_during_dispose=NULL;}
    if(finish_during_dispose){
        finish_during_dispose=false;
        if(fragment_rfc6455)deliver_fragment_control();
        deliver_fragment(true);
    }
    handler=NULL;ws_up=false;
    if(dispose_delay_us&&call_no==dispose_delay_call){
        assert(now+dispose_delay_us<=until);yield_until(now+dispose_delay_us);
    }
    if(!dispose_ok)return false;
    if(during_dispose)t->terminal=during_dispose;
    *out=t->terminal;
    transport_live=false;return true;
}
static void later(int64_t us,ev_kind kind,const char *text){
    assert(script_n<1024);script[script_n++]=(event_t){.at=now+us,.kind=kind,.text=text,.call=call_no};
}
static int esp_websocket_client_send_text(esp_websocket_client_handle_t h,const char *s,size_t n,unsigned ticks){
    bool audio=!strncmp(s,APPEND,sizeof(APPEND)-1);
    /* Conversations use the measured 5 s limit independent of FIFO room. */
    assert(h==&transport&&!guard_held&&ticks&&ticks<=(audio?5000u:1000u)&&n==strlen(s));
    if(audio){
        assert(ticks==(unsigned)((session_deadline-now)/1000<5000?(session_deadline-now)/1000:5000));
        if(ticks>send_limit_high)send_limit_high=ticks;
    }
    assert(transport.started&&!transport.cancelled); /* nothing leaves after the fence */
    assert(!strstr(s,TOKEN));
    if(!strcmp(s,PET_LIVE_START)&&before_start_send_hook){
        void (*hook)(void)=before_start_send_hook;before_start_send_hook=NULL;hook();
    }
    assert(!strstr(s,"pet.turn.")&&!strstr(s,"send_confirm")); /* a conversation never confirms a send */
    if(!ws_up)return -1;
    if(!strncmp(s,APPEND,sizeof(APPEND)-1)){
        assert(!send_failed); /* an uncertain append is never followed by another: no replay */
        int64_t held=0;
        if(send_wait_at&&now>=send_wait_at){
            send_wait_at=0;held=send_wait_us;
            if(held>(int64_t)ticks*1000){
                /* As the pinned SDK does when a send times out: it closes the
                 * connection itself and reports it lost. How much of the frame
                 * left is unknown. */
                yield_until(now+(int64_t)ticks*1000);send_failed=true;
                if(handler&&ws_up){ws_up=false;handler(handler_arg,NULL,WEBSOCKET_EVENT_DISCONNECTED,NULL);}
                return -1;
            }
        }
        if(send_fail_at&&++audio_sends==send_fail_at){send_failed=true;return (int)n-1;}
        unsigned char pcm[4800];size_t bytes=0;const char *b64=s+sizeof(APPEND)-1,*end=strchr(b64,'"');
        assert(end&&!strcmp(end,"\"}"));
        assert(!mbedtls_base64_decode(pcm,sizeof(pcm),&bytes,(const unsigned char*)b64,(size_t)(end-b64)));
        assert(bytes&&bytes<=4800&&bytes%2==0);
        /* The first 4800 samples after THIS connection's codec opened are
         * discarded as stale; from there every sample captured in this
         * connection appears once, in order. A sample captured in an earlier
         * connection carries another stamp and would fail here. */
        assert(mic_call==call_no);
        if(!call_appends)assert(capture_positions[capture_positions_sent]==open_samples+4800);
        for(size_t i=0;i<bytes;i+=2,wire_samples++){
            assert(capture_positions_sent<capture_positions_n);
            wire_samples=capture_positions[capture_positions_sent]+capture_position_offset;
            unsigned v=pcm[i]|(unsigned)pcm[i+1]<<8;
            if(mark_phrase&&v>=0x5801&&v<=0x5803)marker_received[v-0x5801]++;
            if(!v)zero_bytes+=2;
            else if(v>=0x7000)echo_bytes+=2;
            else{
                if(v!=mic_stamp(call_no,wire_samples))
                    fprintf(stderr,"microphone sample %llu of connection %u out of sequence: got 0x%04x\n",
                        (unsigned long long)wire_samples,call_no,v);
                assert(v==mic_stamp(call_no,wire_samples));speech_bytes+=2;
            }
            if(++capture_position_offset==480){capture_position_offset=0;capture_positions_sent++;}
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
        /* Optional record of when each microphone message was handed to the
         * socket (microseconds of fixture time, PCM bytes): input for gateway
         * timing checks. Reads nothing back; no effect on any case. */
        if(getenv("CONVERSE_APPEND_TRACE")){
            FILE *trace=fopen(getenv("CONVERSE_APPEND_TRACE"),"a");
            if(trace){fprintf(trace,"%lld %zu\n",(long long)now,bytes);fclose(trace);}
        }
        mic_sent+=bytes;appends++;call_appends++;call_mic_sent+=bytes;
        if(sent_n<2)appends_before_begin++;
        if(held)yield_until(now+held); /* accepted, late: the frame above is delivered once */
    }else{
        /* Every connection speaks the same fixed sequence, and nothing else. */
        if(sent_n==0)assert(!strcmp(s,PET_LIVE_START));
        else if(sent_n==1)assert(!strcmp(s,G_BEGIN)||!strcmp(s,PET_STOP));
        else assert(!strcmp(s,PET_STOP)||!strcmp(s,SESSION_CLOSE));
        frames_total++;
        assert(sent_n<32&&n<sizeof(sent[0]));memcpy(sent[sent_n++],s,n+1);
        if(!strcmp(s,PET_STOP)&&stop_send_hook){void (*fn)(void)=stop_send_hook;stop_send_hook=NULL;fn();}
        if(!strcmp(s,PET_STOP)&&stop_fault!=STOP_FULL){
            unsigned fault=stop_fault;stop_fault=STOP_FULL;stop_faults++;
            yield_until(now+send_latency_us);
            if(fault==STOP_CANCELLED||fault==STOP_CANCELLED_FULL)transport.cancelled=true;
            return fault==STOP_CANCELLED_FULL?(int)n:fault==STOP_SHORT?(int)n-1:-1;
        }
        if(!strcmp(s,PET_STOP)&&(reply_during_stop||fragment_reply_on_stop)&&call_no==1){
            reply_during_stop=false;stop_replies++;
            if(fragment_reply_on_stop){
                fragment_reply_on_stop=false;fragment_message=CLOSED;deliver_fragment(false);
                if(fragment_rfc6455)deliver_fragment_control();
                finish_during_dispose=!drop_fragment_completion;
            }else deliver(CLOSED);
            if(short_stop){short_stops++;return (int)n-1;}
        }
        if(!strcmp(s,SESSION_CLOSE)&&host_closes){later(20000,EV_TEXT,CLOSED);later(25000,EV_WS_CLOSED,NULL);}
    }
    yield_until(now+send_latency_us);
    return (int)n;
}

#include "bridge-converse-production.inc"
_Static_assert(BV_MIC % BV_CHUNK == 0,"capture capacity must hold whole codec chunks");
static void check_status_publication(live_state_t state){
    /* A retrying NEW call may keep its connecting look; a call being released
     * must never promise a replacement before final-access proof and tail. */
    if(state==LIVE_RECONNECTING&&current)
        assert(current->reconnects&&!current->disposal_started&&!atomic_load(&current->failed));
}
#ifdef BRIDGE_JOIN_REAL_PARENT
/* Only RTOS mutex operations and scheduling barriers are synthetic. The owner
 * block, parent latch/helper/Stop, and bridge Stop are verbatim production. */
static void join_advance(int stage){
    assert(!pthread_mutex_lock(&join_sync));atomic_store(&join_stage,stage);
    assert(!pthread_cond_broadcast(&join_changed));assert(!pthread_mutex_unlock(&join_sync));
}
static void join_wait(int stage){
    struct timespec deadline;assert(!clock_gettime(CLOCK_REALTIME,&deadline));deadline.tv_sec+=5;
    assert(!pthread_mutex_lock(&join_sync));
    while(atomic_load(&join_stage)<stage)
        assert(!pthread_cond_timedwait(&join_changed,&join_sync,&deadline));
    assert(!pthread_mutex_unlock(&join_sync));
}
static void joined_before_status_take(void){
    if(!join_order||atomic_load(&join_stage)==0)return;
    if(self==-1&&join_order==1&&atomic_load(&join_stage)==1){
        assert(pthread_mutex_trylock(&((joined_lock_t*)status_lock)->mutex)==EBUSY);
        join_advance(2); /* actual status mutex held by owner */
    }else if(self==0&&join_order==2&&atomic_load(&join_stage)==2){
        assert(pthread_mutex_trylock(&((joined_lock_t*)status_lock)->mutex)==EBUSY);
        join_advance(3); /* actual status mutex held by Stop */
    }
}
static void joined_after_status_take(void){
    if(self==0&&join_order==1&&atomic_load(&join_stage)==0){
        /* Wake Stop only after the real parent mutex belongs to the owner. */
        assert(guard_held);join_advance(1);join_wait(2);
    }else if(self==-1&&join_order==2&&atomic_load(&join_stage)==1){
        join_advance(2);join_wait(3);
    }
}
static void joined_before_status_give(void){
    if(self==0&&join_order==1&&atomic_load(&join_stage)==2){
        assert(current&&guard_held&&!atomic_load(&stop_requested));
        fprintf(stderr,"joined-owner-before-unlock: capture=%u play=%u stop=%u\n",
                atomic_load(&current->capture_gate),atomic_load(&current->play_gate),
                atomic_load(&stop_requested));
        assert(atomic_load(&current->capture_gate)&&atomic_load(&current->play_gate));
    }else if(self==-1&&join_order==2&&atomic_load(&join_stage)==3){
        assert(atomic_load(&stop_requested));join_advance(4);
    }
}
static void joined_before_guard_give(void){
    if(self==0&&join_order==2&&!join_gate_checked&&atomic_load(&join_stage)>=4){
        /* Observe the admission decision before Stop can take the guard and
         * mask an incorrect transient opening by closing the gates itself. */
        assert(current&&guard_held&&atomic_load(&stop_requested));
        fprintf(stderr,"joined-stop-before-unlock: capture=%u play=%u stop=%u\n",
                atomic_load(&current->capture_gate),atomic_load(&current->play_gate),
                atomic_load(&stop_requested));
        assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
        join_gate_checked=true;
    }
}
static void joined_after_guard_give(void){
    if(self!=0||!join_order||join_post_checked)return;
    if((join_order==1&&atomic_load(&join_stage)>=2)||
       (join_order==2&&atomic_load(&join_stage)>=4)){
        if(join_order==2)assert(join_gate_checked);
        join_wait(5);assert(atomic_load(&join_done));
        assert(current&&atomic_load(&current->stop)&&atomic_load(&current->muted));
        assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate)&&amp_latched_off);
        join_post_checked=true;
    }
}
static void *joined_stop_actor(void *unused){
    (void)unused;self=-1;join_wait(1);live_voice_stop();
    atomic_store(&join_done,true);join_advance(5);return NULL;
}
static void arm_joined_stop(const char *name){
    if(strcmp(name,"pet_bridge_out"))return;
    task_create_hook=NULL;
    assert(current&&guard!=status_lock&&atomic_load(&current->active));
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    if(join_order==2){join_advance(1);join_wait(2);}
}
#endif

/* ---- script ---- */
static setup_bridge_t cfg;
static int64_t run_start,run_end;
static bool parent_stopped(void){
    bool value=parent_stop;
    if(parent_stop_hook){void (*hook)(void)=parent_stop_hook;parent_stop_hook=NULL;hook();}
    return value;
}
static void deliver(const char *json){
    if(!handler||!ws_up)return;
    int n=(int)strlen(json);
    esp_websocket_event_data_t d={.data_len=n,.payload_len=n,.op_code=1,.fin=true,.data_ptr=json};
    handler(handler_arg,NULL,WEBSOCKET_EVENT_DATA,&d);
}
static void deliver_fragment(bool finish){
    assert(handler&&fragment_message&&current);
    bool admitted=transport.local_control;
    if(!finish){if(admitted)fragment_starts_after++;else fragment_starts_before++;}
    else if(admitted)fragment_finishes_after++;
    int n=(int)strlen(fragment_message),split=n/2;
    int length=finish?n-split:split;
    esp_websocket_event_data_t d={.data_len=length,.payload_len=fragment_rfc6455?length:n,
        .payload_offset=fragment_rfc6455?0:finish?split:0,
        .op_code=fragment_rfc6455&&finish?0:1,.fin=fragment_rfc6455?finish:true,
        .data_ptr=fragment_message+(finish?split:0)};
    handler(handler_arg,NULL,WEBSOCKET_EVENT_DATA,&d);
}
static void deliver_fragment_control(void){
    assert(handler&&current&&current->assembly.active);
    if(transport.local_control)fragment_controls_after++;
    static const char ping[]="x";
    esp_websocket_event_data_t d={.data_len=1,.payload_len=1,.op_code=9,.fin=true,.data_ptr=ping};
    handler(handler_arg,NULL,WEBSOCKET_EVENT_DATA,&d);
}
static void fire(event_t *e){
    if(e->kind!=EV_MIC_OFF&&e->kind!=EV_PARENT_STOP&&e->kind!=EV_CHECK&&e->call!=call_no)return;
    switch(e->kind){
    case EV_CONNECT:
        if(handler&&!transport.cancelled){transport.upgraded=true;transport.http=101;ws_up=true;handler(handler_arg,NULL,WEBSOCKET_EVENT_CONNECTED,NULL);}
        break;
    case EV_TEXT:if(handler&&ws_up&&!strcmp(e->text,G_BEGAN))began_sent=true;deliver(e->text);break;
    case EV_PARTIAL_TEXT:
        if(handler&&ws_up){
            fragment_message=e->text;deliver_fragment(false);
            if(fragment_rfc6455)deliver_fragment_control();
        }
        break;
    case EV_HTTP:transport.http=e->http;transport.finished=true;break; /* the host answered, and not with 101 */
    case EV_WS_CLOSED:if(handler&&ws_up){ws_up=false;handler(handler_arg,NULL,WEBSOCKET_EVENT_CLOSED,NULL);}break;
    case EV_WS_DROP:if(handler&&ws_up){ws_up=false;handler(handler_arg,NULL,WEBSOCKET_EVENT_DISCONNECTED,NULL);}break;
    case EV_WS_ERROR:
        if(handler&&ws_up){
            handler(handler_arg,NULL,WEBSOCKET_EVENT_ERROR,NULL);ws_up=false;
            handler(handler_arg,NULL,WEBSOCKET_EVENT_DISCONNECTED,NULL);
        }
        break;
    case EV_TLS_FAILURE:
        /* Synthetic settled result at the transport boundary, not actual TLS.
         * The pinned SDK event ordering and error copy have separate tests. */
        assert(handler&&!ws_up&&!transport.upgraded&&transport.http==0);
        transport.terminal=tls_failure_result;
        if(!e->text){
            handler(handler_arg,NULL,WEBSOCKET_EVENT_ERROR,NULL);
            handler(handler_arg,NULL,WEBSOCKET_EVENT_DISCONNECTED,NULL);
        }
        transport.finished=true;
        break;
    case EV_RAW_CLOSE:
        if(handler&&ws_up){
            esp_websocket_event_data_t d={.data_len=0,.payload_len=0,.payload_offset=0,.op_code=8,.fin=true,.data_ptr=NULL};
            handler(handler_arg,NULL,WEBSOCKET_EVENT_DATA,&d);
        }
        break;
    case EV_CLOSE_1011:case EV_CLOSE_1000:
        if(handler&&ws_up){
            const char code[]={3,e->kind==EV_CLOSE_1000?(char)0xe8:(char)0xf3};
            /* Mirror the real parser's complete-frame classification regardless
             * of admission; pinned receive cases independently check it. */
            if(e->kind==EV_CLOSE_1000){
                normal_close_frames++;
                if(transport.terminal==BR_TRANSPORT_NONE)transport.terminal=BR_TRANSPORT_PEER_NORMAL;
            }
            esp_websocket_event_data_t d={.data_len=2,.payload_len=2,.payload_offset=0,.op_code=8,.fin=true,.data_ptr=code};
            handler(handler_arg,NULL,WEBSOCKET_EVENT_DATA,&d);
        }
        break;
    case EV_PROTOCOL_CLOSE:case EV_POLICY_CLOSE:
        if(handler&&ws_up){
            transport.terminal=e->kind==EV_PROTOCOL_CLOSE?BR_TRANSPORT_PROTOCOL:BR_TRANSPORT_POLICY;
            ws_up=false;handler(handler_arg,NULL,WEBSOCKET_EVENT_DISCONNECTED,NULL);
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
    if(!handler||!ws_up||!began_sent)return; /* the host streams audio only inside a begun conversation */
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
/* Times are milliseconds from the start of the connection being scripted:
 * the first one before call(), a later one from inside its plan. */
static void at(int64_t ms,ev_kind kind,const char *text){
    assert(script_n<1024);script[script_n++]=(event_t){.at=base+ms*1000,.kind=kind,.text=text,.call=call_no?call_no:1};
}
static void http_at(int64_t ms,int status){
    assert(script_n<1024);script[script_n++]=(event_t){.at=base+ms*1000,.kind=EV_HTTP,.http=status,.call=call_no?call_no:1};
}
static void check(int64_t ms,void (*fn)(void)){
    assert(script_n<1024);script[script_n++]=(event_t){.at=base+ms*1000,.kind=EV_CHECK,.check=fn};
}
static void host_audio(int64_t from_ms){host_next=base+from_ms*1000;}
static void says(int64_t from_ms,int64_t to_ms){
    assert(speech_n<32);speech[speech_n].from=base+from_ms*1000;speech[speech_n++].to=base+to_ms*1000;
}
/* Fixture state only. Production statics are NEVER reset between calls: every
 * later call in a process is a conversation started after an earlier one ended. */
static void fresh(void){
    mark_phrase=false;memset(marker_received,0,sizeof(marker_received));
    script_n=0;host_next=0;speech_n=0;omit_silence=false;stall_from=stall_to=0;stalled=0;host_audible=host_silent=0;
    parent_stop=false;mic_off_at=0;first_audible_at=0;played=played_audible=played_after_off=0;loud_n=0;spk_busy=0;
    fail_next_guard=fail_tail_guard=fail_message_alloc=delay_message_alloc=false;
    task_create_hook=NULL;guard_take_hook=guard_give_hook=parent_stop_hook=NULL;hook_only_task=false;
    open_fail=open_committed_before_wait=false;mic_reads=mic_fail_at=mic_read_attempts=spk_writes=spk_fail_at=0;opens=closes=inhibits=0;open_failures=0;
    audio_close_hook=NULL;
    call_no=0;max_calls=1;frames_total=0;call_appends=0;call_mic_sent=0;began_sent=false;plan_rest=NULL;
    memset(plans,0,sizeof(plans));memset(call_at,0,sizeof(call_at));mic_call=0;open_samples=0;
    tasks_created=tasks_exited=task_fail_at=task_attempts=0;
    new_fail=start_fail=token_fail=false;dispose_ok=true;host_closes=true;during_dispose=BR_TRANSPORT_NONE;tls_failure_result=BR_TRANSPORT_NONE;text_during_dispose=NULL;
    before_control_hook=stop_send_hook=dispose_hook=register_hook=before_start_send_hook=NULL;
    fragment_message=NULL;finish_during_dispose=fragment_rfc6455=fragment_reply_on_stop=drop_fragment_completion=inject_before_control=reply_during_stop=short_stop=false;
    injected_pre_control=stop_replies=short_stops=0;
    stop_fault=STOP_FULL;stop_faults=normal_close_frames=0;override_ticket_stamp=false;ticket_stamp=0;
    fragment_starts_before=fragment_starts_after=fragment_finishes_after=fragment_controls_after=0;
    dispose_delay_us=0;dispose_delay_call=0;
    retry_prepare_delay_us=registration_delay_us=retry_start_delay_us=0;
    new_calls=start_calls=cancels=dispose_calls=send_fail_at=audio_sends=send_limit_high=0;send_failed=false;up_at_fence=false;
    send_wait_at=send_wait_us=0;
    sent_n=appends=appends_before_begin=0;mic_sent=echo_bytes=zero_bytes=speech_bytes=0;send_latency_us=2000;
    mic_samples=wire_samples=0;mic_high=mic_waiting=0;mic_short_writes=0;frames_high=0;
    force_capture_short=false;capture_positions_n=capture_positions_sent=capture_position_offset=0;
    late_min_us=late_max_us=owner_stall_at=owner_stall_us=0;late_seed=20261115u;
    first_append_at=last_append_at=max_append_gap=rate_floor=0;rate_floor_set=false;peak_second=0;window_from=0;
    queues_made=queues_deleted=streams_made=streams_deleted=groups_made=groups_deleted=0;
    history_n=0;last_text[0]=0;now+=3000000;run_start=now;base=now;
    prepares=0;amp_latched_off=false; /* live_voice_start prepares the amplifier for the accepted tap */
}
static void opened(void){ /* the design's sequence up to "listening" */
    at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);at(80,EV_TEXT,G_BEGAN);at(300,EV_TEXT,G_STATE("listening"));
}
/* Every sample this connection captured and was able to send reached the wire. */
static void call_done(void){
    if(call_appends){
        assert(call_mic_sent/2==(uint64_t)capture_positions_sent*480+capture_position_offset);
        assert(wire_samples<=mic_samples); /* forward gaps allowed; never replay/reorder */
    }
}
/* One tap on the microphone button with a bridge configured: one mic-on. */
static void call(bool retained){
    entities[0].live=true;
#ifdef BRIDGE_JOIN_REAL_PARENT
    bridge_voice_run(&cfg,now+SESSION_US,SESSION_US,bridge_session_stop);
#else
    bridge_voice_run(&cfg,now+SESSION_US,SESSION_US,parent_stopped);
#endif
    run_end=now;if(call_no)call_done();
    for(unsigned i=1;i<entity_n;i++)if(!entities[i].joined){
        assert(!entities[i].live);assert(!pthread_join(entities[i].thread,NULL));entities[i].joined=true;
    }
    assert(!guard_held&&tasks_created==tasks_exited);
    /* One connection per tap unless this case expects the pet to reconnect. */
    if(new_calls>max_calls)fprintf(stderr,"%u connections were made, at most %u expected\n",new_calls,max_calls);
    assert(new_calls<=max_calls&&start_calls<=max_calls&&new_calls<=21);
    /* The amplifier is re-armed only for a call that is actually made, and is
     * left latched off whenever a mic-on ends after audio was used. */
    assert(prepares==(new_calls?new_calls-1:0)||prepares==new_calls);
    if(opens>open_failures)assert(amp_latched_off);
    assert(echo_bytes==0);                      /* the pet's own voice is never sent as the user's */
    assert(mic_sent==echo_bytes+zero_bytes+speech_bytes);
    assert(appends_before_begin==0);            /* microphone audio only after pet.converse.begin */
    for(unsigned i=0;i<history_n;i++)assert(!strstr(history[i].text,TOKEN));
    if(retained)return;
    /* Idle again and reusable: everything released, nothing held for a reboot. */
    assert(current==NULL&&!bridge_voice_restart_required()&&!codec_open&&!transport_live);
    assert(closes==opens-open_failures);
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
    assert(current&&current->model.phase==BR_CONVERSING&&codec_open&&tasks_created-tasks_exited==2);
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
static void tap_during_registration(void){
    event_t tap={.kind=EV_MIC_OFF};
    fire(&tap);
}
static void stop_during_registration(void){
    /* Whole production owner: the parent's Stop and bridge Stop win while
     * registration is in progress, before any transport start admission. */
    fresh();register_hook=tap_during_registration;call(false);
    fprintf(stderr,"registration-stop: start_calls=%u frames=%u opens=%u mic_reads=%u\n",
            start_calls,frames_total,opens,mic_read_attempts);
    ended(LIVE_OFF,"Mic off");
    assert(start_calls==0&&frames_total==0&&opens==0&&mic_read_attempts==0);
}
static void plan_stop_during_registration(void){register_hook=tap_during_registration;}
static void reconnect_stop_during_registration(void){
    fresh();opened();at(1000,EV_WS_DROP,NULL);
    max_calls=2;plans[1]=plan_stop_during_registration;call(false);
    ended(LIVE_OFF,"Mic off");
    assert(new_calls==2&&start_calls==1&&opens==1&&closes==1);
    assert(!call_report.again&&!transport_live);
}
static void tap_after_publication(void){
    assert(current&&!atomic_load(&current->active));
    tap_during_registration();
}
static void stop_before_active(void){
    fresh();guard_give_hook=tap_after_publication;call(false);
    ended(LIVE_OFF,"Mic off");
    assert(start_calls==0&&frames_total==0&&opens==0&&mic_read_attempts==0);
}
static bool gate_after_stop;
static void inspect_gate_after_stop(void){
    assert(current&&(parent_stop||now>=current->work));
    gate_after_stop=atomic_load(&current->capture_gate);
}
static void stop_before_gate_arm(const char *name){
    if(strcmp(name,"pet_bridge_out"))return;
    task_create_hook=NULL;tap_during_registration();
    guard_take_hook=inspect_gate_after_stop;
}
static void latch_parent_at_gate(void){
    assert(current&&guard_held&&!atomic_load(&current->stop));
    mic_off_at=now;parent_stop=true;guard_give_hook=inspect_gate_after_stop;
}
static void parent_stop_during_gate_arm(const char *name){
    if(strcmp(name,"pet_bridge_out"))return;
    task_create_hook=NULL;parent_stop_hook=latch_parent_at_gate;
}
static void parent_stop_during_audio_arm(void){
    fresh();opened();task_create_hook=parent_stop_during_gate_arm;gate_after_stop=false;
    call(false);
    fprintf(stderr,"parent-stop-gate: gate_rearmed=%u read_attempts=%u\n",
            gate_after_stop,mic_read_attempts);
    ended(LIVE_OFF,"Mic off");
    assert(!gate_after_stop&&mic_read_attempts==0);
}
static void stop_during_audio_start(void){
    fresh();opened();task_create_hook=stop_before_gate_arm;gate_after_stop=false;
    call(false);
    fprintf(stderr,"audio-stop: gate_rearmed=%u reads=%u appends=%u\n",
            gate_after_stop,mic_read_attempts,appends);
    ended(LIVE_OFF,"Mic off");
    assert(!gate_after_stop&&mic_read_attempts==0&&appends==0);
}
static void stop_after_start_send_admission(void){
    fresh();at(40,EV_CONNECT,NULL);before_start_send_hook=tap_during_registration;
    call(false);stopped_cleanly(1);
    /* The start send was already admitted before the tap. Its fake transport
     * settles once afterwards; no codec opens and no second start is sent. */
    assert(!before_start_send_hook&&frames_total==3&&opens==0&&mic_read_attempts==0&&appends==0);
    printf("start-send: one pre-admitted start settled after Stop, no codec/read\n");
}
static unsigned reads_at_guard_stop;
static void stop_before_capture_guard(void){
    reads_at_guard_stop=mic_read_attempts;tap_during_registration();
}
static void arm_capture_guard_stop(void){
    fprintf(stderr,"capture-guard precondition: current=%d gate=%d reads=%u now=%lld base=%lld\n",
            current!=NULL,current?(int)atomic_load(&current->capture_gate):-1,mic_read_attempts,
            (long long)now,(long long)base);
    assert(current&&atomic_load(&current->capture_gate)&&mic_read_attempts);
    guard_take_hook=stop_before_capture_guard;hook_only_task=true;
}
static void stop_while_capture_waits_guard(void){
    fresh();opened();host_audio(100);check(500,arm_capture_guard_stop);
    call(false);stopped_cleanly(2);
    assert(!guard_take_hook&&reads_at_guard_stop&&mic_read_attempts==reads_at_guard_stop);
    printf("capture-guard: reads_before=%u reads_after=%u\n",reads_at_guard_stop,mic_read_attempts);
}
static void guard_timeout_stop(void){
    assert(current&&codec_open);parent_stop=true;mic_off_at=now;fail_next_guard=true;
    unsigned previous_inhibits=inhibits;bridge_voice_stop();
    fprintf(stderr,"guard-timeout-stop: inhibited_now=%u parent_latched=%u\n",
            inhibits>previous_inhibits,parent_stop);
    assert(inhibits>previous_inhibits);
}
static void stop_guard_timeout(void){
    fresh();opened();host_audio(100);check(500,guard_timeout_stop);call(false);
    ended(LIVE_OFF,"Mic off");
}
static void expire_at_audio_gate(void){
    assert(current);now=current->work;guard_give_hook=inspect_gate_after_stop;
}
static void expire_during_audio_arm(const char *name){
    if(strcmp(name,"pet_bridge_out"))return;
    task_create_hook=NULL;parent_stop_hook=expire_at_audio_gate;
}
static void stop_audio_work_deadline(void){
    fresh();opened();task_create_hook=expire_during_audio_arm;gate_after_stop=false;call(false);
    fprintf(stderr,"deadline-audio-gate: gate_rearmed=%u reads=%u\n",gate_after_stop,mic_read_attempts);
    assert(!gate_after_stop&&!mic_read_attempts&&!appends);
}
static void expire_before_codec_open(void){
    assert(current&&!guard_held);
    now=current->work;
}
static void schedule_expiry_at_capture_action(void){
    assert(current&&!atomic_load(&current->capture_gate));
    parent_stop_hook=expire_before_codec_open;
}
static void stop_codec_work_deadline(void){
    fresh();opened();check(79,schedule_expiry_at_capture_action);call(false);
    fprintf(stderr,"deadline-codec: hook_pending=%u opens=%u reads=%u\n",
            parent_stop_hook!=NULL,opens,mic_read_attempts);
    assert(!parent_stop_hook&&!opens&&!mic_read_attempts);
}
static bool inflight_open_boundary_seen,closed_before_audio_tasks;
static void inspect_inflight_open(void){
    assert(current&&codec_open&&!current->audio&&!tasks_created);
    inflight_open_boundary_seen=true;
}
static void expire_during_inflight_open(void){
    inspect_inflight_open();now=current->work;
}
static void audit_inflight_open_release(void){
    assert(current&&codec_open&&current->audio);
    closed_before_audio_tasks=!tasks_created&&!mic_read_attempts&&!appends&&
        !atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate);
}
static void successful_inflight_open(const char *name){
    bool cutoff=!strcmp(name,"open-success-cutoff");
    bool stopped=!strcmp(name,"open-success-stop");
    assert(cutoff||stopped||!strcmp(name,"open-success-control"));
    fresh();opened();open_committed_before_wait=true;
    inflight_open_boundary_seen=closed_before_audio_tasks=false;
    if(stopped){check(150,inspect_inflight_open);at(150,EV_MIC_OFF,NULL);}
    else if(cutoff)check(150,expire_during_inflight_open);
    else {check(150,inspect_inflight_open);at(1000,EV_MIC_OFF,NULL);}
    if(stopped||cutoff)audio_close_hook=audit_inflight_open_release;
    call(false);
    assert(inflight_open_boundary_seen&&opens==1&&!open_failures&&closes==1);
    if(stopped||cutoff){
        fprintf(stderr,"open-crossed-%s: opens=%u closes=%u audio_tasks=%u mic_reads=%u appends=%u gates_closed_at_release=%u\n",
            cutoff?"cutoff":"stop",opens,closes,tasks_created,mic_read_attempts,appends,closed_before_audio_tasks);
        assert(closed_before_audio_tasks&&!tasks_created&&!mic_read_attempts&&!appends);
        ended(LIVE_OFF,cutoff?"Mic off (time limit)":"Mic off");
    }else{
        assert(tasks_created==2&&mic_read_attempts>0);
        stopped_cleanly(2);
    }
}
#ifdef BRIDGE_JOIN_REAL_PARENT
static void joined_gate_order(const char *name){
    fresh();opened();join_order=!strcmp(name,"joined-gate-owner-wins")?1:2;
    assert(join_order==1||!strcmp(name,"joined-gate-stop-wins"));
    atomic_store(&stop_requested,false);session_deadline_us=0;
    atomic_store(&join_stage,0);atomic_store(&join_done,false);
    join_gate_checked=false;join_post_checked=false;
    task_create_hook=arm_joined_stop;
    pthread_t stopper;assert(!pthread_create(&stopper,NULL,joined_stop_actor,NULL));
    call(false);assert(!pthread_join(stopper,NULL));
    assert(atomic_load(&join_done)&&atomic_load(&stop_requested));
    assert(join_post_checked&&(join_order==1||join_gate_checked));
    assert(tasks_created==2&&!mic_read_attempts&&!appends);
    ended(LIVE_OFF,"Mic off");join_order=0;
    printf("joined-status-gate: order=%s real_parent_and_owner=yes closed_after_stop=yes\n",name);
}
#endif
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
        /* Mic off while the codec was starting latched the amplifier off, so
         * that start fails, as on the device: nothing was opened or captured. */
        assert(opens==1&&open_failures==1&&!closes&&!codec_open&&!tasks_created&&!appends&&!played);
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
    /* Ordinary ends announced by the host: mic off, and never a reconnect. */
    fresh();opened();host_audio(100);
    if(!strcmp(name,"host-quiet")||!strcmp(name,"host-time-limit")){
        bool quiet_=!strcmp(name,"host-quiet");
        at(2000,EV_TEXT,quiet_?G_NOTICE("idle_timeout"):G_NOTICE("time_limit"));
        at(2600,EV_TEXT,CLOSED);at(2610,EV_WS_CLOSED,NULL);
        call(false);ended(LIVE_OFF,quiet_?"Mic off (quiet)":"Mic off (time limit)");
    }else{
        /* Asking, a quiet notice, close and failure share one tick. The host
         * ending does not settle the already-admitted, observed pending send. */
        at(2000,EV_TEXT,G_STATE("asking"));at(2000,EV_TEXT,G_NOTICE("idle_timeout"));
        at(2000,EV_TEXT,CLOSED);at(2000,EV_WS_ERROR,NULL);
        call(false);ended(LIVE_ERROR,PENDING_TEXT);
    }
    assert(new_calls==1&&!shown("Reconnecting..."));
    assert(opens==1&&closes==1&&tasks_created==2&&run_end-run_start<5000000);
}
static void host_end_race(const char *name){
    /* Exercise the real owner/cleanup boundary, including callbacks during the
     * close linger. A host notice must not make a definite error look normal. */
    fresh();host_closes=false;
    const char *expect="Mic off (quiet)";live_state_t state=LIVE_OFF;
    if(!strcmp(name,"notice-queue")){
        opened();queue_fault_stall();
        at(5300,EV_TEXT,G_NOTICE("idle_timeout"));
    }else if(!strcmp(name,"notice-linger")){
        opened();queue_fault_stall();
        at(5700,EV_TEXT,G_NOTICE("time_limit"));at(5750,EV_WS_CLOSED,NULL);
        expect="Mic off (time limit)";
    }else if(!strcmp(name,"notice-begin")){
        at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);
        at(30100,EV_TEXT,G_NOTICE("idle_timeout"));at(30150,EV_WS_CLOSED,NULL);
    }else if(!strcmp(name,"notice-user")){
        opened();queue_fault_stall();
        at(5700,EV_TEXT,G_NOTICE("idle_timeout"));at(5750,EV_MIC_OFF,NULL);at(5800,EV_WS_CLOSED,NULL);
        expect="Mic off";
    }else{
        assert(!strcmp(name,"notice-fatal"));
        opened();mic_fail_at=60;
        at(1500,EV_TEXT,G_NOTICE("idle_timeout"));at(1550,EV_WS_CLOSED,NULL);
        expect="Microphone read failed.";state=LIVE_ERROR;
    }
    call(false);ended(state,expect);
    assert(new_calls==1&&dispose_calls==1&&current==NULL&&!transport_live);
}
static void plan_again(void);
static void fail_tail_lock(void){fail_tail_guard=true;}
static void fail_owner_lock(void){fail_next_guard=true;}
static void fail_message_allocation(void){fail_message_alloc=true;}
static void reviewed_races(void){
    /* Successful upgrade and immediate drop before owner observes connected:
     * HTTP 101 was not a refusal and must remain retryable. */
    fresh();host_audio(100);at(40,EV_CONNECT,NULL);at(40,EV_WS_DROP,NULL);
    max_calls=2;plans[1]=plan_again;call(false);stopped_cleanly(2);assert(new_calls==2);
    /* Permanent host error after queue overflow remains fatal even if followed
     * by a transient error and a normal-end notice during linger. */
    fresh();opened();host_closes=false;queue_fault_stall();
    at(5700,EV_TEXT,"{\"type\":\"pet.error\",\"code\":\"model_mismatch\"}");
    at(5710,EV_TEXT,"{\"type\":\"pet.error\",\"code\":\"busy\"}");
    at(5720,EV_TEXT,G_NOTICE("idle_timeout"));at(5750,EV_WS_CLOSED,NULL);
    call(false);ended(LIVE_ERROR,"Host error: model_mismatch");assert(new_calls==1);
    /* An unprocessed tail frame on guard failure never grants a retry. */
    fresh();opened();host_closes=false;queue_fault_stall();
    at(5700,EV_TEXT,G_NOTICE("idle_timeout"));check(5710,fail_tail_lock);at(5750,EV_WS_CLOSED,NULL);
    call(false);ended(LIVE_ERROR,"Voice connection interrupted.");assert(new_calls==1);
    /* The owner cannot lose an already-dequeued notice on a lock failure and
     * then classify a concurrent callback failure as retryable. */
    fresh();opened();check(2000,fail_owner_lock);at(2000,EV_TEXT,G_NOTICE("idle_timeout"));
    call(false);ended(LIVE_ERROR,"Voice connection interrupted.");assert(new_calls==1);
    /* A complete frame whose allocation fails, or cannot fit the queue, may
     * contain a terminal ending. Unknown ingress is not permission to retry. */
    fresh();opened();check(2000,fail_message_allocation);at(2000,EV_TEXT,G_NOTICE("idle_timeout"));
    call(false);ended(LIVE_ERROR,"Voice connection interrupted.");assert(new_calls==1);
    fresh();opened();for(unsigned i=0;i<32;i++)at(2000,EV_TEXT,G_STATE("asking"));
    at(2000,EV_TEXT,G_NOTICE("time_limit"));
    call(false);ended(LIVE_ERROR,"Voice connection interrupted.");assert(new_calls==1);
    /* A previously admitted SDK DATA callback can arrive after cancellation
     * but before the transport task's final-access proof. */
    fresh();opened();host_closes=false;queue_fault_stall();
    text_during_dispose=G_NOTICE("idle_timeout");
    call(false);ended(LIVE_OFF,"Mic off (quiet)");assert(new_calls==1);
}
static void terminal_wire(void){
    /* Covers pre-session, during conversation and publication only after
     * disposal. Neither terminal status may result in another connection. */
    for(unsigned kind=0;kind<2;kind++)for(unsigned stage=0;stage<3;stage++){
        fresh();host_closes=false;
        if(stage==0){at(40,EV_CONNECT,NULL);at(40,kind?EV_POLICY_CLOSE:EV_PROTOCOL_CLOSE,NULL);}
        else if(stage==1){at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);at(80,kind?EV_POLICY_CLOSE:EV_PROTOCOL_CLOSE,NULL);}
        else{opened();at(2000,kind?EV_POLICY_CLOSE:EV_PROTOCOL_CLOSE,NULL);}
        call(false);
        ended(LIVE_ERROR,kind?"Host refused the voice connection.":"Voice connection protocol error.");
        /* The interim status is provisional until dispose proves the terminal
         * result; only the final connection count determines a retry. */
        assert(new_calls==1&&dispose_calls==1);
    }
    /* A terminal result published while the vendor task is being joined must
     * still beat the earlier generic disconnect. */
    fresh();opened();at(2000,EV_WS_DROP,NULL);during_dispose=BR_TRANSPORT_POLICY;
    call(false);ended(LIVE_ERROR,"Host refused the voice connection.");assert(new_calls==1);
    /* Explicit mute is never relabelled by the terminal close. */
    fresh();opened();at(2000,EV_MIC_OFF,NULL);during_dispose=BR_TRANSPORT_PROTOCOL;
    call(false);ended(LIVE_OFF,"Mic off");assert(new_calls==1);
    /* Injected settled wire results check owner precedence, NOT observed peer
     * frames or cause. Actual SDK status parsing is checked by receive cases. */
    fresh();opened();at(2000,EV_WS_DROP,NULL);during_dispose=BR_TRANSPORT_PEER_NORMAL;
    call(false);ended(LIVE_OFF,"Mic off (call ended)");assert(new_calls==1);
    fresh();opened();at(2000,EV_WS_DROP,NULL);during_dispose=BR_TRANSPORT_UNSUPPORTED;
    call(false);ended(LIVE_ERROR,"Host rejected the voice format.");assert(new_calls==1);
    fresh();opened();at(2000,EV_WS_DROP,NULL);during_dispose=BR_TRANSPORT_PAYLOAD_REJECTED;
    call(false);ended(LIVE_ERROR,"Host rejected the voice payload.");assert(new_calls==1);
}
static void malformed_tail(void){
    /* Invalid JSON queued during release is still terminal, not a reconnect. */
    fresh();opened();host_closes=false;queue_fault_stall();
    at(5700,EV_TEXT,"not json");at(5750,EV_WS_CLOSED,NULL);
    call(false);ended(LIVE_ERROR,"Host sent an invalid message.");assert(new_calls==1);
}
static void sdk_event_loss(void){
    /* This seam receives the production transport's settled uncertainty
     * result; pinned SDK fault behavior is tested in the receive fixture. */
    fresh();opened();host_audio(100);at(2000,EV_WS_DROP,NULL);during_dispose=BR_TRANSPORT_EVENT_LOSS;
    call(false);ended(LIVE_ERROR,"Voice connection interrupted.");assert(new_calls==1&&dispose_calls==1);
    fresh();opened();at(2000,EV_WS_DROP,NULL);at(2010,EV_MIC_OFF,NULL);during_dispose=BR_TRANSPORT_EVENT_LOSS;
    call(false);ended(LIVE_OFF,"Mic off");assert(new_calls==1);
    /* A lost frame during release cannot authorize replay of queued mic bytes. */
    fresh();opened();host_audio(100);owner_stall_at=run_start+3000000;owner_stall_us=1500000;
    at(4450,EV_WS_DROP,NULL);during_dispose=BR_TRANSPORT_EVENT_LOSS;
    call(false);ended(LIVE_ERROR,"Voice connection interrupted.");assert(new_calls==1&&mic_high>=60000);
}
static void host_error(const char *name){
    /* A host error that another call cannot fix ends the mic-on, at any stage. */
    static const char *const fatal[]={"model_mismatch","unknown_command","invalid_preview","no_speech","expired"};
    for(unsigned i=0;i<5;i++)for(unsigned mid=0;mid<2;mid++){
        char frame[96],text[112];
        snprintf(frame,sizeof(frame),"{\"type\":\"pet.error\",\"code\":\"%s\"}",fatal[i]);
        snprintf(text,sizeof(text),"Host error: %s",fatal[i]);
        fresh();
        if(mid){opened();host_audio(100);at(2000,EV_TEXT,frame);}
        else{at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);at(80,EV_TEXT,frame);}
        at(mid?2010:90,EV_WS_CLOSED,NULL);
        call(false);ended(LIVE_ERROR,text);
        assert(new_calls==1&&opens==mid&&!strcmp(sent[1],G_BEGIN)&&!shown("Reconnecting..."));
    }
    (void)name;
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
        assert(new_calls==1&&!shown("Reconnecting...")); /* a protocol error is never retried */
        assert(opens==1&&closes==1&&run_end-run_start<4000000);
    }
}
static void timeouts(const char *name){
    (void)name;fresh();
    /* A host that never closes: the device's own backstop, as a normal end. */
    opened();host_audio(100);call(false);ended(LIVE_OFF,"Mic off (time limit)");
    assert(new_calls==1&&run_end-run_start>=SESSION_US-2000000&&run_end-run_start<=SESSION_US);
    assert(mic_sent>(uint64_t)48000*655&&current==NULL);
    assert(!strcmp(sent[sent_n-2],PET_STOP)&&!strcmp(sent[sent_n-1],SESSION_CLOSE));
    printf("deadline: seconds=%lld mic_sent=%llu appends=%u played=%llu\n",(long long)((run_end-run_start)/1000000),
        (unsigned long long)mic_sent,appends,(unsigned long long)played);
}
static void failures(const char *name){
    /* Hardware and setup failures: one status, mic off, never a reconnect. */
    fresh();opened();host_audio(100);
    const char *text=NULL;
    if(!strcmp(name,"audio-open")){open_fail=true;text="Microphone or speaker unavailable.";}
    else if(!strcmp(name,"mic-read")){mic_fail_at=100;text="Microphone read failed.";}
    else if(!strcmp(name,"speaker-write")){spk_fail_at=40;text="Speaker playback failed.";}
    else if(!strcmp(name,"setup-token")){token_fail=true;text="Could not start the host connection.";}
    else if(!strcmp(name,"setup-start")){start_fail=true;text="Could not start the host connection.";}
    call(false);ended(LIVE_ERROR,text);
    assert(run_end-run_start<20000000&&new_calls<=1&&!shown("Reconnecting..."));
    if(!strcmp(name,"setup-token"))assert(!new_calls&&!sent_n);
    if(!strcmp(name,"setup-start"))assert(new_calls==1&&dispose_calls==1&&!sent_n);
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
static void phrase_comparison(unsigned stall_ms){
    fresh();opened();mark_phrase=true; /* quiet pet speaker */
    if(stall_ms){owner_stall_at=run_start+3000000;owner_stall_us=stall_ms*1000;}
    at(12000,EV_MIC_OFF,NULL);call(false);stopped_cleanly(2);
    printf("input PCM markers, quiet speaker, stall_ms=%u: %u %u %u / 4800 each; calls=%u\n",
           stall_ms,marker_received[0],marker_received[1],marker_received[2],new_calls);
    assert(new_calls==1&&marker_received[0]==4800&&marker_received[1]==4800&&marker_received[2]==4800);
    for(unsigned i=1;i<capture_positions_sent;i++)
        assert(capture_positions[i]==capture_positions[i-1]+480);
}
static void send_wait(void){
    /* One lost packet: the socket takes no microphone frame for 1.4 s, until
     * TCP sends it again. The audio waits in the microphone buffer and the
     * SAME call goes on: delivered complete, once, in order, no faster than
     * twice real time. With the 1 s limit this call was replaced. */
    fresh();opened();host_audio(100);says(9000,10000);
    send_wait_at=run_start+3000000;send_wait_us=1400000;
    check(4300,backlog_seen);check(8000,backlog_gone);at(12000,EV_MIC_OFF,NULL);
    call(false);late_report("send-wait");stopped_cleanly(2);
    assert(new_calls==1&&!shown("Reconnecting...")&&!send_failed);
    assert(send_limit_high==5000);
    assert(mic_high>=60000&&mic_high<96000&&mic_short_writes==0);
    assert(peak_second<=96000+4800&&mic_sent>(uint64_t)48000*11);
    assert(played_audible==host_audible);
}
static unsigned off_appends;
static void after_off(void){
    /* 100 ms after the tap, the owner still inside its waiting send. */
    assert(current&&atomic_load(&current->stop)&&!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    assert(amp_latched_off&&!transport.cancelled);off_appends=appends;
}
static void send_wait_off(const char *name){
    /* Mic off while a microphone frame waits for the socket, during an answer.
     * Silence is immediate either way; what the longer wait can delay is only
     * the release of the connection. No microphone frame is admitted after
     * the tap, none is sent again, and no new call is made. */
    bool lost=!strcmp(name,"send-wait-off-lost");
    fresh();opened();host_audio(100);at(1900,EV_TEXT,G_STATE("answering"));says(2000,6000);
    send_wait_at=run_start+3000000;send_wait_us=lost?6000000:1400000;
    at(3300,EV_MIC_OFF,NULL);check(3400,after_off);
    call(false);ended(LIVE_OFF,"Mic off");
    printf("%s: idle_after_off_ms=%lld send_limit_ms=%u played_after_off=%llu appends=%u frames=%u\n",name,
        (long long)((run_end-mic_off_at)/1000),send_limit_high,(unsigned long long)played_after_off,appends,sent_n);
    assert(new_calls==1&&!shown("Reconnecting...")&&appends==off_appends&&played_after_off<=960);
    assert(mic_short_writes==0&&send_limit_high==5000);
    if(lost){
        /* The send is given up 5 s after it began, 4.7 s after the tap; the
         * connection is gone, so there is no stop to send. */
        assert(send_failed&&sent_n==2&&run_end-mic_off_at>=4700000&&run_end-mic_off_at<=4900000);
    }else{
        /* The frame leaves 1.1 s after the tap, then the ordinary stop. */
        assert(!send_failed&&sent_n==4&&!strcmp(sent[2],PET_STOP)&&!strcmp(sent[3],SESSION_CLOSE));
        assert(run_end-mic_off_at>=1100000&&run_end-mic_off_at<=1300000);
    }
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
/* ==== Reconnecting while the mic is on (user decision, 15 November 2026) ==== */
#define DELAYS 6
static const int64_t delay_s[DELAYS]={2,5,10,15,20,30}; /* the firmware's schedule, restated independently */
static int plan_status;
static void plan_ok(void){opened();}                       /* answers and begins the conversation */
static void plan_refused(void){at(50,EV_REFUSED,NULL);}    /* nobody there: no HTTP answer at all */
static void plan_hang(void){}                              /* packets vanish: the connect times out */
static void plan_http(void){http_at(50,plan_status);}      /* the host answers, but not with 101 */
static void plan_only_connect(void){at(40,EV_CONNECT,NULL);} /* connected, then silence */
static void plan_ok_drop31(void){opened();at(31000,EV_WS_DROP,NULL);}
static void plan_ok_drop10(void){opened();at(10000,EV_WS_DROP,NULL);}
static void plan_ok_drop20(void){opened();at(20000,EV_WS_DROP,NULL);}
static void plan_simple_stop(void){opened();at(1500,EV_MIC_OFF,NULL);}
static void plan_tls_fail(void){at(40,EV_TLS_FAILURE,NULL);}
static void plan_tls_silent(void){at(40,EV_TLS_FAILURE,"events-lost");}
static void cert_failure(const char *name){
    bool reconnect=!strcmp(name,"cert-reconnect")||!strcmp(name,"cert-silent-reconnect");
    bool silent=!strcmp(name,"cert-silent")||!strcmp(name,"cert-silent-reconnect");
    bool muted=!strcmp(name,"cert-muted");
    bool unknown=!strcmp(name,"cert-unknown");
    bool ordinary=!strcmp(name,"cert-no-evidence");
    bool stitched=!strcmp(name,"cert-stitch");
    fresh();
    tls_failure_result=ordinary?BR_TRANSPORT_NONE:unknown?BR_TRANSPORT_TLS_UNKNOWN:BR_TRANSPORT_CERT_VERIFY;
    if(stitched){
        const char *value=getenv("CERT_STITCH_TERMINAL");
        assert(value&&*value);char *end=NULL;long terminal=strtol(value,&end,10);
        assert(end&&!*end&&terminal==BR_TRANSPORT_CERT_VERIFY);
        tls_failure_result=(bridge_transport_terminal_t)terminal;
    }
    if(reconnect){
        opened();at(1000,EV_WS_DROP,NULL);max_calls=2;plans[1]=silent?plan_tls_silent:plan_tls_fail;
    }else{
        at(40,EV_TLS_FAILURE,silent?"events-lost":NULL);
        if(muted)at(40,EV_MIC_OFF,NULL);
        plans[1]=plan_simple_stop; /* a forbidden replacement terminates promptly */
        if(ordinary)max_calls=2;
    }
    call(false);
    if(ordinary){
        stopped_cleanly(2);
        assert(new_calls==2&&start_calls==2&&frames_total==4&&opens==1&&closes==1);
        return;
    }
    ended(muted?LIVE_OFF:LIVE_ERROR,muted?"Mic off":unknown?"Secure connection failed.":"Host certificate verification failed.");
    assert(new_calls==(reconnect?2U:1U)&&start_calls==new_calls&&!call_report.again);
    assert(frames_total==(reconnect?2U:0U)&&opens==(reconnect?1U:0U));
    assert(closes==opens&&!transport_live);
}
static uint64_t wait_mic,wait_played,re_audible;
static unsigned wait_frames;
static void reconnecting(void){ended(LIVE_RECONNECTING,"Reconnecting...");assert(!codec_open);}
static void waiting(void){ /* between two connections nothing of a call exists */
    reconnecting();assert(current==NULL&&!transport_live&&!handler&&tasks_created==tasks_exited);
    wait_mic=mic_samples;wait_played=played;wait_frames=frames_total;
}
static void still_quiet(void){ /* no microphone capture, no speaker output, nothing sent */
    reconnecting();assert(mic_samples==wait_mic&&played==wait_played&&frames_total==wait_frames);
}
static void reconnected(void){
    is_conversing();assert(call_no>=2&&mic_samples>wait_mic);
    ended(LIVE_LISTENING,"Listening"); /* the host's state again, nothing stale from before the drop */
    re_audible=played_audible;
}
static void spoke_again(void){is_conversing();assert(played_audible>=re_audible+40000);}
static void plan_again(void){ /* a healthy reconnected call, then the user's mic off */
    opened();check(1000,reconnected);says(2000,3000);check(3600,spoke_again);at(5000,EV_MIC_OFF,NULL);
}
static void plan_again_fast(void){send_latency_us=2000;plan_again();}
static int64_t gap_ms(unsigned k){return (call_at[k+1]-call_at[k])/1000;} /* connection k's start to the next one's */
static void gaps_follow(unsigned from,unsigned first_delay,unsigned n,int64_t each_ms){
    /* n consecutive failed attempts starting at connection `from`, each lasting each_ms */
    for(unsigned i=0;i<n;i++){
        int64_t expect=delay_s[first_delay+i]*1000+each_ms,got=gap_ms(from+i);
        if(got<expect||got>expect+150)fprintf(stderr,"gap after connection %u: %lld ms, expected about %lld\n",from+i,(long long)got,(long long)expect);
        assert(got>=expect&&got<=expect+150);
    }
}
static void reconnect_report(const char *name){
    printf("%s: connections=%u frames=%u seconds=%.2f status=\"%s\" starts_s=",name,new_calls,frames_total,
        (double)(run_end-run_start)/1e6,last_text);
    for(unsigned k=1;k<=call_no&&k<=12;k++)printf("%s%.2f",k>1?",":"",(double)(call_at[k]-run_start)/1e6);
    printf("%s\n",call_no>12?",...":"");
}
static void re_drop(const char *name){
    /* Loss at each conversation stage: replace only without observed closing
     * evidence or unresolved send state. Valid cleanup closes keep ONE call. */
    fresh();opened();host_audio(100);max_calls=2;plans[1]=plan_again;
    int64_t drop=3000;const char *first="Listening";
    if(!strcmp(name,"re-asking")){at(2500,EV_TEXT,G_STATE("asking"));check(2900,sees_asking);at(drop,EV_WS_ERROR,NULL);first="Asking Unified...";}
    else if(!strcmp(name,"re-answering")){at(2000,EV_TEXT,G_STATE("answering"));says(2000,2990);check(2900,sees_speaking);at(drop,EV_WS_DROP,NULL);first="Speaking";}
    else if(!strcmp(name,"re-close")){at(drop,EV_TEXT,CLOSED);at(drop+10,EV_WS_CLOSED,NULL);}
    else if(!strcmp(name,"re-error")){at(drop,EV_TEXT,"{\"type\":\"pet.error\",\"code\":\"reply_unavailable\"}");at(drop+10,EV_WS_CLOSED,NULL);}
    else if(!strcmp(name,"re-flood")){for(unsigned i=0;i<40;i++)at(drop,EV_TEXT,G_STATE("asking"));}
    else if(!strcmp(name,"re-send")){send_fail_at=140;drop=2800;} /* one microphone frame not fully written */
    else if(!strcmp(name,"re-stall")){send_latency_us=900000;host_next=0;plans[1]=plan_again_fast;drop=0;} /* isolate a link that cannot carry the microphone */
    else if(!strcmp(name,"re-stall-long")){owner_stall_at=run_start+3000000;owner_stall_us=2600000;drop=5600;}
    else if(!strcmp(name,"re-send-wait-long")){send_wait_at=run_start+3000000;send_wait_us=2500000;drop=5500;}
    else{assert(!strcmp(name,"re-listening"));at(drop,EV_WS_DROP,NULL);}
    if(!strcmp(name,"re-close")){
        call(false);ended(LIVE_OFF,"Mic off (call ended)");
        assert(new_calls==1&&!shown("Reconnecting..."));return;
    }
    if(!strcmp(name,"re-error")){
        call(false);ended(LIVE_ERROR,"Host error: reply_unavailable");
        assert(new_calls==1&&!shown("Reconnecting..."));return;
    }
    if(!strcmp(name,"re-stall")||!strcmp(name,"re-stall-long")||!strcmp(name,"re-send-wait-long")){
        /* Full microphone FIFO drops newest chunks; one call survives until
         * explicit mute, with admitted sample positions checked at the wire. */
        max_calls=1;at(12000,EV_MIC_OFF,NULL);
        call(false);
        if(!strcmp(name,"re-stall")){
            /* This fixture delays EVERY send, including both cleanup controls,
             * by 900 ms. Audio must mute now; release includes those waits. */
            ended(LIVE_OFF,"Mic off");
            assert(sent_n==4&&!strcmp(sent[2],PET_STOP)&&!strcmp(sent[3],SESSION_CLOSE));
            assert(!up_at_fence&&run_end-mic_off_at<=3*send_latency_us+50000&&played_after_off<=960);
        }else stopped_cleanly(2);
        assert(new_calls==1&&!shown("Reconnecting...")&&mic_short_writes==0);
        if(!strcmp(name,"re-stall"))assert(mic_high==BV_MIC); /* sustained overload */
        else assert(mic_high>96000&&mic_high<BV_MIC); /* former FIFO would lose speech */
        assert(send_limit_high==5000);
        if(strcmp(name,"re-stall"))assert(!send_failed&&mic_sent>48000*10);
        return;
    }
    if(!strcmp(name,"re-send")){
        /* The local failure is real, but the default peer answers cleanup with
         * valid session.closed. Its cause is unknowable: do not replace. */
        max_calls=1;
        call(false);ended(LIVE_OFF,"Mic off (call ended)");
        assert(new_calls==1&&!shown("Reconnecting..."));
        if(!strcmp(name,"re-send"))assert(send_failed);
        else assert(mic_high==96000);
        return;
    }
    if(!strcmp(name,"re-flood")){
        /* The discarded message is unknown, so it might have contained the
         * host's ending. Do not silently open a paid replacement call. */
        call(false);ended(LIVE_ERROR,"Voice connection interrupted.");assert(new_calls==1);return;
    }
    if(!strcmp(name,"re-asking")){
        /* Observed asking follows gateway confirm admission. Callback failure
         * before answering is now a veto, not a paid replacement opportunity. */
        max_calls=1;
        call(false);ended(LIVE_ERROR,PENDING_TEXT);
        assert(new_calls==1&&opens==1&&shown("Asking Unified...")&&!call_report.again);
        assert(!shown("Reconnecting..."));return;
    }
    if(drop){check(drop+1000,waiting);check(drop+1900,still_quiet);}
    call(false);reconnect_report(name);stopped_cleanly(2);
    assert(new_calls==2&&opens==2&&closes==2&&tasks_created==4&&dispose_calls==2);
    assert(shown(first)&&shown("Reconnecting...")&&inhibits>=2);
    if(drop&&strcmp(name,"re-send"))assert(call_at[2]-(run_start+drop*1000)>=2000000&&call_at[2]-(run_start+drop*1000)<=2200000);
    /* The lost call was never told to stop (its link was gone); the new one was. */
    assert(frames_total==(strcmp(name,"re-close")&&strcmp(name,"re-error")&&strcmp(name,"re-stall")&&
        strcmp(name,"re-stall-long")&&strcmp(name,"re-send")&&strcmp(name,"re-flood")?6U:frames_total));
    for(unsigned i=0;i<history_n;i++)assert(history[i].state!=LIVE_ERROR); /* no error was ever shown */
}
static void inject_queued_close(void){inject_before_control=true;}
static void retry_boundaries(void){
    /* Overflow with queued asking then normal close. Neither the independent
     * host ending nor cleanup settles that observed pending send. */
    fresh();opened();queue_fault_stall();
    at(5300,EV_TEXT,G_STATE("asking"));at(5310,EV_TEXT,CLOSED);
    call(false);ended(LIVE_ERROR,PENDING_TEXT);
    assert(new_calls==1&&frames_high>=2);

    /* A callback arrives after the first empty tail drain, but before the
     * control admission. Its queued snapshot remains authoritative when the
     * second tail drain runs after local stop/close. */
    fresh();opened();queue_fault_stall();
    check(5400,inject_queued_close);
    call(false);ended(LIVE_OFF,"Mic off (call ended)");
    assert(new_calls==1&&injected_pre_control==1);

    /* Even inside a full or short pet.stop send, valid session.closed ends
     * replacement eligibility. A short result is uncertain, never repeated. */
    for(unsigned short_send=0;short_send<2;short_send++){
        fresh();opened();queue_fault_stall();
        reply_during_stop=true;short_stop=short_send!=0;plans[1]=plan_simple_stop;
        call(false);ended(LIVE_OFF,"Mic off (call ended)");
        assert(new_calls==1&&mic_high==96000&&stop_replies==1&&short_stops==short_send&&frames_total==4);
        assert(!shown("Reconnecting...")&&!call_report.again);
    }

    /* Bare loss, an empty raw close and 1011 have no definite terminal reason.
     * The actual transport fixture separately verifies 1002/1008 and 1011. */
    for(unsigned kind=0;kind<3;kind++){
        fresh();opened();max_calls=2;plans[1]=plan_simple_stop;
        if(kind==0)at(2000,EV_WS_DROP,NULL);
        else{at(2000,kind==1?EV_RAW_CLOSE:EV_CLOSE_1011,NULL);at(2010,EV_WS_CLOSED,NULL);}
        call(false);stopped_cleanly(2);assert(new_calls==2);
    }

    /* Incomplete data at proof is unknown ingress and vetoes retry. */
    fresh();opened();at(2000,EV_PARTIAL_TEXT,CLOSED);at(2100,EV_WS_DROP,NULL);
    call(false);ended(LIVE_ERROR,"Voice connection interrupted.");assert(new_calls==1);

    /* Completion by an admitted callback during dispose is observed only
     * after final-access proof, so it does not spuriously veto retry. */
    fresh();opened();at(2000,EV_PARTIAL_TEXT,G_STATE("listening"));at(2100,EV_WS_DROP,NULL);
    finish_during_dispose=true;max_calls=2;plans[1]=plan_simple_stop;
    call(false);stopped_cleanly(2);assert(new_calls==2);

    /* This fragmented host close began before local controls but completes
     * during disposal, after the overflow prompted those controls. */
    for(unsigned rfc=0;rfc<2;rfc++){
        fresh();opened();queue_fault_stall();
        at(5300,EV_PARTIAL_TEXT,CLOSED);finish_during_dispose=true;fragment_rfc6455=rfc!=0;host_closes=false;
        call(false);ended(LIVE_OFF,"Mic off (call ended)");
        assert(new_calls==1&&fragment_starts_before==1&&fragment_starts_after==0&&fragment_finishes_after==1);
        assert(fragment_controls_after==rfc);
    }

    /* A fragmented close starting inside already-admitted pet.stop could have
     * either cause. Its valid completion during disposal vetoes replacement,
     * including when our local send returned short. */
    for(unsigned rfc=0;rfc<2;rfc++)for(unsigned short_send=0;short_send<2;short_send++){
        fresh();opened();queue_fault_stall();
        fragment_reply_on_stop=true;fragment_rfc6455=rfc!=0;short_stop=short_send!=0;
        host_closes=false;plans[1]=plan_simple_stop;
        call(false);ended(LIVE_OFF,"Mic off (call ended)");
        assert(new_calls==1&&mic_high==96000&&stop_replies==1&&short_stops==short_send&&frames_total==4);
        assert(!shown("Reconnecting...")&&!call_report.again);
        assert(fragment_starts_before==0&&fragment_starts_after==1&&fragment_finishes_after==1);
        assert(fragment_controls_after==(rfc?2U:0U));
    }

    /* A local-control-era prefix is not enough to classify an unknown frame.
     * If the callback never completes it, settled assembly must veto retry. */
    fresh();opened();queue_fault_stall();
    fragment_reply_on_stop=true;fragment_rfc6455=true;drop_fragment_completion=true;host_closes=false;
    call(false);ended(LIVE_ERROR,"Voice connection interrupted.");
    assert(new_calls==1&&mic_high==96000&&stop_replies==1&&fragment_starts_after==1);
    assert(fragment_finishes_after==0&&fragment_controls_after==1);

    /* Explicit mute remains authoritative even with incomplete ingress. */
    fresh();opened();host_closes=false;at(2000,EV_PARTIAL_TEXT,CLOSED);at(2100,EV_MIC_OFF,NULL);
    call(false);ended(LIVE_OFF,"Mic off");assert(new_calls==1);

    /* A failed final-access proof retains everything; the late reaper checks
     * the unfinished assembly and never reconnects on its own. */
    fresh();opened();at(2000,EV_PARTIAL_TEXT,CLOSED);at(2100,EV_WS_DROP,NULL);dispose_ok=false;
    call(true);ended(LIVE_CLOSING,"Releasing voice connection...");
    assert(new_calls==1&&current&&bridge_voice_restart_required()&&transport_live);
    dispose_ok=true;bridge_voice_reap_finished();
    ended(LIVE_ERROR,"Voice connection interrupted.");
    assert(new_calls==1&&!current&&!bridge_voice_restart_required()&&!transport_live&&!call_report.again);
}
/* ==== Red-first follow-up: terminal evidence survives local cleanup ==== */
#define NO_DELEGATION "{\"type\":\"pet.error\",\"code\":\"no_delegation\"}"
#define CLOSE_UNCONFIRMED "{\"type\":\"pet.error\",\"code\":\"close_unconfirmed\"}"
#define UNCERTAIN_TEXT "Could not confirm it was sent"
/* Observations of the scripted peer and wire, not a replacement retry policy.
 * Sequence numbers also distinguish boundaries on the same virtual-clock tick. */
static struct {
    const char *first;
    bool post_control,consumed_before_stop,overwrite,mute,normal;
    unsigned serial,first_order,stop_order,close_order,dispose_order;
    int64_t first_at,stop_at,close_at;
    unsigned appends_at_stop;
} ending_probe;
static void ending_ingress(void){
    assert(current&&handler&&(ws_up||transport.cancelled)&&call_no==1&&ending_probe.first&&!ending_probe.first_order);
    QueueHandle_t q=current->frames;
    assert(q&&q->n);
    fixture_frame_t *f=&q->items[q->n-1];
    assert(!strcmp(f->text,ending_probe.first));
    assert(f->after_local_control==ending_probe.post_control);
    ending_probe.first_at=f->received_at;
    ending_probe.first_order=++ending_probe.serial;
}
static void ending_queued_after_failure(void){
    /* The injected capacity-probe fault caused a real short enqueue while the owner was blocked. The first
     * terminal frame is waiting for the first cleanup tail, not the main loop. */
    assert(current&&atomic_load(&current->failed)&&current->model.phase==BR_CLOSING);
    assert(atomic_load(&current->exit_reason)==BV_EXIT_QUEUE&&mic_high==96000);
    assert(!transport.local_control&&!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    ending_ingress();
}
static void ending_before_control(void){
    assert(self==0&&current&&!transport.local_control&&!guard_held);
    assert(current->model.phase==BR_CLOSING&&atomic_load(&current->failed));
    assert(atomic_load(&current->exit_reason)==BV_EXIT_QUEUE);
    assert(!ending_probe.first_order&&!ending_probe.stop_order);
    deliver(ending_probe.first);ending_ingress();
}
static void ending_delayed_close(void){
    /* The owner must have entered cleanup, inhibited audio and admitted/sent
     * pet.stop BEFORE this delayed peer session.closed is allowed to arrive. */
    assert(current&&handler&&ws_up&&call_no==1&&transport.local_control);
    assert(ending_probe.stop_order&&!ending_probe.close_order);
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    assert(call_appends==ending_probe.appends_at_stop);
    if(ending_probe.first){
        assert(ending_probe.first_order&&ending_probe.first_at<=now);
        assert(ending_probe.post_control?ending_probe.stop_order<ending_probe.first_order:
            ending_probe.first_order<ending_probe.stop_order);
    }
    assert(!current->model.wire_closed);
    unsigned queued=current->frames->n;
    deliver(CLOSED);
    assert(current->frames->n==queued+1);
    fixture_frame_t *f=&current->frames->items[queued];
    assert(!strcmp(f->text,CLOSED)&&f->after_local_control);
    ending_probe.close_at=f->received_at;
    ending_probe.close_order=++ending_probe.serial;
}
static void ending_disposed(void){
    assert(self==0&&current&&transport.cancelled&&transport.local_control);
    assert(ending_probe.close_order>ending_probe.stop_order);
    if(ending_probe.first)assert(ending_probe.close_order>ending_probe.first_order);
    assert(ending_probe.close_at>ending_probe.stop_at);
    assert(call_appends==ending_probe.appends_at_stop);
    ending_probe.dispose_order=++ending_probe.serial;
    /* normal=true must correspond to an actual complete CLOSE callback, never
     * an injected final enum presented as if the peer sent that frame. */
    if(ending_probe.normal)assert(normal_close_frames==1);
}
static void ending_stop_sent(void){
    assert(self==0&&current&&call_no==1&&!guard_held&&transport.local_control);
    assert(current->model.phase==BR_CLOSING);
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    assert(sent_n==3&&!strcmp(sent[2],PET_STOP)&&!ending_probe.stop_order);
    ending_probe.stop_at=now;ending_probe.stop_order=++ending_probe.serial;
    ending_probe.appends_at_stop=call_appends;
    if(ending_probe.consumed_before_stop){
        assert(ending_probe.first_order&&current->frames->n==0);
        if(!strcmp(ending_probe.first,NO_DELEGATION))assert(!strcmp(current->model.code,"no_delegation"));
    }
    if(ending_probe.post_control){
        assert(!ending_probe.first_order);
        deliver(ending_probe.first);ending_ingress();
    }
    if(ending_probe.overwrite)later(10000,EV_TEXT,CLOSE_UNCONFIRMED);
    if(ending_probe.mute)later(15000,EV_MIC_OFF,NULL);
    later(30000,EV_CHECK,NULL);script[script_n-1].check=ending_delayed_close;
    if(ending_probe.normal)later(35000,EV_CLOSE_1000,NULL);
    later(40000,EV_WS_CLOSED,NULL);
}
static void ending_script(const char *first){
    ending_probe=(typeof(ending_probe)){.first=first};
    host_closes=false;stop_send_hook=ending_stop_sent;dispose_hook=ending_disposed;
    /* If old production opens a forbidden replacement, end that fake call
     * promptly. call() still permits ONE call and fails on the extra start. */
    plans[1]=plan_simple_stop;
}
static void ending_order_proven(void){
    assert(ending_probe.stop_order&&ending_probe.close_order>ending_probe.stop_order);
    assert(ending_probe.dispose_order>ending_probe.close_order);
    if(ending_probe.first)assert(ending_probe.first_order&&ending_probe.close_order>ending_probe.first_order);
    assert(sent_n==4&&!strcmp(sent[2],PET_STOP)&&!strcmp(sent[3],SESSION_CLOSE));
}
static void ending_one_call(void){
    assert(new_calls==1&&start_calls==1&&dispose_calls==1&&!call_report.again);
    assert(opens==1&&closes==1&&tasks_created==2);
}
static void explicit_start_again(void){
    /* fresh() resets fixture state only. No production static is reset, so
     * sticky evidence from an ended mic-on must not poison a new user start. */
    fresh();opened();at(1500,EV_MIC_OFF,NULL);
    call(false);stopped_cleanly(2);ending_one_call();
}
static void terminal_error_cleanup(const char *name){
    fresh();opened();ending_script(NO_DELEGATION);
    bool local_normal=!strcmp(name,"terminal-local-normal");
    bool local=!strcmp(name,"terminal-local-close")||local_normal;
    bool queued=!strcmp(name,"terminal-error-tail-queued");
    bool pre=!strcmp(name,"terminal-error-tail-pre");
    bool post=!strcmp(name,"terminal-error-tail-post");
    bool muted=!strcmp(name,"terminal-error-muted");
    if(local||queued||pre||post){
        /* Injected capacity-probe fault; actual capture-task failure path, not a forged owner reason/model. */
        queue_fault_stall();
        if(local){
            ending_probe.first=NULL;ending_probe.normal=local_normal;
        }else if(queued){
            at(5300,EV_TEXT,NO_DELEGATION);check(5400,ending_queued_after_failure);
            ending_probe.consumed_before_stop=true;
        }else if(pre)before_control_hook=ending_before_control;
        else ending_probe.post_control=true;
    }else{
        assert(!strcmp(name,"terminal-error-delayed")||!strcmp(name,"terminal-error-overwrite")||muted);
        at(2000,EV_TEXT,NO_DELEGATION);check(2000,ending_ingress);
        ending_probe.consumed_before_stop=true;
        ending_probe.overwrite=strcmp(name,"terminal-error-delayed")!=0;
        ending_probe.normal=ending_probe.overwrite;ending_probe.mute=muted;
    }
    call(false);
    if(local){
        /* Valid JSON close, with or without WS1000, vetoes a fresh paid call;
         * receipt during cleanup does not establish local or remote cause. */
        ended(LIVE_OFF,"Mic off (call ended)");ending_order_proven();ending_one_call();
        assert(mic_high==96000&&ending_probe.dispose_order>ending_probe.close_order);
        assert(!shown("Reconnecting..."));
    }else{
        ending_order_proven();ending_one_call();
        ended(muted?LIVE_OFF:LIVE_ERROR,muted?"Mic off":"Host error: no_delegation");
    }
}
static void sees_uncertainty(void){
    see(LIVE_LISTENING,UNCERTAIN_TEXT);
    assert(current->model.notice==BR_NOTICE_SEND_UNCERTAIN&&current->model.notice_visible);
    assert(!transport.local_control); /* accepted during the running conversation */
}
static void sees_expired_uncertainty(void){
    sees_listening();
    assert(shown(UNCERTAIN_TEXT)&&!current->model.notice_visible);
}
static void uncertainty_at_disposal(void){
    /* A bare drop has already forced failure/closing, but the admitted
     * callback can still deliver terminal evidence before final-access proof. */
    assert(self==0&&current&&transport.cancelled&&!transport.local_control);
    assert(atomic_load(&current->failed)&&current->model.phase==BR_CLOSING);
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    assert(!ending_probe.first_order&&current->frames->n==0);
    /* deliver() intentionally suppresses new peer traffic once ws_up is false.
     * This models an ALREADY-admitted SDK DATA callback, not a revived socket:
     * accepting stays true until dispose proves the callback task has exited. */
    assert(handler&&atomic_load(&current->accepting)&&!ws_up);
    const char *json=G_NOTICE("send_uncertain");
    int n=(int)strlen(json);
    esp_websocket_event_data_t d={.data_len=n,.payload_len=n,.op_code=1,.fin=true,.data_ptr=json};
    handler(handler_arg,NULL,WEBSOCKET_EVENT_DATA,&d);ending_ingress();
    ending_probe.dispose_order=++ending_probe.serial;
}
static void uncertain_ending(const char *name){
    fresh();opened();
    bool visible=!strcmp(name,"uncertain-visible-loss");
    bool expired=!strcmp(name,"uncertain-expired-loss");
    bool muted=!strcmp(name,"uncertain-muted");
    bool queued=!strcmp(name,"uncertain-tail-queued");
    bool tail_muted=!strcmp(name,"uncertain-tail-muted");
    bool disposal=!strcmp(name,"uncertain-dispose");
    if(visible||expired||muted){
        at(2000,EV_TEXT,G_NOTICE("send_uncertain"));check(2200,sees_uncertainty);
        if(expired){
            check(6500,sees_expired_uncertainty);
            at(6600,EV_TEXT,G_NOTICE("not_sent"));
            at(6700,EV_TEXT,G_STATE("answering"));at(6800,EV_TEXT,G_STATE("listening"));
        }
        at(expired?7000:3000,muted?EV_MIC_OFF:EV_WS_DROP,NULL);
        plans[1]=plan_simple_stop;
    }else if(disposal){
        ending_probe=(typeof(ending_probe)){.first=G_NOTICE("send_uncertain")};
        at(2000,EV_WS_DROP,NULL);dispose_hook=uncertainty_at_disposal;plans[1]=plan_simple_stop;
    }else{
        assert(queued||!strcmp(name,"uncertain-tail-post")||tail_muted);
        ending_script(G_NOTICE("send_uncertain"));
        queue_fault_stall();
        if(queued){
            at(5300,EV_TEXT,G_NOTICE("send_uncertain"));check(5400,ending_queued_after_failure);
            ending_probe.consumed_before_stop=true;
        }else ending_probe.post_control=true;
        ending_probe.mute=tail_muted;
        /* Even a final normal 1000 cannot resolve an uncertain Unified send. */
        ending_probe.normal=true;
    }
    call(false);ending_one_call();
    if(!visible&&!expired&&!muted&&!disposal)ending_order_proven();
    if(disposal)assert(ending_probe.dispose_order>ending_probe.first_order);
    ended(muted||tail_muted?LIVE_OFF:LIVE_ERROR,muted||tail_muted?"Mic off":UNCERTAIN_TEXT);
    if(visible||expired||muted)assert(shown(UNCERTAIN_TEXT));
    /* A terminal mic-on never starts itself again, but an explicit fresh user
     * start is allowed immediately; there is no retained global veto. */
    explicit_start_again();
}
static void ending_controls(void){
    /* Genuine bare loss has no terminal/uncertain evidence and still recovers.
     * Repeat after an uncertain-but-explicitly-muted mic-on to catch sticky
     * evidence accidentally escaping the call owner into later user starts. */
    for(unsigned round=0;round<2;round++){
        if(round)uncertain_ending("uncertain-muted");
        fresh();opened();at(2000,EV_WS_DROP,NULL);max_calls=2;plans[1]=plan_simple_stop;
        call(false);stopped_cleanly(2);
        assert(new_calls==2&&shown("Reconnecting...")&&opens==2&&closes==2);
        explicit_start_again();
    }
}
static void terminal_code_matrix(void){
    static const char *const codes[]={"busy","deadline","overflow","reply_unavailable",
        "no_delegation","unstable_transcript","close_unconfirmed"};
    for(unsigned i=0;i<sizeof(codes)/sizeof(*codes);i++)for(unsigned active=0;active<2;active++){
        char frame[128],line[80];
        snprintf(frame,sizeof(frame),"{\"type\":\"pet.error\",\"code\":\"%s\"}",codes[i]);
        snprintf(line,sizeof(line),"Host error: %s",codes[i]);
        fresh();
        if(active)opened();else{at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);}
        at(active?2000:80,EV_TEXT,frame);plans[1]=plan_simple_stop;
        call(false);ended(LIVE_ERROR,line);
        assert(new_calls==1&&start_calls==1&&!call_report.again&&!shown("Reconnecting..."));
    }
}
/* ==== Unconfirmed closing evidence after an injected local enqueue fault ====
 * Hooks observe real owner I/O/release boundaries; they never set an owner exit,
 * model, cleanup flag or retry result. The shared sample/wire oracles above stay
 * active in every case. Receipt provenance never permits replacement. */
static struct {
    bool pre,fragment,drop,startup,peer,same_tick,no_stamp,ingress,uncertain_tail,later_error,muted,pending,pending_tail;
    bool expect_secondary,expect_full,inspected;
    const char *terminal,*model_code;
    unsigned stops,errors,appends_at_stop;
    int64_t stop_at;
} cleanup_probe;
static void cleanup_error_ingress(void){
    assert(current&&handler&&call_no==1&&!guard_held);
    assert(!cleanup_probe.errors&&(!cleanup_probe.drop||transport.cancelled));
    if(cleanup_probe.same_tick||cleanup_probe.no_stamp){
        override_ticket_stamp=true;ticket_stamp=cleanup_probe.no_stamp?0:cleanup_probe.stop_at;
    }
    if(cleanup_probe.fragment)deliver_fragment(true);
    else if(cleanup_probe.drop){
        /* Finish an already admitted callback across a bare disconnect. No
         * local send occurred; ordinary terminal-error bookkeeping applies. */
        const char *json=CLOSE_UNCONFIRMED;int n=(int)strlen(json);
        esp_websocket_event_data_t d={.data_len=n,.payload_len=n,.op_code=1,.fin=true,.data_ptr=json};
        handler(handler_arg,NULL,WEBSOCKET_EVENT_DATA,&d);
    }else deliver(CLOSE_UNCONFIRMED);
    assert(current->frames->n);
    fixture_frame_t *f=&current->frames->items[current->frames->n-1];
    assert(!strcmp(f->text,CLOSE_UNCONFIRMED));
    assert(f->after_local_control==(!cleanup_probe.pre&&!cleanup_probe.drop));
    if(!cleanup_probe.pre&&!cleanup_probe.drop&&!cleanup_probe.same_tick&&!cleanup_probe.no_stamp)
        assert(f->received_at>cleanup_probe.stop_at);
    cleanup_probe.errors++;
    if(cleanup_probe.ingress){
        fail_message_alloc=true;deliver(G_STATE("listening"));
        assert(atomic_load(&current->ingress_incomplete));
    }
    if(cleanup_probe.uncertain_tail){
        deliver(G_NOTICE("send_uncertain"));deliver(G_NOTICE("not_sent"));
    }
    if(cleanup_probe.pending_tail){
        deliver(G_STATE("asking"));deliver(G_STATE("listening"));
    }
}
static void cleanup_fragment_start(void){
    assert(current&&transport.local_control&&!current->assembly.active);
    fragment_message=CLOSE_UNCONFIRMED;deliver_fragment(false);
}
static void cleanup_error_before_control(void){
    assert(current&&!transport.local_control&&atomic_load(&current->exit_reason)==BV_EXIT_QUEUE);
    if(cleanup_probe.fragment){fragment_message=CLOSE_UNCONFIRMED;deliver_fragment(false);}
    else cleanup_error_ingress();
}
static void cleanup_stop_observed(void){
    assert(self==0&&current&&call_no==1&&!guard_held&&transport.local_control);
    assert(!cleanup_probe.stops&&!current->cleanup_stop_sent);
    assert(current->model.phase==BR_CLOSING||cleanup_probe.peer);
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    assert(atomic_load(&current->exit_reason)==(cleanup_probe.startup?BV_EXIT_BEGIN:BV_EXIT_QUEUE));
    assert(cleanup_probe.startup?!current->began_at:current->began_at!=0);
    if(!cleanup_probe.startup)assert(mic_high==96000);
    cleanup_probe.stops++;cleanup_probe.stop_at=now;cleanup_probe.appends_at_stop=call_appends;
    if(cleanup_probe.fragment&&!cleanup_probe.pre){
        later(10000,EV_CHECK,NULL);script[script_n-1].check=cleanup_fragment_start;
    }
    if(!cleanup_probe.pre||cleanup_probe.fragment){
        later(cleanup_probe.fragment?20000:10000,EV_CHECK,NULL);
        script[script_n-1].check=cleanup_error_ingress;
    }
    if(cleanup_probe.later_error)later(20000,EV_TEXT,NO_DELEGATION);
    if(cleanup_probe.muted)later(15000,EV_MIC_OFF,NULL);
    /* No session.closed at all: the error followed by an RFC 6455 normal close
     * is the gateway cleanup shape under review, not proof of its cause. */
    later(30000,EV_CLOSE_1000,NULL);later(35000,EV_WS_CLOSED,NULL);
}
static void cleanup_disposal_observed(void){
    assert(current&&transport.cancelled&&call_no==1&&!guard_held);
    if(cleanup_probe.drop)cleanup_probe.appends_at_stop=call_appends;
    if(!cleanup_probe.errors){
        /* A cancelled stop cannot linger. Complete an in-flight callback at
         * disposal instead, with valid after-admission provenance. */
        assert(cleanup_probe.drop||stop_faults==1);
        cleanup_error_ingress();
    }
    if(!cleanup_probe.drop&&!normal_close_frames){
        event_t e={.kind=EV_CLOSE_1000,.call=call_no};fire(&e);
    }
    assert(current->model.wire_closed==cleanup_probe.peer);
    assert(call_appends==cleanup_probe.appends_at_stop);
}
static void cleanup_release_observed(void){
    /* audio close follows the final cleanup tail and successful transport proof,
     * before c is unpublished/freed. Inspect, but never drive, owner state. */
    assert(current&&!current->transport&&!guard_held&&call_no==1);
    assert(current->cleanup_stop_sent==cleanup_probe.expect_full);
    assert(current->cleanup_close_unconfirmed==cleanup_probe.expect_secondary);
    assert(current->send_pending==cleanup_probe.pending);
    assert(!strcmp(current->terminal_code,cleanup_probe.terminal));
    assert(!strcmp(current->model.code,cleanup_probe.model_code));
    assert(current->model.wire_closed==cleanup_probe.peer);
    assert(call_appends==cleanup_probe.appends_at_stop);
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    cleanup_probe.inspected=true;
}
static void cleanup_replacement(void){host_closes=true;plan_simple_stop();}
static void cleanup_unconfirmed_case(const char *name){
    fresh();host_closes=false;plans[1]=cleanup_replacement;
    cleanup_probe=(typeof(cleanup_probe)){.expect_full=true,.expect_secondary=true,.terminal="",.model_code=""};
    bool local=!strcmp(name,"cleanup-unconfirmed-local");
    bool short_send=!strcmp(name,"cleanup-unconfirmed-short");
    bool failed=!strcmp(name,"cleanup-unconfirmed-failed");
    bool cancelled=!strcmp(name,"cleanup-unconfirmed-cancel");
    bool cancelled_full=!strcmp(name,"cleanup-unconfirmed-cancel-full");
    bool prior=!strcmp(name,"cleanup-unconfirmed-prior");
    bool later_error=!strcmp(name,"cleanup-unconfirmed-later");
    bool uncertain=!strcmp(name,"cleanup-unconfirmed-uncertain");
    bool muted=!strcmp(name,"cleanup-unconfirmed-muted");
    cleanup_probe.pending=!strcmp(name,"cleanup-unconfirmed-pending")||!strcmp(name,"cleanup-unconfirmed-pending-tail");
    cleanup_probe.pending_tail=!strcmp(name,"cleanup-unconfirmed-pending-tail");
    cleanup_probe.later_error=later_error;cleanup_probe.muted=muted;
    bool protocol=!strcmp(name,"cleanup-unconfirmed-protocol");
    bool policy=!strcmp(name,"cleanup-unconfirmed-policy");
    bool retained=!strcmp(name,"cleanup-unconfirmed-retained");
    bool notice=!strcmp(name,"cleanup-unconfirmed-notice");
    cleanup_probe.pre=!strcmp(name,"cleanup-unconfirmed-pre")||!strcmp(name,"cleanup-unconfirmed-fragment-pre");
    cleanup_probe.fragment=!strcmp(name,"cleanup-unconfirmed-fragment-pre")||!strcmp(name,"cleanup-unconfirmed-fragment-post");
    cleanup_probe.drop=!strcmp(name,"cleanup-unconfirmed-disconnect");
    cleanup_probe.startup=!strcmp(name,"cleanup-unconfirmed-startup");
    cleanup_probe.peer=!strcmp(name,"cleanup-unconfirmed-peer");
    cleanup_probe.same_tick=!strcmp(name,"cleanup-unconfirmed-same-tick");
    cleanup_probe.no_stamp=!strcmp(name,"cleanup-unconfirmed-no-stamp");
    cleanup_probe.ingress=!strcmp(name,"cleanup-unconfirmed-ingress");
    cleanup_probe.uncertain_tail=!strcmp(name,"cleanup-unconfirmed-uncertain-tail");
    assert(local||short_send||failed||cancelled||cancelled_full||prior||later_error||uncertain||muted||protocol||policy||retained||notice||
        cleanup_probe.pending||
        cleanup_probe.pre||cleanup_probe.fragment||cleanup_probe.drop||cleanup_probe.startup||cleanup_probe.peer||
        cleanup_probe.same_tick||cleanup_probe.no_stamp||cleanup_probe.ingress||cleanup_probe.uncertain_tail);
    /* Every valid close_unconfirmed is a veto, including the separately
     * recorded late-cleanup shape. Keep max_calls==1 in every case. */
    if(cleanup_probe.startup){at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);}
    else opened();
    if(cleanup_probe.drop)at(2000,EV_WS_DROP,NULL);
    else if(!cleanup_probe.startup){queue_fault_stall();}
    stop_send_hook=cleanup_stop_observed;dispose_hook=cleanup_disposal_observed;audio_close_hook=cleanup_release_observed;
    if(short_send||failed||cancelled||cancelled_full){
        stop_fault=short_send?STOP_SHORT:failed?STOP_FAILED:cancelled?STOP_CANCELLED:STOP_CANCELLED_FULL;
        cleanup_probe.expect_full=false;
    }
    if(!cleanup_probe.expect_full||cleanup_probe.pre||cleanup_probe.drop||cleanup_probe.startup||
       cleanup_probe.peer||cleanup_probe.same_tick||cleanup_probe.no_stamp){
        cleanup_probe.expect_secondary=false;
        cleanup_probe.terminal=cleanup_probe.model_code="close_unconfirmed";
    }
    if(cleanup_probe.drop)cleanup_probe.expect_full=false;
    if(cleanup_probe.pre)before_control_hook=cleanup_error_before_control;
    if(prior){
        at(5300,EV_TEXT,NO_DELEGATION);
        cleanup_probe.terminal=cleanup_probe.model_code="no_delegation";
    }
    if(later_error){
        /* Both errors remain queued for the second cleanup tail. A secondary
         * observation must not occupy terminal_code and hide this real error. */
        cleanup_probe.terminal=cleanup_probe.model_code="no_delegation";
    }
    if(uncertain){
        at(1000,EV_TEXT,G_NOTICE("send_uncertain"));
        at(2000,EV_TEXT,G_NOTICE("not_sent"));at(2500,EV_TEXT,G_STATE("listening"));
    }
    if(cleanup_probe.pending&&!cleanup_probe.pending_tail)at(1000,EV_TEXT,G_STATE("asking"));
    if(protocol)during_dispose=BR_TRANSPORT_PROTOCOL;
    if(policy)during_dispose=BR_TRANSPORT_POLICY;
    if(retained)dispose_ok=false;
    if(cleanup_probe.peer)at(5310,EV_TEXT,CLOSED);
    if(notice)at(5300,EV_TEXT,G_NOTICE("time_limit"));
    call(retained);
    if(retained){
        ended(LIVE_CLOSING,"Releasing voice connection...");
        assert(new_calls==1&&current&&current->retained&&current->cleanup_stop_sent);
        assert(!current->cleanup_close_unconfirmed&&!cleanup_probe.inspected&&!call_report.again);
        dispose_ok=true;bridge_voice_reap_finished();
        assert(!current&&!bridge_voice_restart_required()&&!transport_live&&!call_report.again);
        ended(LIVE_ERROR,"Host error: close_unconfirmed");
    }else{
        assert(new_calls==1&&start_calls==1&&!call_report.again);
        if(muted)ended(LIVE_OFF,"Mic off");
        else if(uncertain||cleanup_probe.uncertain_tail)ended(LIVE_ERROR,UNCERTAIN_TEXT);
        else if(cleanup_probe.pending)ended(LIVE_ERROR,PENDING_TEXT);
        else if(protocol)ended(LIVE_ERROR,"Voice connection protocol error.");
        else if(policy)ended(LIVE_ERROR,"Host refused the voice connection.");
        else if(cleanup_probe.ingress)ended(LIVE_ERROR,"Voice connection interrupted.");
        else ended(LIVE_ERROR,prior||later_error?"Host error: no_delegation":"Host error: close_unconfirmed");
    }
    assert(new_calls==1&&start_calls==1&&!call_report.again&&!shown("Reconnecting..."));
    assert(cleanup_probe.errors==1&&cleanup_probe.stops==(cleanup_probe.drop?0U:1U));
    assert(cleanup_probe.startup?!cleanup_probe.inspected:cleanup_probe.inspected);
    if(!cleanup_probe.drop)assert(normal_close_frames==1);
    if(short_send||failed||cancelled||cancelled_full)assert(stop_faults==1);
    if(cleanup_probe.fragment){
        assert(fragment_starts_before==(cleanup_probe.pre?1U:0U));
        assert(fragment_starts_after==(cleanup_probe.pre?0U:1U)&&fragment_finishes_after==1);
    }
}
/* ==== Red-first regressions: receipt after cleanup does not establish cause ====
 * Hand-written, sanitized literals below match 70df142 gateway_core._shutdown:
 * build_session_closed when close is confirmed, otherwise close_unconfirmed;
 * _device_tail/_final_code close a started, nonfatal provider session with 1000.
 * These literals are NOT captured gateway executions. Joined gateway evidence
 * is owned separately; no fixture metadata is presented as a gateway trace.
 *
 * generated_before_stop/generated_at are EXTERNAL SCRIPT METADATA ONLY. They
 * never enter a wire frame, ticket, callback or production context. In particular
 * an independent peer ending can be generated before pet.stop but first received
 * after admission. The firmware cannot infer cause from that receipt ordering.
 *
 * Unlike the receive companion's synthetic cleanup-state setup, every case here
 * runs the complete owner and capture task: a real partial stream-buffer write
 * causes BV_EXIT_QUEUE; cleanup itself sends pet.stop and session.close in full.
 * No helper assigns an owner/model exit, cleanup flag, terminal or retry result.
 * The original regression IDs are retained; all closing evidence forbids retry.
 */
enum {RACE_UNCONFIRMED,RACE_JSON_CLOSED,RACE_NORMAL_ONLY,RACE_BARE_LOSS};
static struct {
    unsigned shape,stops,ingresses,disposals;
    bool generated_before_stop,with_normal,muted,uncertain,pending,released,retained;
    int64_t generated_at,stop_at,ingress_at,normal_at;
    unsigned appends_at_stop;
} cleanup_race_probe;
static void cleanup_race_generated(void){
    assert(current&&call_no==1&&ws_up&&!transport.local_control);
    assert(cleanup_race_probe.generated_before_stop&&!cleanup_race_probe.generated_at);
    /* Record the scripted peer's independent decision, NOT device ingress. */
    cleanup_race_probe.generated_at=now;
}
static void cleanup_race_late_data(void){
    assert(current&&call_no==1&&transport.local_control&&!guard_held);
    assert(current->cleanup_stop_sent&&current->cleanup_stop_started_at==cleanup_race_probe.stop_at);
    assert(sent_n==4&&!strcmp(sent[2],PET_STOP)&&!strcmp(sent[3],SESSION_CLOSE));
    assert(now>cleanup_race_probe.stop_at&&!cleanup_race_probe.ingresses);
    assert(call_appends==cleanup_race_probe.appends_at_stop);
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    if(cleanup_race_probe.shape!=RACE_BARE_LOSS){
        if(cleanup_race_probe.generated_before_stop)
            assert(cleanup_race_probe.generated_at<cleanup_race_probe.stop_at);
        else{
            assert(!cleanup_race_probe.generated_at);
            cleanup_race_probe.generated_at=now;
        }
    }
    const char *text=cleanup_race_probe.shape==RACE_UNCONFIRMED?CLOSE_UNCONFIRMED:
        cleanup_race_probe.shape==RACE_JSON_CLOSED?CLOSED:NULL;
    if(text){
        unsigned queued=current->frames->n;
        deliver(text);
        assert(current->frames->n==queued+1);
        const fixture_frame_t *f=&current->frames->items[queued];
        assert(!strcmp(f->text,text)&&f->after_local_control);
        assert(f->received_at==now&&f->received_at>cleanup_race_probe.stop_at);
    }
    cleanup_race_probe.ingress_at=now;cleanup_race_probe.ingresses++;
}
static void cleanup_race_normal_received(void){
    assert(current&&call_no==1&&transport.local_control&&cleanup_race_probe.with_normal);
    assert(cleanup_race_probe.ingresses==1&&now>cleanup_race_probe.ingress_at);
    assert(cleanup_race_probe.generated_at&&cleanup_race_probe.generated_at<=cleanup_race_probe.ingress_at);
    assert(!normal_close_frames);
    /* Actual ws_event callback; transport classification is synthetic here.
     * cleanup-race-rx-* exercises the actual pinned parser/transport adapter. */
    event_t e={.kind=EV_CLOSE_1000,.call=call_no};fire(&e);
    assert(normal_close_frames==1);cleanup_race_probe.normal_at=now;
}
static void cleanup_race_stop_sent(void){
    assert(self==0&&current&&call_no==1&&transport.local_control&&!guard_held);
    assert(!cleanup_race_probe.stops&&sent_n==3&&!strcmp(sent[2],PET_STOP));
    assert(atomic_load(&current->failed)&&atomic_load(&current->exit_reason)==BV_EXIT_QUEUE);
    assert(current->model.phase==BR_CLOSING&&current->began_at);
    assert(mic_high==96000&&mic_short_writes==1); /* actual failure under injected probe fault */
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    assert(stop_fault==STOP_FULL&&!stop_faults&&!short_stops);
    if(cleanup_race_probe.generated_before_stop)
        assert(cleanup_race_probe.generated_at&&cleanup_race_probe.generated_at<now);
    cleanup_race_probe.stops++;cleanup_race_probe.stop_at=now;
    cleanup_race_probe.appends_at_stop=call_appends;
    later(10000,EV_CHECK,NULL);script[script_n-1].check=cleanup_race_late_data;
    if(cleanup_race_probe.muted)later(15000,EV_MIC_OFF,NULL);
    if(cleanup_race_probe.with_normal){
        later(20000,EV_CHECK,NULL);script[script_n-1].check=cleanup_race_normal_received;
    }
    later(25000,cleanup_race_probe.shape==RACE_BARE_LOSS?EV_WS_DROP:EV_WS_CLOSED,NULL);
}
static void cleanup_race_disposed(void){
    assert(current&&call_no==1&&transport.cancelled&&!guard_held);
    assert(cleanup_race_probe.stops==1&&cleanup_race_probe.ingresses==1);
    assert(current->cleanup_stop_sent&&call_appends==cleanup_race_probe.appends_at_stop);
    assert(!stop_faults&&!short_stops&&sent_n==4);
    assert(last_state==LIVE_CLOSING&&!shown("Reconnecting..."));
    if(cleanup_race_probe.with_normal)assert(cleanup_race_probe.normal_at>cleanup_race_probe.stop_at);
    cleanup_race_probe.disposals++;
    if(cleanup_race_probe.retained&&cleanup_race_probe.disposals==1)
        dispose_hook=cleanup_race_disposed; /* observe reaper's second proof, no I/O */
}
static void cleanup_race_released(void){
    /* Final tail has run and transport has settled; inspect, never drive it. */
    assert(current&&call_no==1&&!current->transport&&!guard_held);
    assert(cleanup_race_probe.disposals==(cleanup_race_probe.retained?2U:1U)&&current->cleanup_stop_sent);
    assert(last_state==LIVE_CLOSING&&!shown("Reconnecting..."));
    assert(mic_short_writes==1&&mic_high==96000);
    assert(current->send_uncertain_seen==cleanup_race_probe.uncertain);
    assert(current->send_pending==cleanup_race_probe.pending);
    assert(!current->model.confirmed&&current->model.send==BR_SEND_NONE);
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    assert(call_appends==cleanup_race_probe.appends_at_stop);
    cleanup_race_probe.released=true;
}
static void cleanup_race_case(const char *name){
    fresh();opened();host_closes=false;
    cleanup_race_probe=(typeof(cleanup_race_probe)){.shape=RACE_UNCONFIRMED,.with_normal=true};
    bool unconfirmed=!strcmp(name,"cleanup-race-unconfirmed");
    bool independent_unconfirmed=!strcmp(name,"cleanup-race-independent-unconfirmed");
    bool independent_normal=!strcmp(name,"cleanup-race-independent-normal");
    bool json_only=!strcmp(name,"cleanup-race-json-closed");
    bool confirmed=!strcmp(name,"cleanup-race-confirmed");
    bool independent_confirmed=!strcmp(name,"cleanup-race-independent-confirmed");
    bool bare=!strcmp(name,"cleanup-race-bare-loss");
    bool unconfirmed_only=!strcmp(name,"cleanup-race-unconfirmed-only");
    bool retained_json=!strcmp(name,"cleanup-race-retained-json");
    bool retained_normal=!strcmp(name,"cleanup-race-retained-normal");
    bool retained_unconfirmed=!strcmp(name,"cleanup-race-retained-unconfirmed");
    cleanup_race_probe.muted=!strcmp(name,"cleanup-race-muted");
    cleanup_race_probe.uncertain=!strcmp(name,"cleanup-race-uncertain");
    cleanup_race_probe.pending=!strcmp(name,"cleanup-race-pending");
    assert(unconfirmed||unconfirmed_only||retained_json||retained_normal||retained_unconfirmed||
        independent_unconfirmed||independent_normal||json_only||confirmed||
        independent_confirmed||bare||cleanup_race_probe.muted||cleanup_race_probe.uncertain||cleanup_race_probe.pending);
    cleanup_race_probe.retained=retained_json||retained_normal||retained_unconfirmed;
    cleanup_race_probe.generated_before_stop=independent_unconfirmed||independent_normal||json_only||independent_confirmed;
    if(independent_normal||retained_normal)cleanup_race_probe.shape=RACE_NORMAL_ONLY;
    if(json_only||confirmed||independent_confirmed||retained_json)cleanup_race_probe.shape=RACE_JSON_CLOSED;
    if(bare)cleanup_race_probe.shape=RACE_BARE_LOSS;
    cleanup_race_probe.with_normal=!json_only&&!bare&&!unconfirmed_only&&!retained_json&&!retained_unconfirmed;
    if(cleanup_race_probe.generated_before_stop)check(2900,cleanup_race_generated);
    if(cleanup_race_probe.uncertain)at(1000,EV_TEXT,G_NOTICE("send_uncertain"));
    if(cleanup_race_probe.pending){
        at(1000,EV_TEXT,G_STATE("asking"));at(1500,EV_TEXT,G_STATE("listening"));
    }
    queue_fault_stall();
    stop_send_hook=cleanup_race_stop_sent;dispose_hook=cleanup_race_disposed;audio_close_hook=cleanup_race_released;
    /* Bound a forbidden second call so RED is the one-call assertion, not a
     * runaway fake session. This does not grant it: max_calls remains ONE. */
    plans[1]=cleanup_replacement;
    if(bare)max_calls=2; /* positive control: no observed terminal evidence */
    if(cleanup_race_probe.retained)dispose_ok=false;
    call(cleanup_race_probe.retained);
    if(cleanup_race_probe.retained){
        ended(LIVE_CLOSING,"Releasing voice connection...");
        assert(current&&current->retained&&transport_live&&codec_open&&!cleanup_race_probe.released);
        assert(new_calls==1&&!call_report.again&&!shown("Reconnecting..."));
        unsigned frames=frames_total;
        dispose_ok=true;bridge_voice_reap_finished();
        assert(frames_total==frames&&!current&&!codec_open&&!transport_live&&!bridge_voice_restart_required());
    }
    assert(cleanup_race_probe.released&&cleanup_race_probe.stops==1);
    assert(cleanup_race_probe.disposals==(cleanup_race_probe.retained?2U:1U));
    if(bare){
        stopped_cleanly(2);
        assert(new_calls==2&&start_calls==2&&dispose_calls==2&&shown("Reconnecting..."));
    }else{
        assert(new_calls==1&&start_calls==1&&dispose_calls==(cleanup_race_probe.retained?2U:1U)&&!call_report.again);
        assert(opens==1&&closes==1&&tasks_created==2);
        assert((last_state==LIVE_OFF||last_state==LIVE_ERROR)&&!shown("Reconnecting..."));
        if(cleanup_race_probe.muted)ended(LIVE_OFF,"Mic off");
        if(cleanup_race_probe.uncertain||cleanup_race_probe.pending){
            assert(last_state==LIVE_ERROR&&
                (strstr(last_text,"unconfirmed")||!strcmp(last_text,UNCERTAIN_TEXT)));
        }
    }
    assert(normal_close_frames==(cleanup_race_probe.with_normal?1U:0U));
}
/* ==== Observed pending send: ordered state, independent of notice visibility ====
 * Only wire/callback fixtures drive these cases. No helper sets owner pending,
 * uncertainty, exit or retry state. The shared microphone sample oracle still
 * rejects any stale/replayed samples across a permitted fresh connection. */
static struct {
    bool pending,uncertain,inspected,tail_post,tail_resolve,dispose_resolve;
    const char *terminal;
} pending_probe;
static void pending_armed(void){assert(current&&current->send_pending);}
static void pending_settled(void){
    assert(current&&!current->send_pending&&current->send_uncertain_seen==pending_probe.uncertain);
}
static void pending_release_observed(void){
    assert(current&&!current->transport&&!guard_held&&call_no==1);
    assert(current->send_pending==pending_probe.pending);
    assert(current->send_uncertain_seen==pending_probe.uncertain);
    assert(!strcmp(current->terminal_code,pending_probe.terminal));
    assert(!strcmp(current->model.code,pending_probe.terminal));
    assert(!atomic_load(&current->capture_gate)&&!atomic_load(&current->play_gate));
    pending_probe.inspected=true;
}
static void pending_fresh_call(void){
    is_conversing();assert(!current->send_pending&&!current->send_uncertain_seen);
}
static void pending_next_call(void){
    assert(pending_probe.inspected);opened();check(600,pending_fresh_call);at(1500,EV_MIC_OFF,NULL);
}
static void pending_queue_observed(void){
    assert(current&&atomic_load(&current->failed)&&atomic_load(&current->exit_reason)==BV_EXIT_QUEUE);
    assert(current->model.phase==BR_CLOSING&&!current->send_pending&&current->frames->n>=2);
    assert(!strcmp(current->frames->items[0].text,G_STATE("listening")));
    assert(!strcmp(current->frames->items[1].text,G_STATE("asking")));
}
static void pending_control_observed(void){
    assert(current&&!transport.local_control&&atomic_load(&current->exit_reason)==BV_EXIT_QUEUE);
    assert(current->model.phase==BR_CLOSING&&mic_high==96000);
    if(pending_probe.tail_post){
        /* After the first empty tail, before local control: it will only be
         * consumed by the final tail, when the model is already closing. */
        assert(!current->send_pending);deliver(G_STATE("asking"));
    }else assert(current->send_pending); /* asking consumed by first cleanup tail */
    if(pending_probe.tail_resolve){
        deliver(G_STATE("answering"));deliver(G_STATE("listening"));
    }
}
static void pending_disposal_ingress(void){
    assert(current&&transport.cancelled&&!transport.local_control&&!ws_up&&handler);
    assert(!current->send_pending&&atomic_load(&current->accepting));
    /* Already-admitted SDK callbacks settling before final-access proof.
     * Do not revive the socket or use deliver(), which suppresses new traffic. */
    const char *frames[]={G_STATE("asking"),G_STATE("answering"),G_STATE("listening")};
    unsigned n=pending_probe.dispose_resolve?3:1;
    for(unsigned i=0;i<n;i++){
        int bytes=(int)strlen(frames[i]);
        esp_websocket_event_data_t d={.data_len=bytes,.payload_len=bytes,.op_code=1,.fin=true,.data_ptr=frames[i]};
        handler(handler_arg,NULL,WEBSOCKET_EVENT_DATA,&d);
    }
    assert(!current->send_pending&&current->frames->n==n); /* owner has not consumed */
}
static void pending_send_case(const char *name){
    fresh();opened();
    pending_probe=(typeof(pending_probe)){.pending=true,.terminal=""};
    bool eof=!strcmp(name,"pending-eof"),callback=!strcmp(name,"pending-callback");
    bool close1011=!strcmp(name,"pending-1011"),closed=!strcmp(name,"pending-close");
    bool notice=!strcmp(name,"pending-notice"),listening=!strcmp(name,"pending-listening");
    bool unknown=!strcmp(name,"pending-unknown"),muted=!strcmp(name,"pending-muted");
    bool resolved=!strcmp(name,"pending-resolved"),resolved_listening=!strcmp(name,"pending-resolved-listening");
    bool reasking=!strcmp(name,"pending-reasking"),uncertain=!strcmp(name,"pending-uncertain-resolved");
    bool terminal=!strcmp(name,"pending-terminal"),queued=!strcmp(name,"pending-tail-queued");
    bool disposal=!strcmp(name,"pending-dispose")||!strcmp(name,"pending-dispose-resolved");
    pending_probe.tail_post=!strcmp(name,"pending-tail-post");
    pending_probe.tail_resolve=!strcmp(name,"pending-tail-resolved");
    pending_probe.dispose_resolve=!strcmp(name,"pending-dispose-resolved");
    assert(eof||callback||close1011||closed||notice||listening||unknown||muted||resolved||resolved_listening||
        reasking||uncertain||terminal||queued||disposal||pending_probe.tail_post||pending_probe.tail_resolve);
    bool settled=resolved||resolved_listening||pending_probe.tail_resolve||pending_probe.dispose_resolve;
    /* tail-resolved clears pending, but cleanup's valid session.closed still
     * ends eligibility. Resolved bare-loss controls continue to replace. */
    bool replace=settled&&!pending_probe.tail_resolve;
    pending_probe.pending=!settled&&!uncertain;pending_probe.uncertain=uncertain;
    if(terminal)pending_probe.terminal="no_delegation";
    audio_close_hook=pending_release_observed;plans[1]=pending_next_call;
    if(replace)max_calls=2;
    if(queued||pending_probe.tail_post||pending_probe.tail_resolve){
        queue_fault_stall();
        before_control_hook=pending_control_observed;
        if(queued){
            /* The owner returns from its stalled queue receive with the neutral
             * first frame; asking remains for the closing tail, not main loop. */
            at(5290,EV_TEXT,G_STATE("listening"));at(5300,EV_TEXT,G_STATE("asking"));
            check(5400,pending_queue_observed);
        }else if(pending_probe.tail_resolve){at(1000,EV_TEXT,G_STATE("asking"));check(1100,pending_armed);}
    }else if(disposal){
        host_audio(100);
        at(2000,EV_WS_DROP,NULL);dispose_hook=pending_disposal_ingress;
    }else{
        host_audio(100);
        at(1000,EV_TEXT,G_STATE("asking"));check(1100,pending_armed);
        if(resolved||resolved_listening||reasking||uncertain){
            if(uncertain)at(1200,EV_TEXT,G_NOTICE("send_uncertain"));
            at(1400,EV_TEXT,G_STATE("answering"));check(1500,pending_settled);
            if(resolved_listening||reasking||uncertain)at(1600,EV_TEXT,G_STATE("listening"));
            if(reasking){at(1700,EV_TEXT,G_STATE("asking"));check(1800,pending_armed);}
        }
        if(listening){at(1500,EV_TEXT,G_STATE("listening"));check(1800,pending_armed);}
        if(unknown){
            at(1400,EV_TEXT,G_STATE("dreaming"));at(1500,EV_TEXT,G_NOTICE("brand_new"));
            at(1600,EV_TEXT,G_NOTICE("not_sent"));check(1800,pending_armed);
        }
        if(terminal)at(1900,EV_TEXT,NO_DELEGATION);
        else if(muted)at(2000,EV_MIC_OFF,NULL);
        else if(closed||notice){
            if(notice)at(1900,EV_TEXT,G_NOTICE("idle_timeout"));
            at(2000,EV_TEXT,CLOSED);at(2010,EV_WS_CLOSED,NULL);
        }else if(close1011){at(2000,EV_CLOSE_1011,NULL);at(2010,EV_WS_DROP,NULL);}
        else at(2000,callback?EV_WS_ERROR:EV_WS_DROP,NULL);
    }
    call(false);assert(pending_probe.inspected);
    if(replace){
        stopped_cleanly(2);
        assert(new_calls==2&&opens==2&&closes==2&&tasks_created==4&&dispose_calls==2&&shown("Reconnecting..."));
    }else{
        assert(new_calls==1&&start_calls==1&&!call_report.again);
        ended(muted||pending_probe.tail_resolve?LIVE_OFF:LIVE_ERROR,
            muted?"Mic off":pending_probe.tail_resolve?"Mic off (call ended)":terminal?"Host error: no_delegation":
            uncertain?UNCERTAIN_TEXT:PENDING_TEXT);
    }
    /* fresh() resets fixtures only, not production statics: a new explicit user
     * activation is immediately usable and cannot inherit a prior call's guard. */
    explicit_start_again();
}
static void re_first(const char *name){
    /* The very first connection after the tap fails; the mic stays on and the
     * pet tries again. */
    fresh();host_audio(100);max_calls=2;plans[1]=plan_again;
    if(!strcmp(name,"re-first"))plans[0]=plan_refused;
    else if(!strcmp(name,"re-first-503")){plans[0]=plan_http;plan_status=503;}
    else if(!strcmp(name,"re-first-timeout"))plans[0]=plan_hang;
    else if(!strcmp(name,"re-error-busy")){at(40,EV_CONNECT,NULL);at(60,EV_TEXT,STARTED);at(80,EV_TEXT,"{\"type\":\"pet.error\",\"code\":\"busy\"}");at(90,EV_WS_CLOSED,NULL);}
    else{assert(!strcmp(name,"re-no-begin"));plans[0]=plan_only_connect;} /* connected, but the conversation never begins */
    if(!strcmp(name,"re-error-busy")){
        call(false);ended(LIVE_ERROR,"Host error: busy");
        assert(new_calls==1&&!shown("Reconnecting..."));return;
    }
    if(!strcmp(name,"re-no-begin")){
        /* Begin timed out, but this peer explicitly closes during cleanup. */
        max_calls=1;call(false);ended(LIVE_OFF,"Mic off (call ended)");
        assert(new_calls==1&&!opens&&frames_total==3&&!shown("Reconnecting..."));return;
    }
    call(false);reconnect_report(name);stopped_cleanly(2);
    assert(new_calls==2&&opens==1&&shown("Connecting...")&&shown("Reconnecting...")&&shown("Listening"));
    int64_t first=!strcmp(name,"re-first-timeout")?25000:50;
    assert(gap_ms(1)>=first+2000&&gap_ms(1)<=first+2300);
    for(unsigned i=0;i<history_n;i++)assert(history[i].state!=LIVE_ERROR);
}
static void re_busy(void){
    /* The host's one slot is still held by the call that just dropped (HTTP
     * 409, for up to about 15 s): keep trying, it clears. */
    fresh();opened();host_audio(100);at(3000,EV_WS_DROP,NULL);
    max_calls=4;plans[1]=plans[2]=plan_http;plan_status=409;plans[3]=plan_again;
    call(false);reconnect_report("re-busy");stopped_cleanly(2);
    assert(new_calls==4&&opens==2&&frames_total==6);gaps_follow(2,1,2,50);
    assert(call_at[4]-(run_start+3000000)>=17000000&&call_at[4]-(run_start+3000000)<=17500000);
}
static void re_give_up(const char *name){
    /* The host stays away: six attempts over 82 s of waiting, then one honest
     * status and mic off. Nothing is held; the next tap works. When every
     * attempt hangs for its full 25 s instead, no attempt starts more than
     * 120 s after the loss was noticed, so it ends after four. */
    bool first=!strcmp(name,"re-give-up-first"),slow=!strcmp(name,"re-give-up-timeout");
    fresh();max_calls=7;plan_rest=slow?plan_hang:plan_refused;
    if(first||slow)plans[0]=plan_rest;else{opened();host_audio(100);at(5000,EV_WS_DROP,NULL);}
    call(false);reconnect_report(name);
    ended(LIVE_ERROR,first||slow?"Could not reach the host - mic off":"Connection lost - mic off");
    assert(new_calls==(slow?5U:7U)&&frames_total==(first||slow?0U:2U)&&opens==(first||slow?0U:1U));
    gaps_follow(2,1,slow?3:5,slow?25000:50);
    int64_t tail=run_end-call_at[2];
    if(slow){
        int64_t noticed=call_at[1]+25000000;
        assert(call_at[5]-noticed<=120000000&&run_end-noticed>=132000000&&run_end-noticed<=133000000);
    }else assert(tail>=300000+80000000&&tail<=300000+80600000);
    if(!first&&!slow)assert(run_end-(run_start+5000000)>=82000000&&run_end-(run_start+5000000)<=83000000);
    /* reusable at once, with no reboot */
    fresh();opened();host_audio(100);at(1500,EV_MIC_OFF,NULL);call(false);stopped_cleanly(2);
}
static void re_off(const char *name){
    /* A tap while reconnecting cancels at once. */
    fresh();opened();host_audio(100);at(3000,EV_WS_DROP,NULL);
    if(!strcmp(name,"re-off-wait")){
        /* in the third wait (10 s long) */
        max_calls=3;plan_rest=plan_refused;check(12000,waiting);check(12900,still_quiet);at(13000,EV_MIC_OFF,NULL);
        call(false);ended(LIVE_OFF,"Mic off");
        assert(new_calls==3&&run_end-mic_off_at<=25000&&frames_total==2);
    }else if(!strcmp(name,"re-off-connect")){
        /* during a connect attempt that is getting no answer */
        max_calls=2;plans[1]=plan_hang;check(5500,reconnecting);at(6000,EV_MIC_OFF,NULL);
        call(false);ended(LIVE_OFF,"Mic off");
        assert(new_calls==2&&sent_n==0&&run_end-mic_off_at<=100000&&dispose_calls==2);
    }else if(!strcmp(name,"re-off-begin")){
        /* connected again, before the conversation began: the new call is told to stop */
        max_calls=2;plans[1]=plan_only_connect;at(6000,EV_MIC_OFF,NULL);
        call(false);stopped_cleanly(1);assert(new_calls==2&&opens==1);
    }else{
        /* the tap and the drop in the same instant, in either order */
        assert(!strcmp(name,"re-off-instant"));
        at(3000,EV_MIC_OFF,NULL);call(false);ended(LIVE_OFF,"Mic off");assert(new_calls==1);
        fresh();opened();host_audio(100);at(3000,EV_MIC_OFF,NULL);at(3000,EV_WS_DROP,NULL);
        call(false);ended(LIVE_OFF,"Mic off");assert(new_calls==1);
        /* the tap lands while the dropped call is still being released */
        fresh();opened();host_audio(100);at(3000,EV_WS_DROP,NULL);at(3010,EV_MIC_OFF,NULL);
        call(false);ended(LIVE_OFF,"Mic off");assert(new_calls==1);
    }
    assert(!shown("Connection lost - mic off"));
    for(unsigned i=0;i<history_n;i++)assert(history[i].state!=LIVE_ERROR);
    /* and nothing reconnects afterwards: time passes, no connection is made */
    unsigned made=new_calls;now+=120000000;assert(new_calls==made&&current==NULL&&!transport_live);
}
static bool refusal_mute_first;
static void plan_http_muted(void){
    if(refusal_mute_first)at(50,EV_MIC_OFF,NULL);
    http_at(50,423);
    if(!refusal_mute_first)at(50,EV_MIC_OFF,NULL);
}
static void no_http(void){
    /* The host answered and refused this pet: trying again cannot help (and
     * repeated 401s would only lock the pet out). First connect or reconnect. */
    static const struct {int status;const char *text;} refused[]={
        {401,"Host refused this pet. Check its token."},{403,"Host refused this pet. Check its token."},
        {404,"Host refused the connection (HTTP 404)."},{400,"Host refused the connection (HTTP 400)."},
        {429,"Host refused the connection (HTTP 429)."},{421,"Host refused the connection (HTTP 421)."},
        /* Compatibility only; do not infer a prior-request message before the
         * gateway's 423 fence contract is frozen. Keep the current generic UI. */
        {423,"Host refused the connection (HTTP 423)."}};
    for(unsigned i=0;i<sizeof(refused)/sizeof(*refused);i++){
        fresh();plans[0]=plan_http;plan_status=refused[i].status;
        call(false);ended(LIVE_ERROR,refused[i].text);
        assert(new_calls==1&&start_calls==1&&!sent_n&&!opens&&!call_report.again&&
            !shown("Reconnecting...")&&run_end-run_start<1000000);
        fresh();opened();host_audio(100);at(3000,EV_WS_DROP,NULL);max_calls=2;plans[1]=plan_http;
        call(false);ended(LIVE_ERROR,refused[i].text);
        assert(new_calls==2&&start_calls==2&&opens==1&&!sent_n&&!call_report.again&&shown("Reconnecting..."));
    }
    /* Explicit mute wins when refusal and mic-off share a scheduler tick, in
     * both event orders and on both first connect and a replacement attempt.
     * EV_HTTP injects status only here; actual HTTP bytes are transport-tested. */
    for(unsigned stage=0;stage<2;stage++)for(unsigned order=0;order<2;order++){
        fresh();refusal_mute_first=order!=0;
        if(stage){opened();at(3000,EV_WS_DROP,NULL);max_calls=2;plans[1]=plan_http_muted;}
        else plans[0]=plan_http_muted;
        call(false);ended(LIVE_OFF,"Mic off");
        assert(new_calls==stage+1&&start_calls==stage+1&&opens==stage&&!sent_n&&!call_report.again);
        assert(transport.http==423&&!transport.upgraded&&run_end-mic_off_at<=100000);
        if(!stage)assert(!shown("Reconnecting..."));
    }
}
static void no_retained(void){
    /* The dropped call's release could not be proven: nothing is freed, and
     * no new call is made until that is resolved. */
    fresh();opened();host_audio(100);at(3000,EV_WS_DROP,NULL);dispose_ok=false;
    call(true);ended(LIVE_CLOSING,"Releasing voice connection...");
    assert(new_calls==1&&current&&bridge_voice_restart_required()&&codec_open&&transport_live);
    now+=200000000;assert(new_calls==1);
    fresh();call(true);ended(LIVE_ERROR,"Voice cleanup incomplete. Connection held.");assert(!new_calls);
    /* Proof arrives later: released by the reaper, which never calls again. */
    dispose_ok=true;bridge_voice_reap_finished();
    assert(current==NULL&&!bridge_voice_restart_required()&&!codec_open&&!transport_live&&!new_calls);
    ended(LIVE_ERROR,"Host disconnected.");
    fresh();opened();host_audio(100);at(1000,EV_MIC_OFF,NULL);call(false);stopped_cleanly(2);
}
static void re_budget(const char *name){
    /* Attempts are counted in a row. A reconnected call that conversed for at
     * least 30 s clears the count; a shorter one does not. */
    bool healthy=!strcmp(name,"re-budget");
    fresh();opened();host_audio(100);at(5000,EV_WS_DROP,NULL);
    plans[1]=plans[2]=plans[3]=plan_refused;plans[4]=healthy?plan_ok_drop31:plan_ok_drop10;plan_rest=plan_refused;
    max_calls=healthy?11:7;
    call(false);reconnect_report(name);ended(LIVE_ERROR,"Connection lost - mic off");
    gaps_follow(2,1,3,50);
    if(healthy){
        assert(new_calls==11);
        assert(gap_ms(5)>=31000+2000&&gap_ms(5)<=31000+2300); /* the first delay again */
        gaps_follow(6,1,5,50);
    }else{
        assert(new_calls==7);
        assert(gap_ms(5)>=10000+20000&&gap_ms(5)<=10000+20300); /* the schedule simply continues */
        gaps_follow(6,5,1,50);
    }
}
static void re_budget_cleanup(const char *name){
    bool healthy=!strcmp(name,"re-budget-long-cleanup");
    fresh();opened();host_audio(100);at(5000,EV_WS_DROP,NULL);
    plans[1]=plans[2]=plans[3]=plan_refused;plans[4]=healthy?plan_ok_drop31:plan_ok_drop20;plan_rest=plan_refused;
    dispose_delay_call=5;dispose_delay_us=12000000;max_calls=healthy?11:7;
    call(false);reconnect_report(name);ended(LIVE_ERROR,"Connection lost - mic off");
    assert(new_calls==(healthy?11U:7U));
    int64_t expected=(healthy?31000+12000+2000:20000+12000+20000);
    assert(gap_ms(5)>=expected&&gap_ms(5)<=expected+300);
}
static void re_window_origin(void){
    /* A slow release consumes the same 120 s window as connection attempts.
     * With 14 s cleanup and repeated 25 s failed attempts, the fourth retry
     * would start after the original loss window and must not be attempted. */
    fresh();opened();at(5000,EV_WS_DROP,NULL);
    dispose_delay_call=1;dispose_delay_us=14000000;plan_rest=plan_hang;max_calls=4;
    call(false);ended(LIVE_ERROR,"Connection lost - mic off");
    assert(new_calls==4&&start_calls==4&&call_at[4]-(run_start+5000000)<=BV_RETRY_WINDOW_US);
    assert(run_end-(run_start+5000000)<BV_RETRY_WINDOW_US);

    /* The clock can advance while the amplifier is being re-armed. */
    fresh();opened();at(5000,EV_WS_DROP,NULL);retry_prepare_delay_us=BV_RETRY_WINDOW_US;
    call(false);ended(LIVE_ERROR,"Connection lost - mic off");
    assert(new_calls==1&&start_calls==1&&prepares==1&&amp_latched_off);

    /* A fresh chain's setup may itself consume the remaining window; it may
     * be allocated but the paid/network start must not be admitted afterward. */
    fresh();opened();at(5000,EV_WS_DROP,NULL);max_calls=2;
    registration_delay_us=BV_RETRY_WINDOW_US;
    call(false);ended(LIVE_ERROR,"Connection lost - mic off");
    assert(new_calls==2&&start_calls==1&&prepares==1&&amp_latched_off);

    /* The cutoff is rechecked after timer setup, immediately before the SDK
     * start, so time spent in that admission step cannot open a late call. */
    fresh();opened();at(5000,EV_WS_DROP,NULL);max_calls=2;
    retry_start_delay_us=BV_RETRY_WINDOW_US;
    call(false);ended(LIVE_ERROR,"Connection lost - mic off");
    assert(new_calls==2&&start_calls==1&&prepares==1&&amp_latched_off);
}
static void delay_next_message(void){delay_message_alloc=true;}
static void plan_callback_threshold(void){
    opened();
    /* Started conversing at ~210ms, so 30.100s is below 30 active seconds.
     * Allocation stalls the callback past the threshold before queueing. */
    check(30099,delay_next_message);
    at(30100,EV_TEXT,"{\"type\":\"pet.error\",\"code\":\"busy\"}");
}
static void re_budget_callback_delay(void){
    fresh();opened();host_audio(100);at(5000,EV_WS_DROP,NULL);
    plans[1]=plans[2]=plans[3]=plan_refused;plans[4]=plan_callback_threshold;plan_rest=plan_refused;
    max_calls=5;call(false);ended(LIVE_ERROR,"Host error: busy");
    assert(new_calls==5&&!delay_message_alloc&&!call_report.again);
}
static void plan_close_threshold(void){
    opened();at(30100,EV_RAW_CLOSE,NULL);at(30700,EV_WS_CLOSED,NULL);
}
static void re_budget_close_delay(void){
    fresh();opened();host_audio(100);at(5000,EV_WS_DROP,NULL);
    plans[1]=plans[2]=plans[3]=plan_refused;plans[4]=plan_close_threshold;plan_rest=plan_refused;
    max_calls=7;call(false);ended(LIVE_ERROR,"Connection lost - mic off");
    assert(new_calls==7&&gap_ms(5)>=50700&&gap_ms(5)<=51000);
}
static void re_ceiling(void){
    /* A link that keeps coming back for half a minute and dropping again: every
     * reconnect is a new paid call, so one mic-on makes at most 20 of them. */
    fresh();opened();host_audio(100);at(31000,EV_WS_DROP,NULL);plan_rest=plan_ok_drop31;max_calls=21;
    call(false);reconnect_report("re-ceiling");ended(LIVE_ERROR,"Connection lost - mic off");
    assert(new_calls==21&&opens==21&&closes==21&&frames_total==42);
    for(unsigned k=1;k<=20;k++)assert(gap_ms(k)>=31000+2000&&gap_ms(k)<=31000+2300);
    assert(mic_sent>(uint64_t)48000*21*30);
}
static void re_no_stale(void){
    /* 1.5 s of microphone audio is waiting, unsent, when the connection drops.
     * None of it is sent on the new call: that call sends only what its own
     * codec session captured (every sample carries its connection's stamp). */
    fresh();opened();host_audio(100);owner_stall_at=run_start+3000000;owner_stall_us=1500000;
    check(4400,backlog_seen);at(4450,EV_WS_DROP,NULL);max_calls=2;plans[1]=plan_again;
    call(false);reconnect_report("re-no-stale");stopped_cleanly(2);
    assert(new_calls==2&&mic_high>=60000);
    uint64_t never_sent=mic_samples*2-mic_sent-2*9600; /* captured, less each connection's discarded start */
    assert(never_sent>=60000);
    printf("re-no-stale: captured=%llu sent=%llu never_sent=%llu waiting_at_drop_high=%zu\n",
        (unsigned long long)(mic_samples*2),(unsigned long long)mic_sent,(unsigned long long)never_sent,mic_high);
}
static void sequence(void){
    /* One process, no reset of the firmware's state: each mic-on follows an
     * earlier one that ended a different way. */
    for(unsigned round=0;round<2;round++){
        fresh();opened();host_audio(100);at(1500,EV_MIC_OFF,NULL);call(false);stopped_cleanly(2);
        fresh();opened();host_audio(100);at(1500,EV_TEXT,G_NOTICE("time_limit"));at(1600,EV_TEXT,CLOSED);
        at(1610,EV_WS_CLOSED,NULL);call(false);ended(LIVE_OFF,"Mic off (time limit)");
        fresh();opened();at(900,EV_TEXT,"{\"type\":\"pet.error\",\"code\":\"model_mismatch\"}");call(false);
        ended(LIVE_ERROR,"Host error: model_mismatch");
        fresh();opened();host_audio(100);at(900,EV_TEXT,"{\"type\":\"pet.converse.state\"}");call(false);
        ended(LIVE_ERROR,"Host sent an invalid message.");
        fresh();opened();host_audio(100);at(900,EV_WS_ERROR,NULL);max_calls=7;plan_rest=plan_refused;call(false);
        ended(LIVE_ERROR,"Connection lost - mic off");
        fresh();opened();host_audio(100);at(900,EV_WS_ERROR,NULL);max_calls=2;plans[1]=plan_again;call(false);stopped_cleanly(2);
        fresh();plans[0]=plan_refused;plan_rest=plan_refused;max_calls=7;call(false);
        ended(LIVE_ERROR,"Could not reach the host - mic off");
        fresh();opened();host_audio(100);mic_fail_at=60;call(false);ended(LIVE_ERROR,"Microphone read failed.");
    }
    normal();
}
int main(int argc,char **argv){
    setbuf(stdout,NULL);assert(argc==2);const char *name=argv[1];
    printf("CASE %s (whole production owner + audio tasks; synthetic clock/RTOS/codec/transport/host)\n",name);
    bridge_voice_init();
#ifdef BRIDGE_JOIN_REAL_PARENT
    (void)parent_stopped;
    status_lock=xSemaphoreCreateMutex();assert(status_lock&&status_lock!=guard);
    if(!strcmp(name,"joined-gate-owner-wins")||!strcmp(name,"joined-gate-stop-wins")){
        joined_gate_order(name);printf("PASS %s\n",name);return 0;
    }
    fprintf(stderr,"joined build accepts only joined-gate cases\n");return 2;
#endif
    (void)plan_ok;
    if(!strcmp(name,"stop-registration"))stop_during_registration();
    else if(!strcmp(name,"stop-reconnect-registration"))reconnect_stop_during_registration();
    else if(!strcmp(name,"stop-before-active"))stop_before_active();
    else if(!strcmp(name,"stop-audio-arm"))stop_during_audio_start();
    else if(!strcmp(name,"stop-parent-audio-arm"))parent_stop_during_audio_arm();
    else if(!strcmp(name,"stop-start-send-admitted"))stop_after_start_send_admission();
    else if(!strcmp(name,"stop-capture-guard"))stop_while_capture_waits_guard();
    else if(!strcmp(name,"stop-guard-timeout"))stop_guard_timeout();
    else if(!strcmp(name,"stop-audio-work-deadline"))stop_audio_work_deadline();
    else if(!strcmp(name,"stop-codec-work-deadline"))stop_codec_work_deadline();
    else if(!strcmp(name,"open-success-stop")||!strcmp(name,"open-success-cutoff")||
            !strcmp(name,"open-success-control"))successful_inflight_open(name);
    else if(!strcmp(name,"normal"))normal();
    else if(!strncmp(name,"off-",4))off_stage(name);
    else if(!strcmp(name,"host-quiet")||!strcmp(name,"host-time-limit")||!strcmp(name,"host-burst-error"))host_end(name);
    else if(!strncmp(name,"notice-",7))host_end_race(name);
    else if(!strcmp(name,"terminal-wire"))terminal_wire();
    else if(!strncmp(name,"cert-",5))cert_failure(name);
    else if(!strcmp(name,"malformed-tail"))malformed_tail();
    else if(!strcmp(name,"sdk-event-loss"))sdk_event_loss();
    else if(!strcmp(name,"reviewed-races"))reviewed_races();
    else if(!strcmp(name,"no-error"))host_error(name);
    else if(!strcmp(name,"malformed")||!strcmp(name,"turn-frame")||!strcmp(name,"unknown-type"))malformed(name);
    else if(!strcmp(name,"deadline"))timeouts(name);
    else if(!strcmp(name,"burst-ok"))burst_ok();
    else if(!strcmp(name,"gappy"))gappy();
    else if(!strcmp(name,"lag"))lag();
    else if(!strcmp(name,"retained"))retained();
    else if(!strcmp(name,"late-600"))late_call(name,8,20);
    else if(!strcmp(name,"late-600-heavy"))late_call(name,20,40);
    else if(!strcmp(name,"recover"))recover();
    else if(!strcmp(name,"phrase-control"))phrase_comparison(0);
    else if(!strcmp(name,"phrase-stall"))phrase_comparison(2600);
    else if(!strcmp(name,"phrase-stall-radio"))phrase_comparison(3336);
    else if(!strcmp(name,"send-wait"))send_wait();
    else if(!strcmp(name,"send-wait-off")||!strcmp(name,"send-wait-off-lost"))send_wait_off(name);
    else if(!strcmp(name,"sequence"))sequence();
    else if(!strcmp(name,"audio-open")||!strcmp(name,"mic-read")||!strcmp(name,"speaker-write")||
            !strcmp(name,"setup-token")||!strcmp(name,"setup-start"))failures(name);
    else if(!strcmp(name,"re-listening")||!strcmp(name,"re-asking")||!strcmp(name,"re-answering")||!strcmp(name,"re-close")||
            !strcmp(name,"re-error")||!strcmp(name,"re-flood")||!strcmp(name,"re-send")||!strcmp(name,"re-stall")||
            !strcmp(name,"re-stall-long")||!strcmp(name,"re-send-wait-long"))re_drop(name);
    else if(!strcmp(name,"re-first")||!strcmp(name,"re-first-503")||!strcmp(name,"re-first-timeout")||
            !strcmp(name,"re-error-busy")||!strcmp(name,"re-no-begin"))re_first(name);
    else if(!strcmp(name,"re-busy"))re_busy();
    else if(!strcmp(name,"retry-boundaries"))retry_boundaries();
    else if(!strncmp(name,"terminal-error-",15)||!strcmp(name,"terminal-local-close")||
            !strcmp(name,"terminal-local-normal"))terminal_error_cleanup(name);
    else if(!strncmp(name,"uncertain-",10))uncertain_ending(name);
    else if(!strcmp(name,"ending-controls"))ending_controls();
    else if(!strncmp(name,"cleanup-unconfirmed-",20))cleanup_unconfirmed_case(name);
    else if(!strncmp(name,"cleanup-race-",13))cleanup_race_case(name);
    else if(!strncmp(name,"pending-",8))pending_send_case(name);
    else if(!strncmp(name,"re-give-up",10))re_give_up(name);
    else if(!strncmp(name,"re-off-",7))re_off(name);
    else if(!strcmp(name,"no-http"))no_http();
    else if(!strcmp(name,"no-retained"))no_retained();
    else if(!strcmp(name,"re-budget")||!strcmp(name,"re-budget-short"))re_budget(name);
    else if(!strcmp(name,"re-budget-long-cleanup")||!strcmp(name,"re-budget-short-cleanup"))re_budget_cleanup(name);
    else if(!strcmp(name,"re-window-origin"))re_window_origin();
    else if(!strcmp(name,"terminal-code-matrix"))terminal_code_matrix();
    else if(!strcmp(name,"re-budget-callback-delay"))re_budget_callback_delay();
    else if(!strcmp(name,"re-budget-close-delay"))re_budget_close_delay();
    else if(!strcmp(name,"re-ceiling"))re_ceiling();
    else if(!strcmp(name,"re-no-stale"))re_no_stale();
    else{fprintf(stderr,"unknown case: %s\n",name);return 2;}
    printf("PASS %s\n",name);return 0;
}
