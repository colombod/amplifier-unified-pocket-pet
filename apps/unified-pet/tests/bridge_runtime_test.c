/* Runtime policy is extracted verbatim by run_bridge_native.sh. New A5 cases
 * execute the actual owner capture/send block, not a replacement pacing policy.
 * Clocks/20ms producer/buffer/transport are fixtures, NOT real codec/RTOS proof.
 * The actual capture_task body has separate overflow/in-flight-read cases. */
#include <assert.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "bridge_protocol.h"
#include "live_protocol.h"
#include "mbedtls/base64.h"
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_ERR_INVALID_STATE 5
#define pdTRUE 1
#define pdFALSE 0
#define pdMS_TO_TICKS(x) (x)
#define BIT0 1
#define BIT1 2
#define BIT2 4
#define BIT3 8
#define BV_TEST_CAP 96000
#define TEST_WIRE_CAP 4096
#define A5_PCM_CAP 3000000
#define STREAM_CANARY UINT64_C(0x2ca78194bd06ef53)
#define TURN "123e4567-e89b-42d3-a456-426614174000"
typedef unsigned EventBits_t;
typedef struct {unsigned bits;} *EventGroupHandle_t;
typedef struct test_stream {
    uint64_t before;
    unsigned char data[BV_TEST_CAP];
    uint64_t after;
    size_t n;
} *StreamBufferHandle_t;
typedef struct {unsigned unused;} StaticStreamBuffer_t;
typedef void *QueueHandle_t;
typedef void *SemaphoreHandle_t;
typedef void *esp_websocket_client_handle_t;
typedef struct {unsigned unused;} audio_session_t;
typedef struct {bool upgraded,cancelled,finished;} bridge_transport_t;
typedef enum {BR_TRANSPORT_NONE,BR_TRANSPORT_PROTOCOL,BR_TRANSPORT_POLICY,BR_TRANSPORT_EVENT_LOSS,BR_TRANSPORT_PEER_NORMAL,BR_TRANSPORT_UNSUPPORTED,BR_TRANSPORT_PAYLOAD_REJECTED,BR_TRANSPORT_CERT_VERIFY,BR_TRANSPORT_TLS_UNKNOWN} bridge_transport_terminal_t;
typedef const char *esp_event_base_t;
typedef struct {int data_len,payload_len,payload_offset,op_code;bool fin;const char *data_ptr;} esp_websocket_event_data_t;
#define WEBSOCKET_EVENT_CONNECTED 1
#define WEBSOCKET_EVENT_DISCONNECTED 2
#define WEBSOCKET_EVENT_CLOSED 3
#define WEBSOCKET_EVENT_ERROR 4
#define WEBSOCKET_EVENT_DATA 5
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
static unsigned reads,writes,inhibits,sends,wait_ms,delay_calls,wire_bytes,played;
static bool lock_ok=true,stop_on_take=false,expire_send=false,end_on_delay=false;
static bool queue_on_delay=false;
static bool cleanup_transport_ok=true,cleanup_final_on_delay,cleanup_mode;
static bridge_transport_terminal_t cleanup_terminal;
static unsigned codec_closes,deleted_queues,deleted_streams,deleted_groups,dispose_calls;
static int64_t dispose_deadline;
static char last_status[112];
static int64_t now=1000000;
static char wire[TEST_WIRE_CAP][7000];
typedef struct {char *text;int64_t received_at;bool after_local_control;} fixture_frame_t;
static fixture_frame_t received[4];
static unsigned received_n;
static unsigned char played_bytes[32000];
/* All A5 state is inert during the unchanged legacy main body. */
static bool a5_enabled,a5_producer,a5_overflow,a5_timed_read,a5_fast_read;
static bool a5_jitter,a5_owner_jitter,a5_alternating,a5_parent_stop,model_lock_held;
static unsigned a5_return_mode,a5_encode_before,a5_encode_after;
static unsigned a5_lock_calls,a5_lock_at,a5_lock_us,a5_read_failure;
static unsigned a5_stall_tick,a5_stop_after_appends;
static unsigned a5_send_us,a5_ticks,a5_appends,a5_finishes,a5_cadence_late,a5_burst;
static int64_t a5_base,a5_until,a5_produce_until,a5_produce_next;
static int64_t a5_last_start,a5_last_return,a5_last_due,a5_finish_at;
static int64_t a5_first_start;
/* max_k(S(k)-C(k)), where C(k) is duration BEFORE wire append k.
 * This is an independent all-suffix oracle, not the production cursor rule. */
static int64_t a5_frontier;
static size_t a5_produced,a5_consumed_n,a5_wire_pcm,a5_highwater,a5_last_n,a5_last_pending,a5_read_bytes;
static unsigned char a5_consumed[A5_PCM_CAP];
static StreamBufferHandle_t a5_mic;
enum a5_event { A5_EVENT_NONE,A5_EVENT_STOP,A5_EVENT_FAILURE,A5_EVENT_PARENT,A5_EVENT_FAILED_FLAG };
static enum a5_event a5_event;
static int64_t a5_event_at;
static unsigned a5_event_sends;
static void a5_advance_us(int64_t us);
static bool a5_owner_tick(void);
static void a5_observe_append(const char *s,size_t n,unsigned timeout_ms);
static void a5_observe_finish(void);
static void a5_stop_on_lock(void);
static bool a5_read(void *p,unsigned n);
static unsigned char a5_byte(size_t i){return (unsigned char)(i*37u+(i>>8)*13u+(i>>16)*7u);}
static void stream_check(StreamBufferHandle_t s){
    assert(s&&s->before==STREAM_CANARY&&s->after==STREAM_CANARY&&s->n<=BV_TEST_CAP);
}
static void *heap_caps_malloc(size_t n,unsigned flags){(void)flags;return malloc(n);}
static int xQueueSend(QueueHandle_t q,const void *p,unsigned ms){(void)q;(void)ms;if(received_n==4)return 0;memcpy(&received[received_n++],p,sizeof(received[0]));return pdTRUE;}
static unsigned uxQueueMessagesWaiting(QueueHandle_t q){(void)q;return received_n;}
static int xQueueReceive(QueueHandle_t q,void *out,unsigned ms){
    (void)q;(void)ms;if(!received_n)return pdFALSE;
    memcpy(out,&received[0],sizeof(received[0]));memmove(received,received+1,--received_n*sizeof(*received));return pdTRUE;
}
static void vQueueDelete(QueueHandle_t q){assert(!model_lock_held&&!received_n);deleted_queues++;free(q);}
static void vStreamBufferDelete(StreamBufferHandle_t s){assert(!model_lock_held);deleted_streams++;free(s);}
static void vEventGroupDelete(EventGroupHandle_t e){assert(!model_lock_held);deleted_groups++;free(e);}
typedef int live_state_t;
enum {LIVE_ERROR=1,LIVE_OFF=2,LIVE_CLOSING=3,LIVE_RECONNECTING=4};
static void live_voice_bridge_status(live_state_t s,const char *text){(void)s;snprintf(last_status,sizeof(last_status),"%s",text);}
static void *selected;
static int xSemaphoreTake(SemaphoreHandle_t h,unsigned ms){
    (void)h;(void)ms;
    if(stop_on_take)a5_stop_on_lock();
    if(a5_enabled&&++a5_lock_calls==a5_lock_at){
        unsigned us=a5_lock_us;a5_lock_at=0;a5_advance_us(us);
    }
    if(!lock_ok)return pdFALSE;
    assert(!model_lock_held);model_lock_held=true;return pdTRUE;
}
static void xSemaphoreGive(SemaphoreHandle_t h){(void)h;assert(model_lock_held);model_lock_held=false;}
static int64_t esp_timer_get_time(void){return now;}
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
#define BSP_POWER_AMP_IO 46
static bool amp_held;
static unsigned amplifier_level,amplifier_enables;
static void enter_amp(portMUX_TYPE *m){assert(m&&!amp_held);amp_held=true;}
static void exit_amp(portMUX_TYPE *m){assert(m&&amp_held);amp_held=false;}
#define portENTER_CRITICAL(m) enter_amp(m)
#define portEXIT_CRITICAL(m) exit_amp(m)
static esp_err_t gpio_set_level(unsigned pin,unsigned level){assert(amp_held&&pin==46);amplifier_level=level;if(level)amplifier_enables++;else inhibits++;return ESP_OK;}
#include "bridge-amp-production.inc"
static void setup_bridge_wipe(void *p,size_t n){volatile unsigned char *b=p;while(n--)*b++=0;}
static unsigned xEventGroupSetBits(EventGroupHandle_t e,unsigned bits){e->bits|=bits;return e->bits;}
static unsigned xEventGroupClearBits(EventGroupHandle_t e,unsigned bits){e->bits&=~bits;return e->bits;}
static unsigned xEventGroupWaitBits(EventGroupHandle_t e,unsigned bits,int clear,int all,unsigned ms){(void)clear;(void)all;wait_ms=ms;if((e->bits&bits)!=bits)now+=(int64_t)ms*1000;return e->bits;}
static size_t xStreamBufferBytesAvailable(StreamBufferHandle_t s){stream_check(s);return s->n;}
static size_t xStreamBufferSend(StreamBufferHandle_t s,const void *p,size_t n,unsigned ms){
    (void)ms;stream_check(s);
    if(n>BV_TEST_CAP-s->n)n=BV_TEST_CAP-s->n; /* RTOS zero-wait partial write */
    memcpy(s->data+s->n,p,n);s->n+=n;
    if(a5_enabled&&s==a5_mic&&s->n>a5_highwater)a5_highwater=s->n;
    stream_check(s);return n;
}
static size_t xStreamBufferReceive(StreamBufferHandle_t s,void *p,size_t n,unsigned ms){
    (void)ms;stream_check(s);if(n>s->n)n=s->n;
    memcpy(p,s->data,n);
    if(a5_enabled&&s==a5_mic){
        assert(a5_consumed_n+n<=sizeof(a5_consumed));
        memcpy(a5_consumed+a5_consumed_n,p,n);a5_consumed_n+=n;
    }
    memmove(s->data,s->data+n,s->n-n);s->n-=n;stream_check(s);return n;
}
static void vTaskDelay(unsigned ms);
static void vTaskDelete(void *p){(void)p;}
static esp_err_t audio_stream_read(audio_session_t *a,void *p,unsigned n){
    (void)a;reads++;if(!a5_read(p,n))memset(p,1,n);
    return a5_enabled&&reads==a5_read_failure?ESP_ERR_INVALID_STATE:ESP_OK;
}
static esp_err_t audio_stream_write(audio_session_t *a,void *p,unsigned n){(void)a;assert(played+n<=sizeof(played_bytes));memcpy(played_bytes+played,p,n);writes++;played+=n;return ESP_OK;}
static bool bridge_transport_upgraded(bridge_transport_t *t){return t&&t->upgraded&&!t->cancelled;}
static bool bridge_transport_take_data(bridge_transport_t *t,bool *after,int64_t *at){(void)t;*after=false;*at=now;return true;}
static int64_t bridge_transport_end_at(bridge_transport_t *t){(void)t;return 0;}
static bool bridge_transport_admit_control(bridge_transport_t *t,int64_t deadline){(void)t;(void)deadline;return true;}
static bool bridge_transport_finished(const bridge_transport_t *t){return t&&t->finished;}
static bool bridge_transport_started(const bridge_transport_t *t){return t!=NULL;}
static void bridge_transport_cancel(bridge_transport_t *t){if(t)t->cancelled=true;}
/* Separate transport suite exercises actual SDK-task ownership. Here the
 * transport completion result is an injected boundary, never claimed a join. */
static bool bridge_transport_dispose_result(bridge_transport_t *t,int64_t until,bridge_transport_terminal_t *out){
    (void)t;assert(!model_lock_held);dispose_calls++;dispose_deadline=until;
    if(cleanup_transport_ok)*out=cleanup_terminal;
    return cleanup_transport_ok;
}
static void audio_stream_close(audio_session_t *a){assert(!model_lock_held);codec_closes++;free(a);}
static int esp_websocket_client_send_text(esp_websocket_client_handle_t h,const char *s,size_t n,unsigned ms){
    assert(!model_lock_held); /* no model guard across the blocking SDK call */
    (void)h;assert(sends<TEST_WIRE_CAP&&n<7000);memcpy(wire[sends],s,n);wire[sends++][n]=0;
    if(strstr(s,"session.input_audio.append")){
        wire_bytes+=(unsigned)n;
        if(a5_enabled)a5_observe_append(s,n,ms);
        else if(expire_send)now+=1000000;
        if(a5_enabled&&a5_return_mode==1)return (int)n-1;
        if(a5_enabled&&a5_return_mode==2)return 0;
        if(a5_enabled&&a5_return_mode==3)return -1; /* all uncertain; NEVER replay */
    }else if(a5_enabled){
        assert(strstr(s,"pet.turn.capture_stop")); /* no automatic Unified confirm */
        a5_observe_finish();
    }
    return (int)n;
}
/* Use the REAL linked codec, with clock advances on both sides. Only extracted
 * production calls are macro-routed; base64 output/algorithm are not replaced. */
static int a5_base64_encode(unsigned char *dst,size_t cap,size_t *olen,
                           const unsigned char *src,size_t n){
    if(a5_enabled)a5_advance_us(a5_encode_before);
    int rc=mbedtls_base64_encode(dst,cap,olen,src,n);
    if(a5_enabled)a5_advance_us(a5_encode_after);
    return rc;
}
#define mbedtls_base64_encode a5_base64_encode
#include "bridge-runtime-production.inc"
#undef mbedtls_base64_encode
static void vTaskDelay(unsigned ms){
    a5_advance_us((int64_t)ms*1000);delay_calls++;
    if(cleanup_mode){
        assert(!codec_closes); /* observed DONE bits alone must not close codec */
        if(cleanup_final_on_delay){
            bridge_voice_t *c=selected;
            atomic_store(&c->capture_exited,true);atomic_store(&c->play_exited,true);
            cleanup_final_on_delay=false;
        }
        return;
    }
    if(queue_on_delay&&selected){
        bridge_voice_t *c=selected;queue_on_delay=false;
        unsigned char prefix[240];memset(prefix,0x6d,sizeof(prefix));
        assert(xStreamBufferSend(c->playback,prefix,sizeof(prefix),0)==sizeof(prefix));
        atomic_store(&c->audio_epoch,1);atomic_store(&c->play_gate,true);atomic_store(&c->drain,true);
    }else if(end_on_delay&&selected)atomic_store(&((bridge_voice_t*)selected)->active,false);
}
/* Parent Stop may be injected at final send-admission guard, before it decides. */
static bool parent_stopped(void){return a5_parent_stop;}
static bridge_voice_t context;
static struct {unsigned bits;} ev;
static struct test_stream mic,speaker;
static bridge_transport_t transport;
static unsigned char append[4800];static char json[7000];
static char assembly[96000];
static void reset(void){
    for(unsigned i=0;i<received_n;i++)free(received[i].text);
    received_n=0;memset(played_bytes,0,sizeof(played_bytes));
    memset(&context,0,sizeof(context));memset(&ev,0,sizeof(ev));memset(&mic,0,sizeof(mic));memset(&speaker,0,sizeof(speaker));memset(wire,0,sizeof(wire));
    mic.before=mic.after=speaker.before=speaker.after=STREAM_CANARY;
    a5_enabled=a5_producer=a5_overflow=a5_timed_read=a5_fast_read=a5_jitter=false;
    a5_owner_jitter=a5_alternating=a5_parent_stop=model_lock_held=false;
    a5_return_mode=a5_encode_before=a5_encode_after=0;
    a5_lock_calls=a5_lock_at=a5_lock_us=a5_read_failure=0;
    a5_stall_tick=a5_stop_after_appends=0;
    a5_send_us=a5_ticks=a5_appends=a5_finishes=a5_cadence_late=a5_burst=0;
    a5_base=a5_until=a5_produce_until=a5_produce_next=0;
    a5_last_start=a5_last_return=a5_last_due=a5_finish_at=a5_first_start=a5_frontier=0;
    a5_produced=a5_consumed_n=a5_wire_pcm=a5_highwater=a5_last_n=a5_last_pending=a5_read_bytes=0;
    a5_event=A5_EVENT_NONE;a5_event_at=0;a5_event_sends=0;a5_mic=&mic;
    transport=(bridge_transport_t){.upgraded=true};now=1000000;reads=writes=inhibits=sends=wait_ms=delay_calls=wire_bytes=played=0;
    amp_held=false;amplifier_level=amplifier_enables=0;audio_stream_prepare();
    lock_ok=true;stop_on_take=false;expire_send=false;end_on_delay=false;queue_on_delay=false;selected=&context;
    guard=(void*)1;current=&context;context.events=(void*)&ev;context.capture=(void*)&mic;context.playback=(void*)&speaker;
    atomic_store(&restart_required,false);
    context.transport=&transport;context.hard=61000000;context.work=59000000;context.append_pcm=append;context.append_json=json;
    context.assembly_memory=assembly;
    context.parent_stopped=parent_stopped;atomic_store(&context.active,true);atomic_store(&context.connected,true);
    bridge_init(&context.model);strcpy(context.model.turn,TURN);context.model.phase=BR_CAPTURING;
}
static void confirm_fixture(void){
    context.model.phase=BR_PREVIEW;context.model.revision=1;context.model.preview_seen=true;
    strcpy(context.model.text,"hello");strcpy(context.model.hash,"2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
    assert(bridge_command(&context.model,BR_CMD_CONFIRM,1)==BR_ACT_CONFIRM);
}
static void legacy_cases(void){
    reset();failure(&context,"overflow");failure(&context,"deadline");
    atomic_store(&context.accepting,true);ws_event(&context,NULL,WEBSOCKET_EVENT_ERROR,NULL);
    assert(atomic_load(&context.exit_reason)==BV_EXIT_QUEUE&&!strcmp(exit_status(&context),"Microphone stream interrupted."));
    reset();note_exit(&context,BV_EXIT_MIC);failure(&context,"reply_unavailable");
    assert(!strcmp(exit_status(&context),"Microphone read failed."));
    reset();atomic_store(&context.accepting,true);ws_event(&context,NULL,WEBSOCKET_EVENT_ERROR,NULL);
    assert(atomic_load(&context.failed)&&atomic_load(&context.exit_reason)==BV_EXIT_CALLBACK);
    reset();failure(&context,"deadline");assert(!strcmp(exit_status(&context),"Voice time limit reached."));
    reset();bridge_voice_stop();assert(!strcmp(exit_status(&context),"Stopped. Unified work is not cancelled."));
    puts("PASS first fixed exit reason survives later failure/callback; mic/queue/receive/deadline/intentional Stop distinguishable");
    reset();end_on_delay=true;capture_task(&context);assert(reads==0&&(ev.bits&BV_IDLE));
    reset();atomic_store(&context.capture_gate,true);bridge_voice_finish_capture();assert(!atomic_load(&context.capture_gate)&&atomic_load(&context.command)==1);
    int64_t next=now;assert(!finish_capture(&context,&next)&&wait_ms==500&&sends==0); // no fabricated idle
    reset();ev.bits=BV_IDLE;mic.n=9600;memset(mic.data,0x22,mic.n);context.model.phase=BR_SETTLING;
    next=now;assert(finish_capture(&context,&next));assert(sends==3&&mic.n==0);
    assert(strstr(wire[0],"session.input_audio.append")&&strstr(wire[1],"session.input_audio.append")&&strstr(wire[2],"pet.turn.capture_stop"));
    assert(!atomic_load(&context.capture_gate)&&now>=1200000);
    reset();ev.bits=BV_IDLE;mic.n=9600;context.work=1100000;expire_send=true;context.model.phase=BR_SETTLING;
    next=now;assert(!finish_capture(&context,&next));assert(sends==1&&strstr(wire[0],"session.input_audio.append")&&mic.n==4800);
    reset();confirm_fixture();bridge_voice_confirm(1);bridge_voice_stop();
    assert(atomic_load(&context.command)==0&&context.model.send==BR_SEND_UNKNOWN&&inhibits&&transport.cancelled);
    assert(!send_action(&context,BR_ACT_CONFIRM)&&sends==0);
    reset();confirm_fixture();assert(send_action(&context,BR_ACT_CONFIRM)&&sends==1);
    atomic_store(&context.failed,true);finalize_model(&context);assert(context.model.send==BR_SEND_UNKNOWN);
    finalize_model(&context);assert(context.model.send==BR_SEND_UNKNOWN&&sends==1);
    reset();context.model.send=BR_SEND_ACCEPTED;finalize_model(&context);assert(context.model.send==BR_SEND_ACCEPTED&&context.model.reply==BR_REPLY_UNAVAILABLE);
    reset();atomic_store(&context.play_gate,true);speaker.n=240;memset(speaker.data,0x55,speaker.n);end_on_delay=true;
    play_task(&context);assert(played==0&&speaker.n==240); // short tail is NOT packet-gap completion
    reset();atomic_store(&context.play_gate,true);atomic_store(&context.drain,true);speaker.n=240;memset(speaker.data,0x55,speaker.n);end_on_delay=true;
    play_task(&context);assert(played==240&&speaker.n==0&&(ev.bits&BV_DRAINED));
    reset();atomic_store(&context.play_gate,true);speaker.n=20000;bridge_voice_stop();end_on_delay=true;
    play_task(&context);assert(played==0&&speaker.n==0&&!atomic_load(&context.play_gate));
    reset();queue_on_delay=true;end_on_delay=true;play_task(&context);
    assert(played==240&&speaker.n==0);for(unsigned i=0;i<played;i++)assert(played_bytes[i]==0x6d);
    reset();atomic_store(&context.accepting,true);
    const char valid[]="{\"type\":\"session.closed\"}";
    esp_websocket_event_data_t frame={.data_len=sizeof(valid)-1,.payload_len=sizeof(valid)-1,.op_code=1,.fin=true,.data_ptr=valid};
    ws_event(&context,NULL,WEBSOCKET_EVENT_DATA,&frame);
    assert(!atomic_load(&context.failed)&&received_n==1&&!strcmp(received[0].text,valid));
    ws_event(&context,NULL,WEBSOCKET_EVENT_DISCONNECTED,NULL);
    assert(!transport_lost(&context)); // queued DATA is consumed before clean EOF
    free(received[0].text);received_n=0;assert(transport_lost(&context));
    atomic_store(&context.drain,true);assert(!transport_lost(&context));
    reset();atomic_store(&context.accepting,true);
    const char corrupt[]="{\"type\":\"session.closed\"}\0{\"type\":\"pet.turn.preview\"}";
    frame=(esp_websocket_event_data_t){.data_len=sizeof(corrupt)-1,.payload_len=sizeof(corrupt)-1,.op_code=1,.fin=true,.data_ptr=corrupt};
    ws_event(&context,NULL,WEBSOCKET_EVENT_DATA,&frame);
    assert(atomic_load(&context.failed)&&received_n==0); // never strlen-truncate wire input
    reset();
    audio_stream_prepare();assert(enable_amplifier()==ESP_OK&&amplifier_level==1);
    bridge_voice_stop();assert(amplifier_level==0);
    unsigned enables=amplifier_enables;
    assert(enable_amplifier()==ESP_ERR_INVALID_STATE&&amplifier_enables==enables&&amplifier_level==0);
    audio_stream_inhibit();assert(enable_amplifier()==ESP_ERR_INVALID_STATE);
    audio_stream_prepare();assert(enable_amplifier()==ESP_OK&&amplifier_level==1);
    audio_stream_inhibit();assert(amplifier_level==0);
    puts("PASS: exact runtime bodies: gate-off no reads; Finish idle500/drain/final pacing/deadline; Stop queued confirm denied; late failure unknown; finite tail/new epoch prefix preserved; stale audio muted; raw NUL refused; queued DATA precedes EOF");
}

/* Fixtures below never calculate/assign the production next_audio cursor.
 * Pacing calculations here are independent CONTRACT ORACLES at the wire seam.
 * The runner inserts the actual production block in production_capture_owner. */
static void a5_stop_on_lock(void){
    stop_on_take=false; /* do not recursively inject into bridge_voice_stop */
    bridge_voice_stop();
}
static void a5_inject(void){
    enum a5_event event=a5_event;a5_event=A5_EVENT_NONE;a5_event_sends=sends;
    if(event==A5_EVENT_STOP)bridge_voice_stop();
    else if(event==A5_EVENT_FAILURE)failure(&context,"reply_unavailable");
    else if(event==A5_EVENT_PARENT)a5_parent_stop=true;
    else if(event==A5_EVENT_FAILED_FLAG)atomic_store(&context.failed,true);
}
static void a5_advance_us(int64_t us){
    assert(us>=0);
    if(!a5_enabled){now+=us;return;}
    int64_t end=now+us;
    assert(end<=a5_base+58000000); /* unchanged work-cap-sized synthetic bound */
    for(;;){
        int64_t tick=end;
        if(a5_producer&&a5_produce_next<tick)tick=a5_produce_next;
        if(a5_event!=A5_EVENT_NONE&&a5_event_at<tick)tick=a5_event_at;
        assert(tick>=now);now=tick;
        /* An event coincident with a producer completion wins admission. */
        if(a5_event!=A5_EVENT_NONE&&now==a5_event_at)a5_inject();
        if(a5_producer&&now==a5_produce_next){
            a5_produce_next+=20000;
            if(atomic_load(&context.capture_gate)&&!atomic_load(&context.stop)&&!atomic_load(&context.failed)){
                unsigned char pcm[960];
                for(size_t i=0;i<sizeof(pcm);i++)pcm[i]=a5_byte(a5_produced+i);
                if(xStreamBufferSend(context.capture,pcm,sizeof(pcm),0)!=sizeof(pcm)){
                    /* Fixture records inability to enqueue; it does NOT fake
                     * capture_task's failure(), cleanup, Stop or final state. */
                    a5_overflow=true;a5_producer=false;
                }else a5_produced+=sizeof(pcm);
            }
            if(a5_produce_next>a5_produce_until)a5_producer=false;
        }
        if(now==end)break;
    }
}
static bool a5_owner_tick(void){
    if(now>=a5_until||a5_overflow)return false;
    if(a5_stop_after_appends&&a5_appends>=a5_stop_after_appends)return false;
    assert(++a5_ticks<=11601);
    /* Mirrors an empty owner xQueueReceive(...,5ms), not a replacement owner.
     * The production outer guard is checked BEFORE this wait, as in the source.
     * Stop/deadline occurring in it must still be refused at send admission. */
    if(a5_ticks==a5_stall_tick)a5_advance_us(500000);
    a5_advance_us(5000+(a5_owner_jitter?(a5_ticks%3)*1000:0));
    /* If the synthetic producer could not enqueue during this tick, stop the
     * excerpt shell before another dequeue. Actual capture failure is exercised
     * separately with the real capture_task body, not fabricated here. */
    return !a5_overflow;
}
static bool a5_read(void *p,unsigned n){
    if(!a5_enabled||(!a5_timed_read&&!a5_fast_read))return false;
    assert(n==960&&reads<=650); /* also bounds instant-read drop/continue bugs */
    if(a5_timed_read)a5_advance_us(20000);
    for(size_t i=0;i<n;i++)((unsigned char*)p)[i]=a5_byte(a5_read_bytes+i);
    a5_read_bytes+=n;return true;
}
static bool a5_window_ok(int64_t prior_duration,int64_t stamp,int64_t frontier){
    /* For EVERY k<i: (C(i)-C(k))-(S(i)-S(k)) <= 5000.
     * The maximum is C(i)-S(i)+max_k(S(k)-C(k)). O(1), no cursor input. */
    return prior_duration-stamp+frontier<=5000;
}
static void a5_observe_append(const char *s,size_t n,unsigned timeout_ms){
    static const char prefix[]="{\"type\":\"session.input_audio.append\",\"audio\":\"";
    unsigned char decoded[4800];size_t bytes=0;
    assert(n>sizeof(prefix)&&!strncmp(s,prefix,sizeof(prefix)-1));
    const char *b64=s+sizeof(prefix)-1,*end=strchr(b64,'"');
    assert(end&&!strcmp(end,"\"}"));
    assert(mbedtls_base64_decode(decoded,sizeof(decoded),&bytes,
        (const unsigned char*)b64,(size_t)(end-b64))==0);
    assert(bytes>0&&bytes<=4800&&bytes%2==0);
    assert(a5_wire_pcm+bytes<=a5_consumed_n&&a5_consumed_n<=a5_produced);
    for(size_t i=0;i<bytes;i++){
        assert(decoded[i]==a5_consumed[a5_wire_pcm+i]);
        assert(decoded[i]==a5_byte(a5_wire_pcm+i));
    }
    /* send_frame has already admitted this send; an in-flight one may settle.
     * No subsequent call can enter the wire seam after Stop/failure/deadline. */
    assert(!atomic_load(&context.stop)&&!atomic_load(&context.failed));
    assert(now<context.work&&now<context.hard);
    if(a5_appends){
        int64_t duration=(int64_t)a5_last_n*1000000/48000;
        int64_t gap=now-a5_last_start;
        /* At most one 5ms owner-wait quantum of timing jitter may be recovered;
         * a running deadline must not turn a long stall into a burst. */
        if(gap<duration-5000)a5_burst++;
        /* With pending PCM and latency within this frame's budget, network
         * time cannot become an extra period of idle. 10ms permits tick edges. */
        if(a5_last_pending>=960&&a5_last_return-a5_last_start<=duration&&gap>duration+10000)
            a5_cadence_late++;
    }
    int64_t prior=(int64_t)a5_wire_pcm*1000000/48000;
    if(!a5_appends){a5_first_start=now;a5_frontier=now;}
    else assert(a5_window_ok(prior,now,a5_frontier));
    if(now-prior>a5_frontier)a5_frontier=now-prior;
    /* Independent cumulative rate oracle: repeated 5ms credit cannot grow into
     * an arbitrarily early stream, even when each pairwise gap looks bounded. */
    assert(now-a5_first_start+5000>=(int64_t)a5_wire_pcm*1000000/48000);
    a5_last_start=now;a5_last_n=bytes;
    a5_last_due=now+(int64_t)bytes*1000000/48000;
    a5_wire_pcm+=bytes;a5_appends++;
    unsigned send_us=a5_jitter?(a5_appends%3==0?58000:a5_appends%3==1?67000:76000):
        a5_alternating?(a5_appends%2?25000:75000):a5_send_us;
    assert(send_us<=(uint64_t)timeout_ms*1000);
    a5_advance_us(send_us); /* producer keeps running inside blocking send */
    a5_last_return=now;a5_last_pending=mic.n;
    stream_check(&mic);stream_check(&speaker);
}
static void a5_observe_finish(void){
    assert(!atomic_load(&context.stop)&&!atomic_load(&context.failed));
    assert(!a5_parent_stop&&now<context.work&&now<context.hard);
    assert(++a5_finishes==1);a5_finish_at=now;
}
static void a5_begin(unsigned send_ms){
    reset();a5_enabled=true;a5_send_us=send_ms*1000;
    a5_base=now;a5_until=now+10000000;
    a5_produce_until=a5_until;a5_produce_next=now+20000;
    atomic_store(&context.capture_gate,true);
}
static void a5_enqueue(size_t n){
    assert(n<=BV_TEST_CAP-mic.n&&n%960==0);
    unsigned char pcm[960];
    for(size_t offset=0;offset<n;offset+=sizeof(pcm)){
        for(size_t i=0;i<sizeof(pcm);i++)pcm[i]=a5_byte(a5_produced+offset+i);
        assert(xStreamBufferSend(context.capture,pcm,sizeof(pcm),0)==sizeof(pcm));
    }
    a5_produced+=n;
}
static void a5_seed(size_t n){
    assert(a5_produced==0);a5_enqueue(n);
}
static void a5_no_unified(void){
    assert(!context.model.confirmed&&context.model.send==BR_SEND_NONE);
    assert(!(atomic_load(&context.command)==2)&&!(context.actions&BR_ACT_CONFIRM));
    for(unsigned i=0;i<sends;i++){
        assert(!strstr(wire[i],"send_confirm")&&!strstr(wire[i],"session.start"));
        assert(strstr(wire[i],"session.input_audio.append")||strstr(wire[i],"pet.turn.capture_stop"));
    }
    stream_check(&mic);stream_check(&speaker);
}
static void a5_exact_complete(void){
    assert(a5_wire_pcm==a5_consumed_n&&a5_consumed_n==a5_produced&&mic.n==0);
    for(size_t i=0;i<a5_consumed_n;i++)assert(a5_consumed[i]==a5_byte(i));
    /* Check after Finish as well: its later wire frontier must not hide extra
     * inter-append idle that was recorded only during finite drain. */
    assert(a5_burst==0&&a5_cadence_late==0);a5_no_unified();
}
static void a5_finish_contract(int64_t cursor){
    assert(a5_finishes==1&&a5_appends>0);
    int64_t total=(int64_t)a5_wire_pcm*1000000/48000;
    /* Include ALL preceding frames. The first total must elapse with NO global
     * 5ms lead; any later suffix may use at most ONE 5ms credit. */
    int64_t due=a5_first_start+total;
    int64_t suffix_due=a5_frontier+total-5000;
    if(suffix_due>due)due=suffix_due;
    if(a5_last_return>due)due=a5_last_return;
    assert(a5_finish_at>=due&&a5_finish_at<=due+10000);
    assert(a5_finish_at>=cursor); /* actual target is additional, NOT the oracle */
    assert(a5_window_ok(total,a5_finish_at,a5_frontier));
}
static void a5_print(const char *name){
    printf("A5 fixture %s: virtual_us=%lld produced=%zu consumed=%zu wire_pcm=%zu "
           "appends=%u queue=%zu highwater=%zu overflow=%u failed=%u "
           "extra_idle=%u burst=%u finish_us=%lld\n",name,
           (long long)(now-a5_base),a5_produced,a5_consumed_n,a5_wire_pcm,
           a5_appends,mic.n,a5_highwater,(unsigned)a5_overflow,
           (unsigned)atomic_load(&context.failed),a5_cadence_late,a5_burst,
           (long long)(a5_finish_at?a5_finish_at-a5_base:0));
}
static void a5_realtime(const char *name,unsigned send_ms,unsigned seconds){
    a5_begin(send_ms);a5_producer=true;int64_t next=0;
    a5_jitter=!strcmp(name,"realtime-long-jitter");
    a5_owner_jitter=!strcmp(name,"realtime-long-owner-jitter");
    a5_alternating=!strcmp(name,"realtime-long-alternating");
    a5_until=a5_base+(int64_t)seconds*1000000;a5_produce_until=a5_until;
    production_capture_owner(&context,&next);a5_print(name);
    /* Both 10s and 55s contracts are bounded, NOT indefinite delivery. The long
     * case stays inside work/hard caps and detects cumulative scheduler drift. */
    assert(!a5_overflow&&!atomic_load(&context.failed)&&!atomic_load(&context.stop));
    assert(a5_cadence_late==0&&a5_burst==0&&a5_highwater<=14400&&mic.n<=14400);
    assert(a5_produced==(size_t)seconds*48000&&a5_appends>0);
    assert(now<=a5_until+100000);
    a5_producer=false;ev.bits|=BV_IDLE;context.model.phase=BR_SETTLING;
    assert(finish_capture(&context,&next));
    a5_print(name);a5_finish_contract(next);a5_exact_complete();
    assert(now<=a5_until+400000); /* bounded tail, not a 2s catch-up allowance */
}
static void a5_empty(void){
    a5_begin(0);a5_until=now+100000;int64_t next=0;
    production_capture_owner(&context,&next);
    assert(sends==0&&a5_consumed_n==0&&mic.n==0&&next==0&&now==a5_until);
    a5_no_unified();
}
static void a5_small(const char *name,unsigned send_ms){
    a5_begin(send_ms);a5_seed(960);a5_until=now+5000;int64_t next=0;
    production_capture_owner(&context,&next);
    assert(a5_appends==1&&a5_last_n==960&&mic.n==0);
    assert(a5_last_start==a5_base+5000&&a5_last_due==a5_last_start+20000);
    ev.bits|=BV_IDLE;context.model.phase=BR_SETTLING;
    assert(finish_capture(&context,&next));a5_print(name);
    a5_finish_contract(next);a5_exact_complete();
}
static void a5_finish_case(const char *name,unsigned send_ms,bool full){
    a5_begin(send_ms);a5_seed(full?96000:4800);
    ev.bits=BV_IDLE;context.model.phase=BR_SETTLING;int64_t next=now;
    assert(finish_capture(&context,&next));a5_print(name);
    a5_finish_contract(next);a5_exact_complete();
    assert(a5_appends==(full?20u:1u));
    if(full)assert(now<=a5_base+2010000);
}
static void a5_capture_overflow(void){
    a5_begin(0);a5_timed_read=true;
    capture_task(&context);a5_print("capture-overflow");
    /* Verbatim capture_task: ten stale-DMA discards, 100 queued 20ms chunks,
     * then a full-buffer write failure. This is the real body failure path,
     * with synthetic nonblocking storage and timed codec-read seam only. */
    assert(reads==111&&mic.n==96000&&now-a5_base==2220000);
    assert(atomic_load(&context.failed)&&!strcmp(context.model.code,"overflow"));
    assert(!atomic_load(&context.capture_gate)&&!atomic_load(&context.play_gate));
    assert((ev.bits&(BV_IDLE|BV_CAPTURE_DONE))==(BV_IDLE|BV_CAPTURE_DONE));
    for(size_t i=0;i<mic.n;i++)assert(mic.data[i]==a5_byte(9600+i));
    unsigned before=reads;capture_task(&context);
    assert(reads==before&&mic.n==96000&&sends==0);a5_no_unified();
}
static void a5_capture_inflight(enum a5_event event){
    a5_begin(0);a5_timed_read=true;a5_event=event;a5_event_at=now+220000;
    capture_task(&context);
    assert(reads==11&&mic.n==0&&sends==0&&a5_event==A5_EVENT_NONE);
    assert(!atomic_load(&context.capture_gate));
    assert((ev.bits&(BV_IDLE|BV_CAPTURE_DONE))==(BV_IDLE|BV_CAPTURE_DONE));
    assert(event==A5_EVENT_STOP?atomic_load(&context.stop):atomic_load(&context.failed));
    unsigned before=reads;capture_task(&context);assert(reads==before);a5_no_unified();
}
static void a5_capture_near_full(void){
    a5_begin(0);
    mic.n=BV_TEST_CAP-480;
    for(size_t i=0;i<mic.n;i++)mic.data[i]=a5_byte(i);
    a5_highwater=mic.n;a5_fast_read=true;
    capture_task(&context); /* actual body must reject a partial 480/960 write */
    assert(reads==11&&mic.n==BV_TEST_CAP);
    assert(atomic_load(&context.failed)&&!strcmp(context.model.code,"overflow"));
    assert(!atomic_load(&context.capture_gate)&&sends==0);
    assert((ev.bits&(BV_IDLE|BV_CAPTURE_DONE))==(BV_IDLE|BV_CAPTURE_DONE));
    for(size_t i=0;i<BV_TEST_CAP-480;i++)assert(mic.data[i]==a5_byte(i));
    for(size_t i=0;i<480;i++)assert(mic.data[BV_TEST_CAP-480+i]==a5_byte(9600+i));
    unsigned before=reads;capture_task(&context);
    assert(reads==before&&mic.n==BV_TEST_CAP);a5_no_unified();
}
static void a5_no_readmit(int64_t *next){
    unsigned before=sends;size_t consumed=a5_consumed_n;
    a5_until=now+100000;
    production_capture_owner(&context,next);
    assert(sends==before&&a5_consumed_n==consumed);
    int64_t saved=*next;
    assert(!append_audio(&context,960,next)&&sends==before&&*next==saved);
    ev.bits|=BV_IDLE;context.model.phase=BR_SETTLING;
    assert(!finish_capture(&context,next)&&sends==before&&*next==saved);
    a5_no_unified();
}
static void a5_too_slow(void){
    a5_begin(200);a5_producer=true;int64_t next=0;
    production_capture_owner(&context,&next);a5_print("too-slow-200");
    assert(now<=a5_base+10000000);
    assert(a5_overflow||atomic_load(&context.failed));
    if(!atomic_load(&context.failed)){
        /* Explicit fixture seam: the realtime producer itself only flags a
         * full queue. Exercise ACTUAL capture_task against that full queue to
         * test fail-closed, not a handwritten mock failure()/drop policy.
         * Fresh task's ten discards use instant synthetic reads here. This
         * transition is NOT a co-simulation of a running codec task. */
        assert(a5_overflow&&mic.n==BV_TEST_CAP);
        a5_fast_read=true;capture_task(&context);a5_fast_read=false;
    }
    assert(atomic_load(&context.failed)&&!atomic_load(&context.capture_gate));
    assert(context.model.phase==BR_CLOSING);
    a5_no_readmit(&next); /* possibly-sent PCM is neither replayed nor confirmed */
}
static void a5_wait_case(bool finish,enum a5_event event,bool deadline){
    a5_begin(0);a5_seed(960);int64_t next=now+100000;
    if(deadline)context.work=now+25000;
    else {a5_event=event;a5_event_at=now+25000;}
    a5_until=now+200000;
    if(finish){
        ev.bits=BV_IDLE;context.model.phase=BR_SETTLING;
        assert(!finish_capture(&context,&next));
    }else production_capture_owner(&context,&next);
    assert(sends==0&&a5_appends==0&&a5_finishes==0);
    assert(now<=a5_base+30000); /* do not sit out the future target after failure */
    assert(atomic_load(&context.failed)||atomic_load(&context.stop));
    if(deadline)assert(now<=context.work+5000);
    a5_no_readmit(&next);
}
static void a5_send_case(enum a5_event event,bool deadline,unsigned return_mode){
    a5_begin(75);a5_producer=true;a5_return_mode=return_mode;int64_t next=0;
    if(deadline)context.work=now+50000;
    else if(event!=A5_EVENT_NONE){a5_event=event;a5_event_at=now+50000;}
    production_capture_owner(&context,&next);
    assert(sends==1&&a5_appends==1&&a5_finishes==0);
    assert(a5_wire_pcm==960&&a5_consumed_n==960);
    assert(now==a5_base+95000); /* 20ms first PCM + admitted 75ms send */
    assert(return_mode?next==0:next>0); /* uncertain send never advances cursor */
    assert(atomic_load(&context.failed)||atomic_load(&context.stop));
    if(event!=A5_EVENT_NONE)assert(a5_event_sends==1&&a5_event==A5_EVENT_NONE);
    a5_no_readmit(&next);
}
static void a5_admission_stop(void){
    a5_begin(0);a5_seed(960);a5_until=now+5000;stop_on_take=true;int64_t next=0;
    production_capture_owner(&context,&next);
    assert(sends==0&&atomic_load(&context.stop)&&transport.cancelled);
    assert(a5_consumed_n==960&&a5_wire_pcm==0);a5_no_readmit(&next);
}
static void a5_pacer_stall(void){
    a5_begin(0);int64_t next=0;
    pace_sent_audio(&next,now,4800);
    assert(next==now+100000);
    /* A half-second stall grants only 5ms credit, never its entire backlog. */
    now+=600000;pace_sent_audio(&next,now,4800);
    assert(next==now+95000);
    now=next+2000;pace_sent_audio(&next,now,960);
    assert(next==now+18000);
    /* Bounded jitter recovery keeps the original sample target, rather than
     * resetting a fresh late interval on every successful frame. */
    now=next+4000;pace_sent_audio(&next,now,4800);
    assert(next==now+96000);
    a5_no_unified();
}
static void a5_oracle_window(void){
    /* Pairwise 95ms gaps and the original first-prefix bound both pass after
     * a large stall, but two successive 5ms gains violate that STALLED suffix.
     * Pin the oracle's sign/min-vs-max independently of production pacing. */
    const int64_t c[]={0,100000,200000,300000,400000};
    const int64_t s[]={1000000,1100000,1700000,1795000,1890000};
    int64_t frontier=s[0];
    for(unsigned i=1;i<5;i++){
        assert(s[i]-s[0]+5000>=c[i]);
        assert(s[i]-s[i-1]>=95000);
        assert(a5_window_ok(c[i],s[i],frontier)==(i!=4));
        if(s[i]-c[i]>frontier)frontier=s[i]-c[i];
    }
}
static void a5_prep_timing(bool finish){
    a5_begin(0);a5_seed(960);int64_t next=0;
    a5_encode_before=7000;a5_encode_after=11000;
    /* Owner: first lock is append admission. Finish: gate handoff then append. */
    a5_lock_at=finish?2:1;a5_lock_us=13000;
    a5_until=now+5000;
    if(finish){
        ev.bits=BV_IDLE;context.model.phase=BR_SETTLING;
        assert(finish_capture(&context,&next));
        assert(a5_last_start==a5_base+31000);
    }else{
        production_capture_owner(&context,&next);
        assert(a5_appends==1&&a5_last_start==a5_base+36000);
        /* One successful first append: its actual SDK admission is the origin. */
        assert(next==a5_last_start+20000);
        ev.bits=BV_IDLE;context.model.phase=BR_SETTLING;
        assert(finish_capture(&context,&next));
    }
    a5_finish_contract(next);a5_exact_complete();
}
/* seam 0 = owner 5ms wait; 1 = real base64 wrapper; 2 = append admission lock.
 * Parent callback and UI bridge_voice_stop are intentionally distinct events. */
static void a5_boundary(unsigned seam,enum a5_event event,bool deadline){
    a5_begin(0);a5_seed(960);int64_t next=0;
    a5_until=now+100000;
    if(seam==1){a5_encode_before=3000;a5_encode_after=8000;}
    if(seam==2){a5_lock_at=1;a5_lock_us=11000;}
    int64_t boundary=now+(seam?9000:3000);
    if(deadline)context.work=boundary;
    else {a5_event=event;a5_event_at=boundary;}
    production_capture_owner(&context,&next);
    assert(sends==0&&a5_appends==0&&a5_finishes==0&&next==0);
    assert(atomic_load(&context.stop)||atomic_load(&context.failed));
    if(event==A5_EVENT_PARENT)assert(a5_parent_stop&&!transport.cancelled);
    a5_no_readmit(&next);
}
static void a5_empty_target(enum a5_event event,bool deadline){
    a5_begin(0);int64_t next=now+100000;
    ev.bits=BV_IDLE;context.model.phase=BR_SETTLING;
    if(deadline)context.work=now+25000;
    else if(event!=A5_EVENT_NONE){a5_event=event;a5_event_at=now+25000;}
    bool ok=finish_capture(&context,&next);
    assert(a5_appends==0&&a5_consumed_n==0&&mic.n==0);
    if(event==A5_EVENT_NONE&&!deadline){
        assert(ok&&a5_finishes==1&&sends==1);
        assert(a5_finish_at==next&&now==a5_base+100000);
    }else{
        assert(!ok&&sends==0&&a5_finishes==0&&now<=a5_base+30000);
        a5_no_readmit(&next);
    }
    a5_no_unified();
}
static void a5_finish_admission(enum a5_event event,bool deadline){
    a5_begin(0);int64_t next=0;
    ev.bits=BV_IDLE;context.model.phase=BR_SETTLING;
    /* Empty capture: gate lock, model encode lock, FINAL network admission lock.
     * Time crosses the boundary only in that last lock. */
    a5_lock_at=3;a5_lock_us=11000;
    if(deadline)context.work=now+5000;
    else {a5_event=event;a5_event_at=now+5000;}
    assert(!finish_capture(&context,&next));
    assert(sends==0&&a5_finishes==0&&a5_appends==0&&next==0);
    a5_no_readmit(&next);
}
static void a5_long_stall(void){
    a5_begin(0);a5_producer=true;a5_stall_tick=40;int64_t next=0;
    production_capture_owner(&context,&next);a5_print("owner-long-stall");
    /* A 500ms stop in owner service alone adds 24k PCM: NOT normal <=14.4k.
     * Keep the separate finite bound, exact bytes and suffix-credit invariant. */
    assert(!a5_overflow&&!atomic_load(&context.failed));
    assert(a5_highwater>=24000&&a5_highwater<=38400&&mic.n<=38400);
    assert(a5_produced==480000&&a5_burst==0);
    a5_producer=false;ev.bits=BV_IDLE;context.model.phase=BR_SETTLING;
    assert(finish_capture(&context,&next));
    a5_finish_contract(next);a5_exact_complete();a5_print("owner-long-stall");
    assert(now<=a5_until+900000);
}
static void a5_variable(void){
    a5_begin(0);int64_t next=0;
    for(unsigned i=0;i<15;i++){
        size_t n=(i%5+1)*960;
        a5_enqueue(n);a5_stop_after_appends=i+1;a5_until=now+150000;
        production_capture_owner(&context,&next);
        assert(a5_appends==i+1&&a5_last_n==n&&mic.n==0);
    }
    /* The final partial 3840B frame is drained by the REAL Finish helper. */
    a5_enqueue(13440);ev.bits=BV_IDLE;context.model.phase=BR_SETTLING;
    assert(finish_capture(&context,&next));
    assert(a5_appends==18&&a5_last_n==3840);
    a5_finish_contract(next);a5_exact_complete();
}
static void a5_read_error(void){
    a5_begin(0);a5_timed_read=true;a5_read_failure=11;
    capture_task(&context);
    assert(reads==11&&now==a5_base+220000&&mic.n==0&&sends==0);
    assert(atomic_load(&context.failed)&&!atomic_load(&context.capture_gate));
    assert(!strcmp(context.model.code,"reply_unavailable"));
    assert((ev.bits&(BV_IDLE|BV_CAPTURE_DONE))==(BV_IDLE|BV_CAPTURE_DONE));
    capture_task(&context);assert(reads==11);int64_t next=0;a5_no_readmit(&next);
}
static void a5_final_guard_error(void){
    reset();confirm_fixture();lock_ok=false;
    finalize_model(&context);
    assert(atomic_load(&restart_required)&&sends==0);
    /* Retention/cleanup itself is NOT extracted or claimed exercised here. */
    assert(context.model.confirmed&&context.model.send==BR_SEND_NONE);
}
static void a5_closing_caps(void){
    reset();context.work=now-1;atomic_store(&context.stop,true);
    atomic_store(&context.failed,true);a5_parent_stop=true;
    assert(send_frame(&context,"{\"type\":\"pet.stop\"}"));
    assert(send_frame(&context,"{\"type\":\"session.close\"}")&&sends==2);
    context.hard=now;
    assert(!send_frame(&context,"{\"type\":\"pet.stop\"}")&&sends==2);
}
static void a5_timed_failure(unsigned mode){
    reset();a5_enabled=true;a5_base=now;a5_return_mode=mode;
    /* Prepare a real append through the owner so exact-prefix oracles stay on.
     * Inspect the timed helper's out parameter separately on refusal. */
    a5_seed(960);a5_until=now+5000;int64_t next=0;
    production_capture_owner(&context,&next);
    assert(sends==1&&a5_wire_pcm==960&&next==0&&atomic_load(&context.failed));
    int64_t stamp=INT64_C(-7788);
    assert(!send_frame_timed(&context,json,BV_SEND_MS,&stamp)&&stamp==INT64_C(-7788)&&sends==1);
    a5_no_readmit(&next);
}
static void a5_timed_output(unsigned mode){
    a5_begin(0);a5_seed(960);a5_return_mode=mode;
    assert(xStreamBufferReceive(context.capture,context.append_pcm,960,0)==960);
    /* Let the REAL append helper prepare JSON, but refuse its lock BEFORE any
     * SDK call. A subsequent first-ever SDK attempt is not an uncertain replay. */
    lock_ok=false;int64_t next=0;
    assert(!append_audio(&context,960,&next)&&sends==0&&next==0);
    lock_ok=true;int64_t stamp=INT64_C(-7788),admitted=now;
    bool ok=send_frame_timed(&context,json,BV_SEND_MS,&stamp);
    assert(ok==(mode==0)&&sends==1&&a5_appends==1&&a5_wire_pcm==960);
    assert(stamp==(mode==0?admitted:INT64_C(-7788)));
    assert(next==0&&a5_finishes==0); /* timed transport alone must not pace */
    a5_no_unified();
}
static bridge_voice_t *cleanup_fixture(void){
    cleanup_mode=true;cleanup_transport_ok=true;cleanup_final_on_delay=false;cleanup_terminal=BR_TRANSPORT_NONE;
    codec_closes=deleted_queues=deleted_streams=deleted_groups=dispose_calls=0;
    transport=(bridge_transport_t){.upgraded=true};
    bridge_voice_t *c=calloc(1,sizeof(*c));assert(c);
    current=c;selected=c;terminal_valid=false;
    c->transport=&transport;c->hard=now+2000000;c->work=now;c->audio=calloc(1,sizeof(*c->audio));
    c->events=calloc(1,sizeof(*c->events));c->frames=calloc(1,1);
    c->capture=calloc(1,sizeof(*c->capture));c->playback=calloc(1,sizeof(*c->playback));
    c->capture_memory=calloc(1,BV_MIC+1);c->playback_memory=calloc(1,BV_SPEAKER+1);
    c->assembly_memory=calloc(1,BV_FRAME);c->append_pcm=calloc(1,BV_APPEND);
    c->append_json=calloc(1,7000);c->decoded=calloc(1,BV_APPEND);
    bridge_init(&c->model);c->model.phase=BR_CAPTURING;
    atomic_store(&c->active,true);atomic_store(&c->accepting,true);
    for(unsigned i=0;i<2;i++){bv_frame_t s={.text=strdup("private queued frame"),.received_at=now};assert(xQueueSend(c->frames,&s,0));}
    return c;
}
static void cleanup_case(const char *name){
    reset();
    bridge_voice_t *c=cleanup_fixture();int64_t hard=c->hard;
    bool retained=false;
    if(!strcmp(name,"cleanup-unknown")){c->model.confirmed=true;c->model.phase=BR_SENDING;}
    if(!strcmp(name,"cleanup-interrupted-tail")){
        c->model.send=BR_SEND_ACCEPTED;c->model.reply=BR_REPLY_RECEIVED;c->model.draining=true;
        atomic_store(&c->stop,true);
    }
    bool terminal=!strcmp(name,"cleanup-retained-terminal")||!strcmp(name,"cleanup-retained-muted");
    if(terminal){
        bv_frame_t p;while(xQueueReceive(c->frames,&p,0)==pdTRUE)free(p.text);
        bridge_init(&c->model);bridge_command(&c->model,BR_CMD_CONVERSE,0);
        atomic_store(&c->converse,true);atomic_store(&c->exit_reason,BV_EXIT_CALLBACK);
        cleanup_terminal=BR_TRANSPORT_POLICY;
    }
    if(!strcmp(name,"cleanup-audio-final")||!strcmp(name,"cleanup-timeout")||!strcmp(name,"cleanup-hard-cap")||terminal){
        c->audio_tasks=BV_CAPTURE_DONE|BV_PLAY_DONE;
        c->events->bits=c->audio_tasks; /* bits set, actual signaling tails NOT finished */
        cleanup_final_on_delay=!strcmp(name,"cleanup-audio-final");
        retained=!cleanup_final_on_delay;
        if(!strcmp(name,"cleanup-hard-cap"))hard=c->hard=now+2000;
    }
    if(!strcmp(name,"cleanup-transport-timeout")){cleanup_transport_ok=false;retained=true;}
    cleanup(c,true);
    assert(dispose_calls==1&&dispose_deadline==hard&&sends==0);
    if(retained){
        assert(current==c&&atomic_load(&restart_required)&&!codec_closes&&!deleted_queues&&!deleted_groups);
        if(terminal){
            assert(!c->transport&&c->transport_terminal==BR_TRANSPORT_POLICY);
            /* A real mute while inactive remains authoritative, even if the
             * reaper observes it after this call's deadline. */
            if(!strcmp(name,"cleanup-retained-muted")){now=c->work+1;bridge_voice_stop();assert(atomic_load(&c->muted));}
        }
        assert(now<=hard); /* no fresh local budget beyond the original cap */
        /* Explicitly finish the synthetic workers/transport, then exercise the
         * SAME cleanup on retained storage. This is fixture settlement, not a
         * product reconnect or a fabricated successful prior cleanup. */
        atomic_store(&c->capture_exited,true);atomic_store(&c->play_exited,true);
        cleanup_transport_ok=true;bridge_voice_reap_finished();
    }
    assert(current==NULL&&codec_closes==1&&deleted_queues==1&&deleted_streams==2&&deleted_groups==1&&!received_n);
    assert(!atomic_load(&restart_required)&&!terminal_view.can_send&&!terminal_view.can_stop);
    bridge_view_t view;assert(bridge_voice_snapshot(&view));
    if(!strcmp(name,"cleanup-unknown"))assert(view.send==BR_SEND_UNKNOWN&&strstr(last_status,"Unknown"));
    if(!strcmp(name,"cleanup-interrupted-tail"))assert(view.reply==BR_REPLY_STOPPED&&!strstr(last_status,"finished"));
    if(terminal)assert(!strcmp(last_status,!strcmp(name,"cleanup-retained-muted")?"Mic off":"Host refused the voice connection."));
    if(!strcmp(name,"cleanup-reuse")){
        /* No boot/global reset between the real cleanup invocations. */
        c=cleanup_fixture();c->model.send=BR_SEND_ACCEPTED;c->model.reply=BR_REPLY_RECEIVED;
        cleanup(c,true);assert(current==NULL&&!atomic_load(&restart_required)&&strstr(last_status,"Reply finished"));
    }
    cleanup_mode=false;selected=&context;
}
int main(int argc,char **argv){
    setbuf(stdout,NULL);
    const char *name=argc==1?"legacy":argv[1];
    assert(argc<=2);
    printf("CASE %s (synthetic clock/RTOS/codec/transport, no device/Unified)\n",name);
    if(!strncmp(name,"cleanup-",8))cleanup_case(name);
    else if(!strcmp(name,"legacy"))legacy_cases();
    else if(!strcmp(name,"realtime-0"))a5_realtime(name,0,10);
    else if(!strcmp(name,"realtime-25"))a5_realtime(name,25,10);
    else if(!strcmp(name,"realtime-75"))a5_realtime(name,75,10);
    else if(!strcmp(name,"realtime-long-0"))a5_realtime(name,0,55);
    else if(!strcmp(name,"realtime-long-25"))a5_realtime(name,25,55);
    else if(!strcmp(name,"realtime-long-75"))a5_realtime(name,75,55);
    else if(!strcmp(name,"realtime-long-67"))a5_realtime(name,67,55);
    else if(!strcmp(name,"realtime-long-jitter"))a5_realtime(name,67,55);
    else if(!strcmp(name,"realtime-long-owner-jitter"))a5_realtime(name,67,55);
    else if(!strcmp(name,"realtime-long-alternating"))a5_realtime(name,25,55);
    else if(!strcmp(name,"empty"))a5_empty();
    else if(!strcmp(name,"small-0"))a5_small(name,0);
    else if(!strcmp(name,"small-25"))a5_small(name,25);
    else if(!strcmp(name,"finish-0"))a5_finish_case(name,0,false);
    else if(!strcmp(name,"finish-25"))a5_finish_case(name,25,false);
    else if(!strcmp(name,"finish-75"))a5_finish_case(name,75,false);
    else if(!strcmp(name,"finish-full-75"))a5_finish_case(name,75,true);
    else if(!strcmp(name,"capture-overflow"))a5_capture_overflow();
    else if(!strcmp(name,"capture-near-full"))a5_capture_near_full();
    else if(!strcmp(name,"capture-inflight-stop"))a5_capture_inflight(A5_EVENT_STOP);
    else if(!strcmp(name,"capture-inflight-failure"))a5_capture_inflight(A5_EVENT_FAILURE);
    else if(!strcmp(name,"too-slow-200"))a5_too_slow();
    else if(!strcmp(name,"owner-wait-stop"))a5_wait_case(false,A5_EVENT_STOP,false);
    else if(!strcmp(name,"owner-wait-failure"))a5_wait_case(false,A5_EVENT_FAILURE,false);
    else if(!strcmp(name,"owner-wait-deadline"))a5_wait_case(false,A5_EVENT_NONE,true);
    else if(!strcmp(name,"finish-wait-stop"))a5_wait_case(true,A5_EVENT_STOP,false);
    else if(!strcmp(name,"finish-wait-failure"))a5_wait_case(true,A5_EVENT_FAILURE,false);
    else if(!strcmp(name,"finish-wait-deadline"))a5_wait_case(true,A5_EVENT_NONE,true);
    else if(!strcmp(name,"send-stop"))a5_send_case(A5_EVENT_STOP,false,false);
    else if(!strcmp(name,"send-failure"))a5_send_case(A5_EVENT_FAILURE,false,false);
    else if(!strcmp(name,"send-deadline"))a5_send_case(A5_EVENT_NONE,true,false);
    else if(!strcmp(name,"send-short"))a5_send_case(A5_EVENT_NONE,false,true);
    else if(!strcmp(name,"admission-stop"))a5_admission_stop();
    else if(!strcmp(name,"pacer-stall"))a5_pacer_stall();
    else if(!strcmp(name,"oracle-suffix-credit"))a5_oracle_window();
    else if(!strcmp(name,"prep-timing-owner"))a5_prep_timing(false);
    else if(!strcmp(name,"prep-timing-finish"))a5_prep_timing(true);
    else if(!strcmp(name,"tick-deadline"))a5_boundary(0,A5_EVENT_NONE,true);
    else if(!strcmp(name,"tick-parent-stop"))a5_boundary(0,A5_EVENT_PARENT,false);
    else if(!strcmp(name,"tick-failure"))a5_boundary(0,A5_EVENT_FAILURE,false);
    else if(!strcmp(name,"prep-deadline"))a5_boundary(1,A5_EVENT_NONE,true);
    else if(!strcmp(name,"prep-parent-stop"))a5_boundary(1,A5_EVENT_PARENT,false);
    else if(!strcmp(name,"prep-ui-stop"))a5_boundary(1,A5_EVENT_STOP,false);
    else if(!strcmp(name,"prep-failure"))a5_boundary(1,A5_EVENT_FAILURE,false);
    else if(!strcmp(name,"lock-deadline"))a5_boundary(2,A5_EVENT_NONE,true);
    else if(!strcmp(name,"lock-parent-stop"))a5_boundary(2,A5_EVENT_PARENT,false);
    else if(!strcmp(name,"lock-failure"))a5_boundary(2,A5_EVENT_FAILURE,false);
    else if(!strcmp(name,"empty-target"))a5_empty_target(A5_EVENT_NONE,false);
    else if(!strcmp(name,"empty-target-stop"))a5_empty_target(A5_EVENT_STOP,false);
    else if(!strcmp(name,"empty-target-parent-stop"))a5_empty_target(A5_EVENT_PARENT,false);
    else if(!strcmp(name,"empty-target-failure"))a5_empty_target(A5_EVENT_FAILURE,false);
    else if(!strcmp(name,"empty-target-deadline"))a5_empty_target(A5_EVENT_NONE,true);
    else if(!strcmp(name,"finish-admission-parent-stop"))a5_finish_admission(A5_EVENT_PARENT,false);
    else if(!strcmp(name,"finish-admission-failure"))a5_finish_admission(A5_EVENT_FAILURE,false);
    else if(!strcmp(name,"finish-admission-deadline"))a5_finish_admission(A5_EVENT_NONE,true);
    else if(!strcmp(name,"owner-long-stall"))a5_long_stall();
    else if(!strcmp(name,"variable-owner-drain"))a5_variable();
    else if(!strcmp(name,"capture-read-failure"))a5_read_error();
    else if(!strcmp(name,"model-final-guard-failure"))a5_final_guard_error();
    else if(!strcmp(name,"closing-work-hard-caps"))a5_closing_caps();
    else if(!strcmp(name,"send-zero"))a5_send_case(A5_EVENT_NONE,false,2);
    else if(!strcmp(name,"send-negative"))a5_send_case(A5_EVENT_NONE,false,3);
    else if(!strcmp(name,"timed-short"))a5_timed_failure(1);
    else if(!strcmp(name,"timed-zero"))a5_timed_failure(2);
    else if(!strcmp(name,"timed-negative"))a5_timed_failure(3);
    else if(!strcmp(name,"timed-out-full"))a5_timed_output(0);
    else if(!strcmp(name,"timed-out-short"))a5_timed_output(1);
    else if(!strcmp(name,"timed-out-zero"))a5_timed_output(2);
    else if(!strcmp(name,"timed-out-negative"))a5_timed_output(3);
    else if(!strcmp(name,"tick-failed-flag"))a5_boundary(0,A5_EVENT_FAILED_FLAG,false);
    else if(!strcmp(name,"prep-failed-flag"))a5_boundary(1,A5_EVENT_FAILED_FLAG,false);
    else if(!strcmp(name,"lock-failed-flag"))a5_boundary(2,A5_EVENT_FAILED_FLAG,false);
    else if(!strcmp(name,"finish-wait-failed-flag"))a5_wait_case(true,A5_EVENT_FAILED_FLAG,false);
    else if(!strcmp(name,"empty-target-failed-flag"))a5_empty_target(A5_EVENT_FAILED_FLAG,false);
    else if(!strcmp(name,"finish-admission-failed-flag"))a5_finish_admission(A5_EVENT_FAILED_FLAG,false);
    else {fprintf(stderr,"unknown case: %s\n",name);return 2;}
    printf("PASS %s\n",name);return 0;
}
