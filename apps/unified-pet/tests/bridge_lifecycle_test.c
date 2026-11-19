/* I1 offline lifecycle regressions. The runner hash-verifies and compiles the
 * COMPLETE WS 1.6.1 task/start/abort/destroy bodies and the actual app adapter.
 * Init, event dispatch, RTOS, timer delivery, TLS/WS byte I/O and recv are explicit
 * synthetic seams. Host pthread exit really invokes the app's key destructor;
 * this is NOT ESP32/FreeRTOS cleanup timing, real TLS or physical qualification.
 * No socket, listener, provider, token file or device is opened. */
#include <assert.h>
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>
#include <sanitizer/asan_interface.h>
#include "bridge_transport.h"
#include "esp_transport_internal.h"
#include "esp_transport_ssl.h"
#include "esp_transport_ws.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#ifdef BRIDGE_RECEIVE_TEST
#include "bridge_protocol.h"
#include "live_protocol.h"
#include "mbedtls/base64.h"
#include "mbedtls/sha1.h"
#include "mbedtls/x509.h"
#endif

typedef void *TaskHandle_t;
TaskHandle_t xTaskGetCurrentTaskHandle(void){return (void*)(uintptr_t)pthread_self();}
typedef struct fixture_events {unsigned bits;} *EventGroupHandle_t;
typedef void *esp_event_loop_handle_t;
#ifndef BRIDGE_RECEIVE_TEST
typedef struct {int error_type,esp_ws_handshake_status_code;} esp_websocket_error_codes_t;
#endif
#define portMAX_DELAY 0x7fffffff
#define portTICK_PERIOD_MS 1
#define pdPASS pdTRUE
#define STOPPED_BIT 1U
#define CLOSE_FRAME_SENT_BIT 2U
#define REQUESTED_STOP_BIT 4U
#define WS_TRANSPORT_REDIRECT_HEADER_SUPPORT 1
#define WS_HTTP_REDIRECT(code) ((code)>=300&&(code)<400)
#define ESP_WS_CLIENT_STATE_CHECK(tag,c,action) do {if((c)->state<WEBSOCKET_STATE_INIT){action;}}while(0)
#include "pinned_client_types.inc"

struct fixture_lock {pthread_mutex_t mutex;bool io;};
struct fixture_timer {esp_timer_create_args_t args;bool active;};
struct fixture_ws {esp_transport_handle_t ssl;char *auth;int status;};
enum gate_id {G_BEGIN,G_FINISH,G_STOPPED,G_DESTRUCTOR,G_TIMER,G_TIMER_START,G_COUNT};
static struct {bool armed,reached,released;} gates[G_COUNT];
static pthread_mutex_t control=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed=PTHREAD_COND_INITIALIZER;
static _Thread_local bool sdk_thread,timer_thread;
static pthread_t worker;
static bool worker_exists,early_exit=true,creating_client;
static bool fail_create,fail_timer_start,fail_key,fail_setspecific,fail_self_destroy;
static bool fail_timer_stop,fail_timer_delete,connect_failure,peer_failure;
static atomic_uint task_create_attempts;
static atomic_uint connects,control_sends,payload_sends,closed_events,disconnect_events;
static atomic_uint clients_live,ws_live,ssl_live,locks_live,timers_live,destructors,start_tail;
static atomic_uint client_frees,app_frees;
static atomic_bool observe_io_contention,io_contended;
static atomic_int_fast64_t forced_clock;
static esp_event_handler_t event_handler;
static void *event_arg;
static esp_websocket_client_handle_t last_client;
static void (*app_destructor)(void*);
static char order[32];static size_t order_at;
static void *app_slot;static size_t app_size;static bool app_slot_live;
#ifdef BRIDGE_RECEIVE_TEST
static unsigned char rx_wire[200000],tx_wire[10000];
static size_t rx_size,rx_at,tx_size,tcp_split=8192;
static struct {size_t first,last;} fixture_headers[128];
static unsigned fixture_header_count;
static bool fixture_eof,fixture_socket=true;
static bool fault_raw_once;
static size_t fault_raw_at;
static int fault_raw_result;
static unsigned data_events,control_events,zero_control_events,continuation_events,split_events;
static size_t controlled_payload_begin;
static unsigned payload_reads,fault_payload_read,cancel_payload_read;
static int fault_payload_result,payload_waits[130];
static bool cancel_raw_once;
static size_t cancel_raw_at;
static int64_t payload_latency_us;
static bool fake_clock;
static int64_t fake_now;
static int64_t timer_start_delay_us;
static esp_websocket_event_data_t pending_event;
static int pending_id;static bool pending_valid;
static bool admit_on_data_dispatch;
static bool consume_ticket_before_dispatch;
static unsigned consumed_ticket_faults;
static int64_t data_dispatch_delay_us;
static int64_t nondata_dispatch_delay_us;
static bool fail_next_data_post;
static bool fail_next_disconnect_post,cert_event_probe;
static unsigned cert_error_events,cert_disconnect_events;
static int cert_flags_seen;
static unsigned data_post_count,fail_data_post_at;
static struct {esp_event_handler_t fn;void *arg;} handlers[2];
static unsigned handler_count;
static const char *WEBSOCKET_EVENTS="fixture websocket";
static int (*vendor_ws_destroy)(esp_transport_handle_t);
/* The actual pinned IDF event-post body executes for the allocator-failure
 * branch. Its allocator and RTOS/event-loop data structures are synthetic. */
typedef int BaseType_t;
typedef struct {union {void *ptr;} data;esp_event_base_t base;int32_t id;} esp_event_post_instance_t;
typedef struct {void *task,*running_task,*queue;SemaphoreHandle_t mutex;} esp_event_loop_instance_t;
#define ESP_EVENT_ANY_BASE NULL
#define ESP_EVENT_ANY_ID (-1)
#ifndef pdFALSE
#define pdFALSE 0
#endif
static esp_event_loop_instance_t post_loop;
static unsigned failed_allocations;
static void *event_fault_calloc(size_t n,size_t size){(void)n;(void)size;failed_allocations++;return NULL;}
static int xQueueSendToBack(void *q,const void *p,unsigned ticks){(void)q;(void)p;(void)ticks;assert(!"fault branch cannot enqueue");return 0;}
static void post_instance_delete(esp_event_post_instance_t *p){free(p->data.ptr);}
#define calloc event_fault_calloc
#define esp_event_post_to fixture_sdk_event_post_to
#define TAG "fixture event post"
#include "pinned_event_post.inc"
#undef TAG
#undef esp_event_post_to
#undef calloc
static esp_err_t esp_event_post_to(void *loop,esp_event_base_t base,int id,
                                  const void *event,size_t n,unsigned ticks){
    (void)loop;(void)ticks;assert(base==WEBSOCKET_EVENTS&&!pending_valid&&n==sizeof(pending_event));
    if(id==WEBSOCKET_EVENT_DATA&&++data_post_count==fail_data_post_at)fail_next_data_post=true;
    if(id==WEBSOCKET_EVENT_DATA&&fail_next_data_post){
        fail_next_data_post=false;return fixture_sdk_event_post_to(&post_loop,base,id,event,n,ticks);
    }
    if(id==WEBSOCKET_EVENT_DISCONNECTED&&fail_next_disconnect_post){
        fail_next_disconnect_post=false;return fixture_sdk_event_post_to(&post_loop,base,id,event,n,ticks);
    }
    pending_event=*(const esp_websocket_event_data_t*)event;pending_id=id;pending_valid=true;return ESP_OK;
}
#include "pinned_tls_get_clear.inc"
#endif

int64_t esp_timer_get_time(void){
    int64_t forced=atomic_load(&forced_clock);
    if(forced)return forced;
#ifdef BRIDGE_RECEIVE_TEST
    if(fake_clock)return fake_now;
#endif
    struct timespec ts;assert(!clock_gettime(CLOCK_MONOTONIC,&ts));
    return (int64_t)ts.tv_sec*1000000+ts.tv_nsec/1000;
}
static struct timespec wall_until(void){struct timespec ts;assert(!clock_gettime(CLOCK_REALTIME,&ts));ts.tv_sec+=5;return ts;}
static void arm(enum gate_id id){assert(!pthread_mutex_lock(&control));gates[id].armed=true;assert(!pthread_mutex_unlock(&control));}
static void at_gate(enum gate_id id){
    assert(!pthread_mutex_lock(&control));gates[id].reached=true;assert(!pthread_cond_broadcast(&changed));
    while(gates[id].armed&&!gates[id].released)assert(!pthread_cond_wait(&changed,&control));
    assert(!pthread_mutex_unlock(&control));
}
static void await(enum gate_id id){
    struct timespec until=wall_until();assert(!pthread_mutex_lock(&control));
    while(!gates[id].reached)assert(!pthread_cond_timedwait(&changed,&control,&until));
    assert(!pthread_mutex_unlock(&control));
}
static void release(enum gate_id id){
    assert(!pthread_mutex_lock(&control));gates[id].released=true;assert(!pthread_cond_broadcast(&changed));assert(!pthread_mutex_unlock(&control));
}
static void record(char c){
    assert(!pthread_mutex_lock(&control));assert(order_at+1<sizeof(order));order[order_at++]=c;order[order_at]=0;assert(!pthread_mutex_unlock(&control));
}
void vTaskDelay(TickType_t ticks){struct timespec ts={.tv_nsec=(long)ticks*1000000};assert(!nanosleep(&ts,NULL));}
static void vTaskDelete(TaskHandle_t h){assert(!h&&sdk_thread);pthread_exit(NULL);}
static void real_exit_destructor(void *value){
    at_gate(G_DESTRUCTOR);app_destructor(value); /* exact app task_deleted */
    atomic_fetch_add(&destructors,1); /* no value/context access after callback */
}
static int fixture_key_create(pthread_key_t *key,void(*fn)(void*)){
    if(fail_key)return ENOMEM;
    assert(!app_destructor);app_destructor=fn;return pthread_key_create(key,real_exit_destructor);
}
static int fixture_setspecific(pthread_key_t key,const void *value){return fail_setspecific?ENOMEM:pthread_setspecific(key,value);}
static void *app_calloc(size_t n,size_t size){
    assert(n==1);
    /* A denied second constructor allocates before the singleton CAS. Its
     * temporary allocation is real, separately freed, never the live slot. */
    if(app_slot_live)return calloc(n,size);
    if(!app_slot){app_slot=calloc(n,size);assert(app_slot);app_size=size;}
    assert(size==app_size);__asan_unpoison_memory_region(app_slot,app_size);
    memset(app_slot,0,app_size);app_slot_live=true;return app_slot;
}
static void app_free(void *p);
static int fixture_shutdown(int fd,int how){
#ifdef BRIDGE_RECEIVE_TEST
    assert(fd==12345&&how==SHUT_RDWR);fixture_socket=false;return 0;
#else
    (void)fd;(void)how;assert(!"synthetic socket must remain invalid");return -1;
#endif
}
#define pthread_key_create fixture_key_create
#define pthread_setspecific fixture_setspecific
#define calloc app_calloc
#define free app_free
#define shutdown fixture_shutdown
#ifdef BRIDGE_TEST_TRANSPORT_MUTANT
#include "bridge-transport-mutant.inc"
#else
#include "../main/bridge_transport.c"
#endif
#undef shutdown
#undef free
#undef calloc
#undef pthread_setspecific
#undef pthread_key_create

static void app_free(void *p){
    if(p!=app_slot){free(p);return;}
    assert(p==app_slot&&app_slot_live&&!atomic_load(&chain));
    assert(!atomic_load(&clients_live)&&!atomic_load(&ws_live)&&!atomic_load(&ssl_live));
    assert(!atomic_load(&locks_live)&&!atomic_load(&timers_live));
    for(size_t i=0;i<app_size;i++)assert(((unsigned char*)p)[i]==0);
    record('A');atomic_fetch_add(&app_frees,1);app_slot_live=false;
    /* Quarantine is only an allocator seam, not lifetime proof. Poison between
     * frees/reuse so a stale callback in the gap is still an ASan failure. */
    __asan_poison_memory_region(app_slot,app_size);
}
static void wait_finishing(bridge_transport_t *t){
    int64_t until=esp_timer_get_time()+5000000;
    while(!bridge_transport_finished(t)){assert(esp_timer_get_time()<until);sched_yield();}
}
static void *task_entry(void *arg);
static int xTaskCreate(void(*fn)(void*),const char *name,unsigned stack,void *arg,unsigned prio,TaskHandle_t *out){
    (void)name;assert(stack==8192&&prio==5&&!worker_exists);
    atomic_fetch_add(&task_create_attempts,1);
    if(fail_create)return 0;
    extern void fixture_check_task_function(void(*)(void*));fixture_check_task_function(fn);
    *out=(void*)(uintptr_t)1;worker_exists=true;
    assert(!pthread_create(&worker,NULL,task_entry,arg));
    if(early_exit){
        /* Force FINISH BEFORE xTaskCreate returns to SDK start. Without the
         * owner handshake SDK self-destruction races start's ClearBits tail. */
        wait_finishing(atomic_load(&chain));
        assert(!atomic_load(&client_frees)&&!atomic_load(&destructors));
        assert(!atomic_load(&((bridge_transport_t*)event_arg)->owner_done));
    }else await(G_BEGIN);
    return pdTRUE;
}
static unsigned xEventGroupClearBits(EventGroupHandle_t g,unsigned bits){
    assert(g);unsigned old=g->bits;g->bits&=~bits;
    if(!sdk_thread&&bits==(STOPPED_BIT|CLOSE_FRAME_SENT_BIT|REQUESTED_STOP_BIT))atomic_fetch_add(&start_tail,1);
    return old;
}
static unsigned xEventGroupSetBits(EventGroupHandle_t g,unsigned bits){
    assert(g);g->bits|=bits;
    if(sdk_thread&&(bits&STOPPED_BIT))at_gate(G_STOPPED);
    /* Explicit late group access after waking the observer. */
    return g->bits;
}
static unsigned xEventGroupGetBits(EventGroupHandle_t g){assert(g);return g->bits;}
static unsigned xEventGroupWaitBits(EventGroupHandle_t g,unsigned b,bool clear,bool all,unsigned ms){
#ifdef BRIDGE_RECEIVE_TEST
    (void)clear;(void)all;(void)ms;assert(g);return g->bits&b;
#else
    (void)g;(void)b;(void)clear;(void)all;(void)ms;assert(!"unexpected SDK wait/reconnect");return 0;
#endif
}
static void vEventGroupDelete(EventGroupHandle_t g){assert(g);free(g);}
static int esp_event_loop_delete(void *loop){assert(!loop);return ESP_OK;}
static int esp_event_loop_run(void *loop,unsigned ticks){
    (void)loop;(void)ticks;
#ifdef BRIDGE_RECEIVE_TEST
    if(pending_valid){
        pending_valid=false;
        if(cert_event_probe&&pending_id==WEBSOCKET_EVENT_ERROR){
            /* No other error_handle field is initialized at this event. */
            assert(pending_event.error_handle.error_type==WEBSOCKET_ERROR_TYPE_NONE);
            esp_tls_error_handle_t h=esp_transport_get_error_handle(atomic_load(&chain)->ws);
            assert(h&&h->esp_tls_flags==cert_flags_seen);cert_error_events++;
        }
        if(cert_event_probe&&pending_id==WEBSOCKET_EVENT_DISCONNECTED){
            assert(pending_event.error_handle.error_type==WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT);
            assert(pending_event.error_handle.esp_tls_cert_verify_flags==cert_flags_seen);
            esp_tls_error_handle_t h=esp_transport_get_error_handle(atomic_load(&chain)->ws);
            assert(h&&!h->last_error&&!h->esp_tls_error_code&&!h->esp_tls_flags);cert_disconnect_events++;
        }
        if(pending_id!=WEBSOCKET_EVENT_DATA&&nondata_dispatch_delay_us){
            assert(fake_clock);fake_now+=nondata_dispatch_delay_us;nondata_dispatch_delay_us=0;
        }
        if(pending_id==WEBSOCKET_EVENT_DATA){
            if(data_dispatch_delay_us){assert(fake_clock);fake_now+=data_dispatch_delay_us;data_dispatch_delay_us=0;}
            if(admit_on_data_dispatch){
                admit_on_data_dispatch=false;
                assert(bridge_transport_admit_control(atomic_load(&chain),esp_timer_get_time()+1000000));
            }
            if(consume_ticket_before_dispatch){
                bool after;int64_t at;
                consume_ticket_before_dispatch=false;
                assert(bridge_transport_take_data(atomic_load(&chain),&after,&at));
                consumed_ticket_faults++;
            }
            assert(pending_event.payload_offset>=0&&pending_event.payload_len>=0);
            data_events++;
            if(pending_event.op_code>=8){
                control_events++;if(pending_event.data_len==0)zero_control_events++;
            }
            if(pending_event.op_code==0)continuation_events++;
            if(pending_event.payload_offset>0)split_events++;
        }
        for(unsigned i=0;i<handler_count;i++)
            handlers[i].fn(handlers[i].arg,WEBSOCKET_EVENTS,pending_id,&pending_event);
    }
#endif
    return ESP_OK;
}
static int stop_wait_task(esp_websocket_client_handle_t c){(void)c;assert(!"public stop/destroy while task active");return ESP_FAIL;}
static esp_err_t esp_websocket_client_create_transport(esp_websocket_client_handle_t c){(void)c;assert(!"external WS must be preserved");return ESP_FAIL;}
static esp_err_t esp_websocket_client_set_uri(esp_websocket_client_handle_t c,const char *uri){(void)c;(void)uri;assert(!"redirect is not authorized");return ESP_FAIL;}
static const char *esp_err_to_name(int e){(void)e;return "synthetic error";}
static unsigned esp_get_free_heap_size(void){return 123456;}
static esp_err_t esp_websocket_client_error(esp_websocket_client_handle_t c,const char *fmt,...){(void)c;(void)fmt;return ESP_OK;}
#ifndef BRIDGE_RECEIVE_TEST
static esp_err_t esp_websocket_client_dispatch_event(esp_websocket_client_handle_t c,esp_websocket_event_id_t id,const char *data,int n){
    (void)data;(void)n;assert(c&&sdk_thread&&event_handler);
    if(id==WEBSOCKET_EVENT_FINISH)at_gate(G_FINISH);
    event_handler(event_arg,"fixture websocket",id,NULL); /* synchronous SDK event-loop seam */
    if(id==WEBSOCKET_EVENT_BEGIN)at_gate(G_BEGIN);
    if(id==WEBSOCKET_EVENT_CLOSED)atomic_fetch_add(&closed_events,1);
    if(id==WEBSOCKET_EVENT_DISCONNECTED)atomic_fetch_add(&disconnect_events,1);
    return ESP_OK;
}
static esp_err_t esp_websocket_client_recv(esp_websocket_client_handle_t c){
    assert(c&&sdk_thread);if(peer_failure)return ESP_FAIL;
    c->state=WEBSOCKET_STATE_CLOSING;return ESP_OK; /* synthetic peer close frame */
}
#endif
static void sdk_free(void *p){
    if(p&&p==last_client){
        /* The exact SDK destructor must run before external WS/SSL disposal. */
        assert(atomic_load(&ws_live)==1&&atomic_load(&ssl_live)==1);
        record('C');last_client=NULL;atomic_fetch_sub(&clients_live,1);atomic_fetch_add(&client_frees,1);
    }
    free(p);
}
#define free sdk_free
#define esp_websocket_client_destroy_on_exit sdk_destroy_on_exit
static const char *TAG="websocket_client";
/* SDK's own 64-bit host signed-size warnings, not app/fixture exceptions. */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wsign-compare"
#include "pinned_client_lifecycle.inc"
#pragma GCC diagnostic pop
#undef esp_websocket_client_destroy_on_exit
#undef free
void fixture_check_task_function(void(*fn)(void*)){assert(fn==esp_websocket_client_task);}
static void *task_entry(void *arg){sdk_thread=true;esp_websocket_client_task(arg);assert(!"SDK task returned rather than deleted itself");return NULL;}
esp_err_t esp_websocket_client_destroy_on_exit(esp_websocket_client_handle_t c){return fail_self_destroy?ESP_FAIL:sdk_destroy_on_exit(c);}
static void join_worker(void){if(worker_exists){assert(!pthread_join(worker,NULL));worker_exists=false;}}

SemaphoreHandle_t xSemaphoreCreateRecursiveMutex(void){
    SemaphoreHandle_t s=calloc(1,sizeof(*s));assert(s);pthread_mutexattr_t a;
    assert(!pthread_mutexattr_init(&a));assert(!pthread_mutexattr_settype(&a,PTHREAD_MUTEX_RECURSIVE));
    assert(!pthread_mutex_init(&s->mutex,&a));assert(!pthread_mutexattr_destroy(&a));
    s->io=!creating_client;atomic_fetch_add(&locks_live,1);return s;
}
int xSemaphoreTakeRecursive(SemaphoreHandle_t s,unsigned ticks){
    assert(s);if(timer_thread)at_gate(G_TIMER);
    if(!ticks)return pthread_mutex_trylock(&s->mutex)==0;
    if(s->io&&atomic_load(&observe_io_contention)){
        assert(pthread_mutex_trylock(&s->mutex)==EBUSY);
        atomic_store(&io_contended,true);
    }
    assert(!pthread_mutex_lock(&s->mutex));return pdTRUE;
}
int xSemaphoreGiveRecursive(SemaphoreHandle_t s){assert(s&&!pthread_mutex_unlock(&s->mutex));return pdTRUE;}
void vSemaphoreDelete(SemaphoreHandle_t s){
    assert(s&&!pthread_mutex_destroy(&s->mutex));
    if(s->io){assert(!atomic_load(&ws_live)&&!atomic_load(&ssl_live));record('M');}
    atomic_fetch_sub(&locks_live,1);free(s);
}
esp_err_t esp_timer_create(const esp_timer_create_args_t *a,esp_timer_handle_t *out){
    assert(a&&a->callback&&a->arg);*out=calloc(1,sizeof(**out));assert(*out);(*out)->args=*a;atomic_fetch_add(&timers_live,1);return ESP_OK;
}
esp_err_t esp_timer_start_once(esp_timer_handle_t t,uint64_t us){
    assert(t&&us);if(fail_timer_start)return ESP_FAIL;t->active=true;
    at_gate(G_TIMER_START);
#ifdef BRIDGE_RECEIVE_TEST
    if(timer_start_delay_us){assert(fake_clock);fake_now+=timer_start_delay_us;timer_start_delay_us=0;}
#endif
    return ESP_OK;
}
esp_err_t esp_timer_stop(esp_timer_handle_t t){assert(t);if(fail_timer_stop)return ESP_FAIL;bool active=t->active;t->active=false;return active?ESP_OK:ESP_ERR_INVALID_STATE;}
esp_err_t esp_timer_delete(esp_timer_handle_t t){assert(t&&!t->active);if(fail_timer_delete)return ESP_FAIL;atomic_fetch_sub(&timers_live,1);free(t);return ESP_OK;}
void esp_log_level_set(const char *tag,int level){(void)tag;assert(level==ESP_LOG_NONE);}
void fixture_log(const char *tag,const char *fmt,...){(void)tag;(void)fmt;} /* no tokens/transcripts emitted */
esp_err_t esp_crt_bundle_attach(void *p){(void)p;return ESP_OK;}

static esp_tls_last_error_t fixture_connect_error;
static int raw_connect(esp_transport_handle_t t,const char *h,int p,int ms){
    assert(h&&p==9443&&ms>0);atomic_fetch_add(&connects,1);
    if(connect_failure)esp_transport_set_errors(t,&fixture_connect_error);
    return connect_failure?-1:0;
}
static int raw_read(esp_transport_handle_t t,char *b,int n,int ms){
    (void)t;(void)ms;
#ifdef BRIDGE_RECEIVE_TEST
    assert(n>0&&b);
    /* Cancellation inside an admitted read does not undo bytes that the TLS
     * operation subsequently returns to the parser. */
    if(cancel_raw_once&&rx_at==cancel_raw_at){
        cancel_raw_once=false;bridge_transport_cancel(atomic_load(&chain));
    }
    if(fault_raw_once&&rx_at==fault_raw_at){fault_raw_once=false;return fault_raw_result;}
    if(controlled_payload_begin&&rx_at>=controlled_payload_begin){
        assert(payload_reads<sizeof(payload_waits)/sizeof(*payload_waits));
        payload_waits[payload_reads++]=ms;
        if(fault_payload_read==payload_reads)return fault_payload_result;
        if(payload_latency_us){
            assert(fake_clock&&ms>=0);
            if(payload_latency_us>(int64_t)ms*1000){
                fake_now+=(int64_t)ms*1000;return 0;
            }
            fake_now+=payload_latency_us;
        }
        if(cancel_payload_read==payload_reads)bridge_transport_cancel(atomic_load(&chain));
    }
    if(rx_at==rx_size)return fixture_eof?-1:0;
    size_t count=rx_size-rx_at;if(count>(size_t)n)count=(size_t)n;
    /* Positive-path frames have deliberately complete headers: the pinned
     * parser's exact header reads cannot be forced to one byte and still be
     * expected to succeed. Payload splitting remains active. Hand-built
     * partial-header cases below do not register a complete header here. */
    bool header=false;
    for(unsigned i=0;i<fixture_header_count;i++){
        if(rx_at>=fixture_headers[i].first&&rx_at<fixture_headers[i].last){
            size_t remaining=fixture_headers[i].last-rx_at;
            if(count>remaining)count=remaining;
            header=true;break;
        }
    }
    if(!header&&count>tcp_split)count=tcp_split;
    memcpy(b,rx_wire+rx_at,count);rx_at+=count;return (int)count;
#else
    (void)b;(void)n;assert(!"no TLS byte read in lifecycle seam");return -1;
#endif
}
static int raw_write(esp_transport_handle_t t,const char *b,int n,int ms){
    (void)t;(void)ms;
#ifdef BRIDGE_RECEIVE_TEST
    assert(n>=0&&tx_size+(size_t)n<=sizeof(tx_wire));
    memcpy(tx_wire+tx_size,b,(size_t)n);tx_size+=(size_t)n;return n;
#else
    (void)b;(void)n;atomic_fetch_add(&payload_sends,1);return -1;
#endif
}
static int raw_poll(esp_transport_handle_t t,int ms){
    (void)t;(void)ms;
#ifdef BRIDGE_RECEIVE_TEST
    return rx_at<rx_size?1:fixture_eof?-1:0;
#else
    return 1;
#endif
}
static int raw_close(esp_transport_handle_t t){
    (void)t;
#ifdef BRIDGE_RECEIVE_TEST
    fixture_socket=false;
#endif
    return 0;
}
static int raw_poll_write(esp_transport_handle_t t,int ms){
#ifdef BRIDGE_RECEIVE_TEST
    (void)t;(void)ms;return fixture_socket?1:-1;
#else
    return raw_poll(t,ms);
#endif
}
static int raw_socket(esp_transport_handle_t t){
    (void)t;
#ifdef BRIDGE_RECEIVE_TEST
    return fixture_socket?12345:-1; /* synthetic identifier, never a host FD */
#else
    return -1;
#endif
}
static int raw_destroy(esp_transport_handle_t t){assert(!atomic_load(&ws_live));record('S');atomic_fetch_sub(&ssl_live,1);free(t->foundation->error_handle);free(t->foundation);return 0;}
esp_transport_handle_t esp_transport_ssl_init(void){
    esp_transport_handle_t t=esp_transport_init();assert(t);t->foundation=calloc(1,sizeof(*t->foundation));assert(t->foundation);
    t->foundation->error_handle=calloc(1,sizeof(*t->foundation->error_handle));assert(t->foundation->error_handle);
    esp_transport_set_func(t,raw_connect,raw_read,raw_write,raw_close,raw_poll,raw_poll_write,raw_destroy);t->_get_socket=raw_socket;atomic_fetch_add(&ssl_live,1);return t;
}
void esp_transport_ssl_crt_bundle_attach(esp_transport_handle_t t,esp_err_t(*f)(void*)){assert(t&&f==esp_crt_bundle_attach);}
void esp_transport_ssl_set_common_name(esp_transport_handle_t t,const char *s){assert(t&&s);}
#ifdef BRIDGE_RECEIVE_TEST
/* The receive variant links UNCHANGED transport_ws.c, not the lifecycle stub. */
esp_transport_handle_t fixture_vendor_ws_init(esp_transport_handle_t);
static int observed_ws_destroy(esp_transport_handle_t t){
    assert(!atomic_load(&clients_live));record('W');atomic_fetch_sub(&ws_live,1);
    return vendor_ws_destroy(t);
}
esp_transport_handle_t esp_transport_ws_init(esp_transport_handle_t ssl){
    esp_transport_handle_t t=fixture_vendor_ws_init(ssl);assert(t);
    vendor_ws_destroy=t->_destroy;t->_destroy=observed_ws_destroy;
    atomic_fetch_add(&ws_live,1);return t;
}
struct timeval*esp_transport_utils_ms_to_timeval(int ms,struct timeval *t){
    t->tv_sec=ms/1000;t->tv_usec=ms%1000*1000;return t;
}
#else
static int ws_connect(esp_transport_handle_t t,const char *h,int p,int ms){struct fixture_ws *w=t->data;int r=esp_transport_connect(w->ssl,h,p,ms);w->status=r?0:101;return r;}
static int ws_close(esp_transport_handle_t t){struct fixture_ws *w=t->data;return esp_transport_close(w->ssl);}
static int ws_poll(esp_transport_handle_t t,int ms){(void)t;return ms?1:0;}
static int ws_destroy(esp_transport_handle_t t){assert(!atomic_load(&clients_live));record('W');atomic_fetch_sub(&ws_live,1);struct fixture_ws *w=t->data;free(w->auth);free(w);return 0;}
esp_transport_handle_t esp_transport_ws_init(esp_transport_handle_t ssl){
    esp_transport_handle_t t=esp_transport_init();assert(t);struct fixture_ws *w=calloc(1,sizeof(*w));assert(w);w->ssl=ssl;t->data=w;
    esp_transport_set_func(t,ws_connect,raw_read,raw_write,ws_close,ws_poll,ws_poll,ws_destroy);atomic_fetch_add(&ws_live,1);return t;
}
esp_err_t esp_transport_ws_set_config(esp_transport_handle_t t,const esp_transport_ws_config_t *c){struct fixture_ws *w=t->data;w->auth=strdup(c->auth);assert(w->auth);return ESP_OK;}
int esp_transport_ws_get_upgrade_request_status(esp_transport_handle_t t){return ((struct fixture_ws*)t->data)->status;}
/* Public getter ABI needed by the production control wrapper. Lifecycle-only
 * cases still use a synthetic WS; receive cases above link the real getters. */
ws_transport_opcodes_t esp_transport_ws_get_read_opcode(esp_transport_handle_t t){assert(t);return WS_TRANSPORT_OPCODES_NONE;}
bool esp_transport_ws_get_fin_flag(esp_transport_handle_t t){assert(t);return true;}
int esp_transport_ws_get_read_payload_len(esp_transport_handle_t t){assert(t);return 0;}
int esp_transport_ws_send_raw(esp_transport_handle_t t,ws_transport_opcodes_t op,const char *b,int n,int ms){
    (void)t;(void)b;(void)ms;assert((op&0xf)==WS_TRANSPORT_OPCODES_CLOSE&&n==0);atomic_fetch_add(&control_sends,1);return n;
}
int esp_transport_ws_poll_connection_closed(esp_transport_handle_t t,int ms){(void)t;(void)ms;return 1;}
char *esp_transport_ws_get_redir_uri(esp_transport_handle_t t){(void)t;assert(!"redirect not permitted");return NULL;}
#endif

esp_websocket_client_handle_t esp_websocket_client_init(const esp_websocket_client_config_t *c){
    assert(c->disable_auto_reconnect&&!c->enable_close_reconnect&&c->ext_transport);
    esp_websocket_client_handle_t h=calloc(1,sizeof(*h));assert(h);h->config=calloc(1,sizeof(*h->config));assert(h->config);
    h->config->host=strdup(c->host);h->config->path=strdup(c->path);h->config->port=c->port;
    h->config->ext_transport=c->ext_transport;h->config->network_timeout_ms=c->network_timeout_ms;
    h->config->auto_reconnect=!c->disable_auto_reconnect;h->config->close_reconnect=c->enable_close_reconnect;
    h->config->task_stack=c->task_stack;h->config->task_prio=c->task_prio;
    h->config->ping_interval_sec=c->ping_interval_sec;h->config->pingpong_timeout_sec=c->pingpong_timeout_sec;
    h->status_bits=calloc(1,sizeof(*h->status_bits));assert(h->status_bits);h->status_bits->bits=STOPPED_BIT;
    creating_client=true;h->lock=xSemaphoreCreateRecursiveMutex();creating_client=false;
    h->tx_buffer=calloc(1,32);h->rx_buffer=calloc(1,32);assert(h->tx_buffer&&h->rx_buffer);
    last_client=h;atomic_fetch_add(&clients_live,1);return h;
}
esp_err_t esp_websocket_register_events(esp_websocket_client_handle_t c,int id,esp_event_handler_t fn,void *arg){
    assert(c&&id==WEBSOCKET_EVENT_ANY);
#ifdef BRIDGE_RECEIVE_TEST
    assert(handler_count<2);handlers[handler_count].fn=fn;handlers[handler_count++].arg=arg;
#endif
    event_handler=fn;event_arg=arg;return ESP_OK;
}
static const setup_bridge_t cfg={.version=1,.enabled=1,.ip={192,168,1,50},.port=9443,.fqdn="pet.example.net"};
static const char *synthetic_token="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
static bridge_transport_t *make(void){return bridge_transport_new(&cfg,synthetic_token,esp_timer_get_time()+60000000);}
static void no_automatic_work(unsigned attempts){assert(atomic_load(&connects)==attempts&&!atomic_load(&payload_sends));}
static void clean(void){
    assert(!atomic_load(&chain)&&!app_slot_live&&!cutoff_owner);
    assert(!atomic_load(&clients_live)&&!atomic_load(&ws_live)&&!atomic_load(&ssl_live));
    assert(!atomic_load(&locks_live)&&!atomic_load(&timers_live));
    assert(!strcmp(order,"CWSMA"));
}
static void reset_observers(void){
    clean();assert(!worker_exists);memset(gates,0,sizeof(gates));order_at=0;order[0]=0;
    atomic_store(&client_frees,0);atomic_store(&destructors,0);atomic_store(&start_tail,0);
}
static void finish_and_dispose(bridge_transport_t *t){
    assert(bridge_transport_dispose(t,esp_timer_get_time()+3000000));join_worker();clean();
}
static void assert_retained(bridge_transport_t *t){
    assert(atomic_load(&chain)==t&&app_slot_live&&atomic_load(&ws_live)==1&&atomic_load(&ssl_live)==1);
    assert(!bridge_transport_new(&cfg,synthetic_token,esp_timer_get_time()+60000000));
    assert(bridge_transport_start(t)==ESP_ERR_INVALID_STATE);
}
static esp_timer_create_args_t delayed;
static void *timer_entry(void *unused){(void)unused;timer_thread=true;delayed.callback(delayed.arg);return NULL;}
static atomic_bool dispose_done;
static bool dispose_result;
static bridge_transport_t *dispose_target;
static void *dispose_entry(void *unused){(void)unused;dispose_result=bridge_transport_dispose(dispose_target,esp_timer_get_time()+3000000);atomic_store(&dispose_done,true);return NULL;}

#ifdef BRIDGE_RECEIVE_TEST
/* Focused receive integration, not a generic RTOS emulator. These bounded
 * queues/buffers are the same 4-pointer/zero-wait boundaries as the runtime
 * fixture. The callback and owner consumer below are literal source excerpts. */
typedef struct {char *text;int64_t received_at;bool after_local_control;} fixture_frame_t;
typedef struct {fixture_frame_t items[4];unsigned n;} *QueueHandle_t;
typedef struct {unsigned char bytes[96000];size_t n;} *StreamBufferHandle_t;
typedef struct {unsigned unused;} StaticStreamBuffer_t;
typedef unsigned EventBits_t;
typedef struct {unsigned unused;} audio_session_t;
#define BIT0 1U
#define BIT1 2U
#define BIT2 4U
#define BIT3 8U
#define pdFALSE 0
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
static char sent_app[8][7000];static unsigned app_sends,inhibits;
static struct fixture_lock model_guard={.mutex=PTHREAD_MUTEX_INITIALIZER};
static int xQueueSend(QueueHandle_t q,const void *p,unsigned ticks){
    assert(!ticks);if(q->n==4)return 0;memcpy(&q->items[q->n++],p,sizeof(q->items[0]));return pdTRUE;
}
static int xQueueReceive(QueueHandle_t q,void *out,unsigned ticks){
    (void)ticks;if(!q->n)return 0;memcpy(out,&q->items[0],sizeof(q->items[0]));
    memmove(q->items,q->items+1,--q->n*sizeof(*q->items));return pdTRUE;
}
static unsigned uxQueueMessagesWaiting(QueueHandle_t q){return q->n;}
static size_t xStreamBufferSend(StreamBufferHandle_t s,const void *p,size_t n,unsigned ticks){
    assert(!ticks);if(n>sizeof(s->bytes)-s->n)n=sizeof(s->bytes)-s->n;
    memcpy(s->bytes+s->n,p,n);s->n+=n;return n;
}
static size_t xStreamBufferReceive(StreamBufferHandle_t s,void *p,size_t n,unsigned ticks){
    assert(!ticks);if(n>s->n)n=s->n;memcpy(p,s->bytes,n);
    memmove(s->bytes,s->bytes+n,s->n-n);s->n-=n;return n;
}
static size_t xStreamBufferBytesAvailable(StreamBufferHandle_t s){return s->n;}
int xSemaphoreTake(SemaphoreHandle_t s,unsigned ticks){(void)ticks;assert(s==&model_guard);return pthread_mutex_lock(&s->mutex)==0;}
int xSemaphoreGive(SemaphoreHandle_t s){assert(s==&model_guard&&!pthread_mutex_unlock(&s->mutex));return pdTRUE;}
static void *heap_caps_malloc(size_t n,unsigned flags){(void)flags;return malloc(n);}
static void audio_stream_inhibit(void){inhibits++;}
static bool fixture_post_upgrade(bridge_transport_t *t){return t&&!atomic_load(&t->cancelled);}
static int esp_websocket_client_send_text(esp_websocket_client_handle_t c,const char *json,size_t n,unsigned ticks){
    (void)ticks;assert(c&&app_sends<8&&n<sizeof(sent_app[0]));
    assert(!pthread_mutex_trylock(&model_guard.mutex));assert(!pthread_mutex_unlock(&model_guard.mutex));
    memcpy(sent_app[app_sends],json,n);sent_app[app_sends++][n]=0;return (int)n;
}
/* A successful HTTP/TLS upgrade and send completion are injected boundaries,
 * not falsely claimed actual TLS. Actual post-upgrade WS bytes/read callbacks
 * do pass through the production bounded transport adapter. */
#define bridge_transport_upgraded fixture_post_upgrade
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#include "receive_app.inc"
#pragma GCC diagnostic pop
#undef bridge_transport_upgraded
#include "receive_golden.inc"
static bridge_voice_t receive_context;
static struct {fixture_frame_t items[4];unsigned n;} receive_queue;
static struct {unsigned char bytes[96000];size_t n;} capture_buffer,playback_buffer;
static struct fixture_events audio_events={.bits=BV_IDLE};
static char assembly_storage[BV_FRAME],append_json[7000];
static unsigned char append_storage[BV_APPEND],decode_storage[BV_APPEND];
static void add_frame(unsigned op,bool fin,const void *payload,size_t n){
    assert(rx_at==0&&n<65536&&rx_size+n+4<sizeof(rx_wire));
    assert(fixture_header_count<sizeof(fixture_headers)/sizeof(*fixture_headers));
    unsigned index=fixture_header_count++;
    fixture_headers[index].first=rx_size;
    rx_wire[rx_size++]=(unsigned char)((fin?0x80:0)|op);
    if(n<126)rx_wire[rx_size++]=(unsigned char)n;
    else{rx_wire[rx_size++]=126;rx_wire[rx_size++]=(unsigned char)(n>>8);rx_wire[rx_size++]=(unsigned char)n;}
    fixture_headers[index].last=rx_size;
    if(n)memcpy(rx_wire+rx_size,payload,n);rx_size+=n;
}
static void add_text(const char *json){add_frame(1,true,json,strlen(json));}
static void add_message(const char *json,bool fragmented){
    size_t n=strlen(json);
    if(!fragmented){add_text(json);return;}
    size_t split=n/2;assert(split>0);
    add_frame(1,false,json,split);
    add_frame(9,true,"ping",4);add_frame(10,true,NULL,0);
    add_frame(0,true,json+split,n-split);
}
static void empty_wire(void){assert(rx_at==rx_size);rx_at=rx_size=0;fixture_header_count=0;fixture_eof=false;}
static void read_batch(bool eof){
    fixture_eof=eof;unsigned calls=0;
    while(rx_at<rx_size&&!atomic_load(&receive_context.failed)){
        assert(++calls<20000);int result=esp_websocket_client_recv(receive_context.ws);
        if(result!=ESP_OK){assert(eof);break;}
    }
}
static void receive_eof(void){
    assert(rx_at==rx_size);fixture_eof=true;
    assert(esp_websocket_client_recv(receive_context.ws)==ESP_FAIL);
    assert(xSemaphoreTakeRecursive(receive_context.ws->lock,0)==pdTRUE);
    assert(esp_websocket_client_abort_connection(receive_context.ws,WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT)==ESP_OK);
    assert(xSemaphoreGiveRecursive(receive_context.ws->lock)==pdTRUE);
    assert(!atomic_load(&receive_context.connected));
}
static void consume_batch(void){
    unsigned count=0;
    while(receive_queue.n&&!atomic_load(&receive_context.failed)){
        assert(++count<=4);production_consume_one(&receive_context);
    }
}
static bridge_transport_t *receive_init(void){
    bridge_transport_t *t=make();assert(t);
    bridge_voice_t *c=&receive_context;guard=&model_guard;
    c->transport=t;c->ws=bridge_transport_client(t);c->ws->transport=t->ws;
    c->ws->state=WEBSOCKET_STATE_CONNECTED;c->ws->buffer_size=32;
    c->frames=(void*)&receive_queue;c->capture=(void*)&capture_buffer;c->playback=(void*)&playback_buffer;
    c->events=&audio_events;c->assembly_memory=assembly_storage;c->append_json=append_json;
    c->append_pcm=append_storage;c->decoded=decode_storage;
    c->hard=t->deadline;c->work=t->deadline-2000000;
    atomic_store(&c->active,true);atomic_store(&c->accepting,true);atomic_store(&c->connected,true);
    bridge_init(&c->model);bridge_command(&c->model,BR_CMD_CONNECT,0);
    assert(esp_websocket_register_events(c->ws,WEBSOCKET_EVENT_ANY,ws_event,c)==ESP_OK);
    sdk_thread=true;return t; /* direct exact recv, NOT the SDK task/owner scheduler */
}
static void setup_preview(bool fragmented){
    bridge_voice_t *c=&receive_context;
    add_message(g_started,fragmented);add_message(g_began,fragmented);
    read_batch(false);consume_batch();empty_wire();
    assert(!atomic_load(&c->failed)&&c->model.phase==BR_CAPTURING);
    assert(c->actions==(BR_ACT_BEGIN|BR_ACT_CAPTURE));
    assert(!strcmp(c->model.label,"Receive fixture")&&!strcmp(c->model.short_id,"1234abcd"));
    assert(send_action(c,BR_ACT_BEGIN));
    /* Finite known PCM source/idle acknowledgement is synthetic, but append,
     * base64, deadline/pacing and capture_stop helpers below are actual source. */
    memcpy(c->append_pcm,g_pcm,sizeof(g_pcm));int64_t cursor=0;
    assert(append_audio(c,sizeof(g_pcm),&cursor));
    assert(bridge_command(&c->model,BR_CMD_FINISH_CAPTURE,0)==BR_ACT_FINISH_CAPTURE);
    assert(finish_capture(c,&cursor));
    assert(app_sends==3&&strstr(sent_app[0],"pet.turn.begin")&&
           strstr(sent_app[1],"session.input_audio.append")&&strstr(sent_app[2],"pet.turn.capture_stop"));
    assert(!c->model.confirmed&&c->model.send==BR_SEND_NONE);
    add_message(g_preview,fragmented);read_batch(false);consume_batch();empty_wire();
    assert(!atomic_load(&c->failed)&&c->model.phase==BR_PREVIEW&&!strcmp(c->model.text,g_text));
    assert(c->model.revision==1&&!c->model.confirmed&&app_sends==3);
    c->actions=0;
}
static void confirm_fixture_once(void){
    bridge_voice_t *c=&receive_context;
    /* Explicit synthetic UI action only, never a callback-driven confirmation. */
    assert(bridge_command(&c->model,BR_CMD_CONFIRM,1)==BR_ACT_CONFIRM);
    assert(send_action(c,BR_ACT_CONFIRM));assert(app_sends==4);
    add_text(g_accepted);read_batch(false);consume_batch();empty_wire();
    assert(c->model.send==BR_SEND_ACCEPTED&&c->model.phase==BR_AWAIT_REPLY);
}
static void check_control_replies(unsigned expected){
    assert(!atomic_load(&control_sends)); /* lifecycle WS stub was not called */
    size_t at=0;unsigned count=0;
    while(at<tx_size){
        assert(at+6<=tx_size&&tx_wire[at]==0x8a&&(tx_wire[at+1]&0x80));
        size_t n=tx_wire[at+1]&127;assert((n==0||n==4)&&at+6+n<=tx_size);
        for(size_t i=0;i<n;i++){
            unsigned char observed=tx_wire[at+6+i]^tx_wire[at+2+i%4];
            if(observed!=(unsigned char)"ping"[i])
                fprintf(stderr,"RX PONG echo mismatch: tcp_read_cap=%zu pong=%u byte=%zu expected=0x%02x actual=0x%02x\n",
                        tcp_split,count,i,(unsigned char)"ping"[i],observed);
            assert(observed==(unsigned char)"ping"[i]);
        }
        at+=6+n;count++;
    }
    assert(at==tx_size&&count==expected);
}
static void check_one_pong(const unsigned char *payload,size_t n){
    fprintf(stderr,"CONTROL echo: expected_payload=%zu wire_bytes=%zu header=%02x %02x DATA=%u controls=%u\n",
            n,tx_size,tx_wire[0],tx_wire[1],data_events,control_events);
    assert(n<=125&&tx_size==6+n&&tx_wire[0]==0x8a&&tx_wire[1]==(0x80|n));
    for(size_t i=0;i<n;i++)
        assert((tx_wire[6+i]^tx_wire[2+i%4])==payload[i]);
    assert(control_events==1&&data_events==1&&app_sends==0);
}
static void app_rx_size(size_t n){
    esp_websocket_client_handle_t c=receive_context.ws;
    free(c->rx_buffer);c->rx_buffer=calloc(1,n);assert(c->rx_buffer);c->buffer_size=(int)n;
}
static void failed_control(bridge_transport_t *t){
    bridge_voice_t *c=&receive_context;
    assert(atomic_load(&t->cancelled)&&!tx_size&&!data_events&&!receive_queue.n);
    /* Same exact abort+callback+owner consequence as the complete SDK task,
     * without pretending this direct receive fixture is its scheduler. */
    assert(esp_websocket_client_abort_connection(c->ws,WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT)==ESP_OK);
    production_consume_one(c);
    assert(atomic_load(&c->failed)&&!c->model.confirmed&&c->model.send==BR_SEND_NONE);
    assert(!app_sends&&!(c->actions&BR_ACT_CONFIRM)&&!playback_buffer.n);
}
static void receive_finish(bridge_transport_t *t){
    bv_frame_t p;while(xQueueReceive((void*)&receive_queue,&p,0)==pdTRUE){setup_bridge_wipe(p.text,strlen(p.text));free(p.text);}
    assert(bridge_transport_dispose(t,esp_timer_get_time()+1000000));
    clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
}
static int run_receive(const char *which){
    if(!strcmp(which,"start-before-timer-delay")){
        /* Actual bridge admission and extracted pinned SDK start; only the
         * timer setup time is injected. No real task or connection may start. */
        fake_clock=true;fake_now=100000000;
        bridge_transport_t *t=make();assert(t);
        int64_t start_by=fake_now+100000;
        timer_start_delay_us=200000;fail_create=true; /* backstop if guard regresses */
        assert(bridge_transport_start_before(t,start_by)==ESP_ERR_TIMEOUT);
        assert(!timer_start_delay_us&&fake_now>start_by&&atomic_load(&t->started));
        assert(atomic_load(&t->cancelled)&&!t->task_created&&!worker_exists);
        assert(!atomic_load(&task_create_attempts)&&!atomic_load(&connects)&&!atomic_load(&payload_sends));
        bridge_transport_terminal_t result=BR_TRANSPORT_PROTOCOL;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&result));
        assert(result==BR_TRANSPORT_NONE);clean();
        if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        puts("PASS RECEIVE start-before-timer-delay: no SDK task admitted after cutoff.");return 0;
    }
    bridge_transport_t *t=receive_init();bridge_voice_t *c=&receive_context;
    if(!strcmp(which,"cert-event-order")||!strcmp(which,"cert-event-post-loss")){
        bool lost=!strcmp(which,"cert-event-post-loss");
        /* The actual SDK task does connect -> ERROR dispatch -> abort, which
         * closes and dispatches DISCONNECTED. Execute those exact bodies here,
         * not the task scheduler; the handshake itself is synthetic. */
        cert_event_probe=true;cert_flags_seen=MBEDTLS_X509_BADCERT_CN_MISMATCH;
        fixture_connect_error=(esp_tls_last_error_t){
            .last_error=ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED,
            .esp_tls_error_code=1,.esp_tls_flags=cert_flags_seen};
        connect_failure=true;c->ws->state=WEBSOCKET_STATE_INIT;
        assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)<0);
        assert(!bridge_transport_upgraded(t)&&bridge_transport_http_status(t)==0);
        assert(esp_websocket_client_dispatch_event(c->ws,WEBSOCKET_EVENT_ERROR,NULL,0)==ESP_OK);
        assert(cert_error_events==1);
        fail_next_disconnect_post=lost;
        assert(xSemaphoreTakeRecursive(c->ws->lock,0)==pdTRUE);
        assert(esp_websocket_client_abort_connection(c->ws,WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT)==ESP_OK);
        assert(xSemaphoreGiveRecursive(c->ws->lock)==pdTRUE);
        assert(cert_disconnect_events==(lost?0U:1U));
        if(lost)assert(failed_allocations==1&&!fail_next_disconnect_post);
        esp_tls_error_handle_t h=esp_transport_get_error_handle(t->ws);
        assert(h&&!h->last_error&&!h->esp_tls_error_code&&!h->esp_tls_flags);
        bridge_transport_terminal_t result=BR_TRANSPORT_NONE;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&result));
        assert(result==BR_TRANSPORT_CERT_VERIFY&&!app_sends&&!tx_size&&!atomic_load(&payload_sends));
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        puts("PASS certificate event ordering/get-clear: exact dispatch/abort, synthetic TLS/RTOS/events");return 0;
    }
    if(!strncmp(which,"cleanup-race-rx-",16)){
        /* These sanitized literal text payloads match 70df142's builders and
         * gateway shutdown branch; they are NOT a captured gateway execution.
         * Actual pinned WS header/payload parsing, recv/dispatch, transport
         * tickets, callback, cleanup tail and end classification execute here.
         *
         * The owner/audio scheduler is NOT present in this receive fixture.
         * failure("overflow"), began_at and a full mocked send establish its
         * synthetic cleanup preconditions only. The cleanup-race-* whole-owner
         * cases independently prove actual capture-buffer overflow/full stop.
         * No receive-only result is a one-call or gateway-cause proof.
         */
        static const char unconfirmed[]="{\"type\":\"pet.error\",\"code\":\"close_unconfirmed\"}";
        static const char closed[]="{\"type\":\"session.closed\"}";
        static const char stop[]="{\"type\":\"pet.stop\"}";
        static const char close[]="{\"type\":\"session.close\"}";
        static const unsigned char normal_close[]={0x03,0xe8};
        bool unconfirmed_case=!strcmp(which,"cleanup-race-rx-unconfirmed");
        bool unconfirmed_only=!strcmp(which,"cleanup-race-rx-unconfirmed-only");
        bool json_only=!strcmp(which,"cleanup-race-rx-json-closed");
        bool confirmed=!strcmp(which,"cleanup-race-rx-confirmed");
        bool normal=!strcmp(which,"cleanup-race-rx-normal");
        bool before=!strcmp(which,"cleanup-race-rx-normal-before");
        bool bare=!strcmp(which,"cleanup-race-rx-bare-loss");
        assert(unconfirmed_case||unconfirmed_only||json_only||confirmed||normal||before||bare);
        app_rx_size(8192);tcp_split=8192;
        fake_clock=true;fake_now=100000000;t->deadline=fake_now+60000000;
        c->hard=t->deadline;c->work=t->deadline-2000000;
        bridge_init(&c->model);bridge_command(&c->model,BR_CMD_CONVERSE,0);
        atomic_store(&c->converse,true);c->began_at=fake_now-1000000;
        /* Generated-before-stop is external fixture metadata, not firmware
         * input. There are NO peer bytes available to the parser at that time.
         * Exactly the same bytes could instead be generated by local cleanup. */
        const struct {bool generated_before_stop;int64_t generated_at;} peer={
            .generated_before_stop=!bare,.generated_at=fake_now
        };
        failure(c,"overflow");
        assert(c->model.phase==BR_CLOSING&&atomic_load(&c->exit_reason)==BV_EXIT_QUEUE);
        assert(!receive_queue.n&&!rx_size&&!app_sends);
        fake_now+=1000;
        if(!before){
            /* The real send/admission helper runs with the existing mocked
             * send_text boundary. This is NOT a provider/owner execution. */
            c->cleanup_stop_sent=send_frame_timed(c,stop,BV_SEND_MS,&c->cleanup_stop_started_at);
            assert(c->cleanup_stop_sent&&send_frame(c,close));
            assert(app_sends==2&&!strcmp(sent_app[0],stop)&&!strcmp(sent_app[1],close));
            assert(t->local_control_admitted);
            if(!bare)assert(peer.generated_before_stop&&peer.generated_at<c->cleanup_stop_started_at);
        }
        fake_now+=10000;
        const char *text=unconfirmed_case||unconfirmed_only?unconfirmed:json_only||confirmed?closed:NULL;
        if(text){
            add_text(text);
            assert(esp_websocket_client_recv(c->ws)==ESP_OK);
            assert(receive_queue.n==1&&!strcmp(receive_queue.items[0].text,text));
            assert(receive_queue.items[0].after_local_control);
            assert(receive_queue.items[0].received_at>c->cleanup_stop_started_at);
            empty_wire();
        }
        if(!json_only&&!bare&&!unconfirmed_only){
            fake_now+=1000;
            add_frame(8,true,normal_close,sizeof(normal_close));
            assert(esp_websocket_client_recv(c->ws)==ESP_OK);
            assert(control_events==1&&!atomic_load(&t->data_pending));
            assert(!t->ws_read_active&&!atomic_load(&t->data_lost));
            /* A parser ingress timestamp, even for COMPLETE WS1000, cannot
             * reveal the earlier peer-generation metadata recorded above. */
            assert(peer.generated_before_stop&&peer.generated_at<fake_now);
        }else if(bare){
            receive_eof();
            assert(!control_events&&!receive_queue.n&&!atomic_load(&t->data_lost));
        }
        bridge_transport_terminal_t result=BR_TRANSPORT_PROTOCOL;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&result));
        c->transport=NULL;c->ws=NULL;c->transport_terminal=result;
        converse_tail(c); /* exact final cleanup-tail function, after exit proof */
        assert(!receive_queue.n&&!c->send_pending&&!c->send_uncertain_seen);
        assert(!c->model.confirmed&&c->model.send==BR_SEND_NONE&&!playback_buffer.n);
        assert(!atomic_load(&connects)&&!atomic_load(&payload_sends));
        char line[112]={0};unsigned end=converse_end(c,line,sizeof(line));
        fprintf(stderr,"CLEANUP RECEIVE %s: terminal=%u end=%u (retry=%u), generated_before_stop=%d; generation is NOT firmware-observable\n",
                which,(unsigned)result,end,(unsigned)BV_END_RETRY,peer.generated_before_stop);
        if(bare)assert(end==BV_END_RETRY); /* no terminal evidence: recovery control */
        else assert(end!=BV_END_RETRY); /* red-first normal/JSON/unconfirmed regressions */
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        printf("PASS RECEIVE %s: exact parser/admission/cleanup-tail disposition, synthetic owner preconditions; NOT full-owner or causal proof.\n",which);
        return 0;
    }
    if(!strcmp(which,"missing-ticket")||!strcmp(which,"missing-ticket-inactive")){
        /* Inject a consumed ticket immediately before the real callback:
         * this tests an unknown delivery, not a claimed SDK failure. */
        app_rx_size(8192);
        if(!strcmp(which,"missing-ticket-inactive"))atomic_store(&c->accepting,false);
        consume_ticket_before_dispatch=true;add_text(g_closed);
        assert(esp_websocket_client_recv(c->ws)==ESP_OK);
        assert(!consume_ticket_before_dispatch&&consumed_ticket_faults==1&&data_events==1);
        assert(atomic_load(&c->ingress_incomplete)&&atomic_load(&c->failed));
        assert(atomic_load(&t->data_lost)&&!atomic_load(&t->data_pending));
        assert(!receive_queue.n&&!playback_buffer.n&&!app_sends);
        bridge_transport_terminal_t result=BR_TRANSPORT_NONE;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&result));
        assert(result==BR_TRANSPORT_EVENT_LOSS);
        c->transport=NULL;c->transport_terminal=result;c->model.converse=true;
        atomic_store(&c->exit_reason,BV_EXIT_DISCONNECTED);char line[112]={0};
        assert(converse_end(c,line,sizeof(line))==BV_END_ERROR&&!strcmp(line,"Voice connection interrupted."));
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        printf("PASS RECEIVE %s: injected missing ticket fails closed.\n",which);return 0;
    }
    if(!strcmp(which,"lost-post-middle")){
        app_rx_size(8192);fail_data_post_at=2;
        add_frame(1,false,"abc",3);add_frame(0,false,"def",3);add_frame(0,true,"ghi",3);
        assert(esp_websocket_client_recv(c->ws)==ESP_OK&&!receive_queue.n&&c->assembly.active);
        assert(esp_websocket_client_recv(c->ws)==ESP_OK&&failed_allocations==1&&!receive_queue.n);
        size_t before=rx_at;assert(esp_websocket_client_recv(c->ws)==ESP_FAIL&&rx_at==before);
        assert(esp_websocket_client_abort_connection(c->ws,WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT)==ESP_OK);
        bridge_transport_terminal_t outcome=BR_TRANSPORT_NONE;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&outcome)&&outcome==BR_TRANSPORT_EVENT_LOSS);
        c->transport=NULL;c->transport_terminal=outcome;c->model.converse=true;char line[112]={0};
        assert(converse_end(c,line,sizeof(line))==BV_END_ERROR&&!strcmp(line,"Voice connection interrupted."));
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        puts("PASS RECEIVE lost-post-middle: later fragment not admitted after missing callback.");return 0;
    }
    if(!strncmp(which,"lost-post-",10)){
        /* Exact pinned SDK recv ignores dispatch's failure. The dropped DATA
         * callback cannot tell the owner what was in the received frame. */
        const char *msg=!strcmp(which,"lost-post-error")?
            "{\"type\":\"pet.error\",\"code\":\"model_mismatch\"}":
            "{\"type\":\"pet.converse.notice\",\"code\":\"idle_timeout\"}";
        app_rx_size(8192);fail_next_data_post=true;
        add_text(msg);assert(esp_websocket_client_recv(c->ws)==ESP_OK);
        assert(failed_allocations==1&&!data_events&&!receive_queue.n&&!atomic_load(&c->failed));
        assert(c->ws->payload_offset==(int)strlen(msg)); /* SDK ignored post error */
        /* The next read detects the outstanding ticket and aborts without
         * consuming more input; the normal SDK error/disconnect follows. */
        receive_eof();
        bridge_transport_terminal_t outcome=BR_TRANSPORT_NONE;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&outcome));
        assert(outcome==BR_TRANSPORT_EVENT_LOSS); /* unknown ingress must veto retry */
        c->transport=NULL;c->transport_terminal=outcome;
        c->model.converse=true;char line[112]={0};
        assert(converse_end(c,line,sizeof(line))==BV_END_ERROR);
        assert(!strcmp(line,"Voice connection interrupted.")&&!app_sends);
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        printf("PASS RECEIVE %s: event post failed before callback, retry veto preserved.\n",which);return 0;
    }
    if(!strncmp(which,"header-loss-",12)||!strncmp(which,"text-first-",11)){
        /* The production observer surrounds the real pinned WS parser. A
         * prior complete zero control tests that no old length/opcode is used
         * as identity when the new header fails before it is published. */
        app_rx_size(8192);tcp_split=8192;
        bool prior=strstr(which,"-prior")!=NULL;
        if(prior){
            add_frame(10,true,NULL,0);
            assert(esp_websocket_client_recv(c->ws)==ESP_OK&&data_events==1&&!receive_queue.n);
            assert(!atomic_load(&t->data_pending)&&!atomic_load(&t->data_lost));
            empty_wire();data_events=control_events=zero_control_events=0;
        }
        if(!strncmp(which,"text-first-",11)){
            add_text(g_started);controlled_payload_begin=2;
            fault_payload_read=1;
            fault_payload_result=!strcmp(which,"text-first-zero")?0:-123;
        }else if(!strcmp(which,"header-loss-one")||!strcmp(which,"header-loss-cancel")){
            rx_wire[rx_size++]=0x81; /* first byte of a TEXT header only */
            if(!strcmp(which,"header-loss-cancel")){cancel_raw_once=true;cancel_raw_at=0;}
        }else if(!strcmp(which,"header-loss-extended-prior")||!strcmp(which,"header-loss-extended-zero-prior")){
            rx_wire[rx_size++]=0x88;rx_wire[rx_size++]=126;
            rx_wire[rx_size++]=0; /* incomplete extended CLOSE length */
            if(strstr(which,"-zero-")){rx_wire[rx_size++]=2;fault_raw_once=true;fault_raw_at=2;fault_raw_result=0;}
        }else{
            assert(!strcmp(which,"header-loss-masked-prior")||!strcmp(which,"header-loss-masked-zero-prior"));
            rx_wire[rx_size++]=0x88;rx_wire[rx_size++]=0x82;
            rx_wire[rx_size++]=0x12;rx_wire[rx_size++]=0x34; /* incomplete mask */
            if(strstr(which,"-zero-")){
                rx_wire[rx_size++]=0x56;rx_wire[rx_size++]=0x78;
                fault_raw_once=true;fault_raw_at=2;fault_raw_result=0;
            }
        }
        fixture_eof=true;
        assert(esp_websocket_client_recv(c->ws)==ESP_FAIL);
        size_t expected=!strncmp(which,"text-first-",11)?2:
            !strcmp(which,"header-loss-one")||!strcmp(which,"header-loss-cancel")?1:
            strstr(which,"-zero-")?2:strstr(which,"extended")?3:4;
        assert(rx_at==expected&&!t->ws_read_active&&atomic_load(&t->data_lost));
        if(!strncmp(which,"text-first-",11))assert(payload_reads==1);
        if(strstr(which,"-zero-"))assert(!fault_raw_once);
        assert(!data_events&&!receive_queue.n&&!app_sends);
        assert(atomic_load(&t->cancelled));
        assert(esp_websocket_client_abort_connection(c->ws,WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT)==ESP_OK);
        bridge_transport_terminal_t result=BR_TRANSPORT_NONE;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&result));
        assert(result==BR_TRANSPORT_EVENT_LOSS);
        c->transport=NULL;c->transport_terminal=result;c->model.converse=true;
        atomic_store(&c->exit_reason,BV_EXIT_DISCONNECTED);char line[112]={0};
        assert(converse_end(c,line,sizeof(line))==BV_END_ERROR&&!strcmp(line,"Voice connection interrupted."));
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        printf("PASS RECEIVE %s: undispatched parser ingress vetoes retry.\n",which);return 0;
    }
    if(!strcmp(which,"bare-zero")||!strcmp(which,"bare-cancel")||!strcmp(which,"bare-eof-after-data")){
        app_rx_size(8192);
        if(!strcmp(which,"bare-eof-after-data")){
            add_text(g_started);assert(esp_websocket_client_recv(c->ws)==ESP_OK);
            assert(data_events==1&&receive_queue.n==1&&!atomic_load(&t->data_pending));
            production_consume_one(c);empty_wire();
            receive_eof();
        }else{
            char buffer[32];
            if(!strcmp(which,"bare-cancel"))bridge_transport_cancel(t);
            assert(esp_transport_read(t->ws,buffer,sizeof(buffer),0)<=0);
        }
        assert(!atomic_load(&t->data_lost)&&!t->ws_read_active);
        bridge_transport_terminal_t result=BR_TRANSPORT_PROTOCOL;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&result));
        assert(result==BR_TRANSPORT_NONE);
        c->transport=NULL;c->transport_terminal=result;c->model.converse=true;
        atomic_store(&c->exit_reason,BV_EXIT_DISCONNECTED);char line[112]={0};
        assert(converse_end(c,line,sizeof(line))==BV_END_RETRY);
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        printf("PASS RECEIVE %s: zero-progress loss is retryable.\n",which);return 0;
    }
    if(!strcmp(which,"origin-fragment-before")||!strcmp(which,"origin-fragment-after")){
        /* Real pinned parser and callback: first TEXT fragment, interleaved
         * control, then final continuation. Admission occurs in the first
         * dispatch after its parser ticket, or before the first parser read. */
        app_rx_size(8192);bridge_init(&c->model);bridge_command(&c->model,BR_CMD_CONVERSE,0);
        atomic_store(&c->converse,true);
        bool before=!strcmp(which,"origin-fragment-before");
        size_t n=strlen(g_closed),split=n/2;assert(split>0);
        add_frame(1,false,g_closed,split);
        add_frame(9,true,"ping",4);
        add_frame(0,true,g_closed+split,n-split);
        if(before)admit_on_data_dispatch=true;
        else assert(bridge_transport_admit_control(t,esp_timer_get_time()+1000000));
        assert(esp_websocket_client_recv(c->ws)==ESP_OK);
        assert(!admit_on_data_dispatch&&c->assembly.active&&!receive_queue.n);
        assert(c->assembly_after_local_control!=before&&!atomic_load(&c->failed));
        read_batch(false);
        assert(!c->assembly.active&&receive_queue.n==1&&control_events&&continuation_events);
        assert(receive_queue.items[0].after_local_control!=before&&!atomic_load(&t->data_lost));
        production_consume_one(c);
        assert(c->model.wire_closed&&c->peer_closed_before_control==before);
        char line[112]={0};
        assert(converse_end(c,line,sizeof(line))==BV_END_OFF);
        assert(!strcmp(line,"Mic off (call ended)")); /* receipt order never grants retry */
        receive_finish(t);printf("PASS RECEIVE %s: fragmented parser provenance.\n",which);return 0;
    }
    if(!strcmp(which,"origin-before-dispatch")||!strcmp(which,"origin-after-admission")){
        /* Real pinned parser has already produced its DATA ticket when event
         * dispatch admits local cleanup. The opposite case admits it before
         * the parser reads any bytes. The owner consumes the same queued frame. */
        app_rx_size(8192);bridge_init(&c->model);bridge_command(&c->model,BR_CMD_CONVERSE,0);
        atomic_store(&c->converse,true);
        bool before=!strcmp(which,"origin-before-dispatch");
        if(before){
            fake_clock=true;fake_now=100000000;t->deadline=fake_now+60000000;
            c->hard=t->deadline;c->work=t->deadline-2000000;
            c->began_at=fake_now-29700000;c->failures=3;
            data_dispatch_delay_us=500000;admit_on_data_dispatch=true;
        }
        else assert(bridge_transport_admit_control(t,esp_timer_get_time()+1000000));
        add_text(g_closed);assert(esp_websocket_client_recv(c->ws)==ESP_OK);
        assert(!admit_on_data_dispatch&&receive_queue.n==1&&!atomic_load(&t->data_pending));
        if(before)assert(receive_queue.items[0].received_at==100000000&&fake_now==100500000);
        production_consume_one(c);
        if(before){unsigned failures=99;assert(retry_left(c,&failures)&&failures==3);}
        assert(c->model.wire_closed&&c->peer_closed_before_control==before);
        char line[112]={0};assert(converse_end(c,line,sizeof(line))==BV_END_OFF);
        assert(!strcmp(line,"Mic off (call ended)")); /* same veto after admission */
        receive_finish(t);printf("PASS RECEIVE %s: first parser ingress orders provenance.\n",which);return 0;
    }
    if(!strcmp(which,"eof-delayed-dispatch")){
        /* SDK recv observes bare EOF just before the healthy threshold, then
         * the event loop is delayed beyond it. The delayed callback must not
         * reset consecutive failures or extend the original loss window. */
        fake_clock=true;fake_now=100000000;t->deadline=fake_now+60000000;
        c->hard=t->deadline;c->work=t->deadline-2000000;c->began_at=fake_now-29700000;c->failures=3;
        bridge_init(&c->model);bridge_command(&c->model,BR_CMD_CONVERSE,0);atomic_store(&c->converse,true);
        nondata_dispatch_delay_us=500000;receive_eof();
        assert(!nondata_dispatch_delay_us&&fake_now==100500000);
        assert(bridge_transport_end_at(t)==100000000&&c->ended_at==100000000);
        unsigned failures=99;assert(retry_left(c,&failures)&&failures==3);
        assert(!atomic_load(&t->data_lost));
        receive_finish(t);puts("PASS RECEIVE eof-delayed-dispatch: failure time precedes delayed callback.");return 0;
    }
    if(!strncmp(which,"partial-",8)||!strcmp(which,"bare-eof")){
        /* Execute the pinned SDK receive and production transport wrapper.
         * No code is inferred from a partial CLOSE, even when both status
         * bytes arrived: its declared payload has not completed. */
        /* This branch classifies a conversation ending; set its mode before
         * the local failure, rather than converting a one-turn model after it. */
        c->model.converse=true;
        app_rx_size(8192);tcp_split=1;
        bool bare=!strcmp(which,"bare-eof");
        bool close_frame=!strncmp(which,"partial-close-",14);
        if(bare){
            receive_eof();production_consume_one(c);
        }else{
            unsigned opcode=close_frame?8:strstr(which,"partial-ping-")?9:10;
            unsigned code=strstr(which,"-1002")?1002:strstr(which,"-1011")?1011:1008;
            unsigned char body[]={(unsigned char)(code>>8),(unsigned char)code,'b','y','e'};
            add_frame(opcode,true,body,sizeof(body));controlled_payload_begin=2;
            bool first_fault=!strcmp(which,"partial-close-error-0")||!strcmp(which,"partial-close-zero-0");
            bool cancel=!strcmp(which,"partial-close-cancel");
            if(cancel)cancel_payload_read=2;
            if(!first_fault&&!cancel)
                rx_size=controlled_payload_begin+(!strcmp(which,"partial-close-eof-2")||strstr(which,"-1002")||
                    strstr(which,"-1008")||strstr(which,"-1011")?2:
                    !strcmp(which,"partial-close-eof-0")?0:1);
            fixture_eof=true;
            bool delayed=strstr(which,"-delayed")!=NULL;
            if(delayed){
                fake_clock=true;fake_now=100000000;t->deadline=fake_now+60000000;
                c->hard=t->deadline;c->work=t->deadline-2000000;c->began_at=fake_now-29700000;c->failures=3;
                nondata_dispatch_delay_us=500000;
            }
            if(first_fault){fault_payload_read=1;fault_payload_result=!strcmp(which,"partial-close-zero-0")?0:-123;}
            assert(esp_websocket_client_recv(c->ws)==ESP_FAIL);
            if(first_fault)assert(payload_reads==1);
            failed_control(t);
            if(delayed){
                assert(!nondata_dispatch_delay_us&&bridge_transport_end_at(t)==100000000&&c->ended_at==100000000);
                unsigned failures=99;assert(retry_left(c,&failures)&&failures==3);
            }
        }
        bridge_transport_terminal_t result=BR_TRANSPORT_PROTOCOL;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&result));
        assert(result==(close_frame?BR_TRANSPORT_EVENT_LOSS:BR_TRANSPORT_NONE));
        c->transport=NULL;c->transport_terminal=result;c->model.converse=true;
        atomic_store(&c->exit_reason,BV_EXIT_DISCONNECTED);
        char line[112]={0};
        assert(converse_end(c,line,sizeof(line))==(close_frame?BV_END_ERROR:BV_END_RETRY));
        if(close_frame)assert(!strcmp(line,"Voice connection interrupted."));
        assert(!app_sends);
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        printf("PASS RECEIVE %s: partial control or bare EOF classification at settled disposal.\n",which);return 0;
    }
    if(!strcmp(which,"bare-eof-after-close")||!strcmp(which,"bare-zero-after-close")){
        /* A previously completed 1011 may remain in SDK getters; it is not
         * evidence that a later EOF began another CLOSE. */
        app_rx_size(8192);tcp_split=1;
        static const unsigned char body[]={3,0xf3};
        add_frame(8,true,body,sizeof(body));fixture_eof=strcmp(which,"bare-zero-after-close")!=0;
        char bytes[32];
        assert(esp_transport_read(t->ws,bytes,sizeof(bytes),1000)==2);
        bridge_transport_data_delivered(t);
        assert(esp_transport_read(t->ws,bytes,sizeof(bytes),0)<=0);
        bridge_transport_terminal_t result=BR_TRANSPORT_PROTOCOL;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&result));
        assert(result==BR_TRANSPORT_NONE);
        c->transport=NULL;c->transport_terminal=result;c->model.converse=true;
        atomic_store(&c->exit_reason,BV_EXIT_DISCONNECTED);char line[112]={0};
        assert(converse_end(c,line,sizeof(line))==BV_END_RETRY);
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        printf("PASS RECEIVE %s: stale metadata does not veto retry.\n",which);return 0;
    }
    if(!strncmp(which,"close-",6)){
        /* Real SDK parser and recv, with one-byte TCP reads, preserve only
         * complete peer close codes. The final result survives disposal. */
        app_rx_size(8192);tcp_split=1;
        unsigned code=strstr(which,"close-normal")?1000:strstr(which,"close-unsupported")?1003:
                      strstr(which,"close-bad-payload")?1007:strstr(which,"close-too-large")?1009:
                      strstr(which,"close-wire-0000")?0:strstr(which,"close-wire-0999")?999:
                      strstr(which,"close-wire-1015")?1015:strstr(which,"close-wire-3000")?3000:
                      strstr(which,"close-wire-4999")?4999:strstr(which,"close-wire-5000")?5000:
                      strstr(which,"close-wire-65535")?65535:
                      strstr(which,"close-reserved-1004")?1004:strstr(which,"close-reserved-1005")?1005:
                      strstr(which,"close-reserved-1006")?1006:strstr(which,"close-client-only")?1010:
                      strstr(which,"close-protocol")?1002:
                      strstr(which,"close-policy")?1008:
                      strstr(which,"close-ambiguous")?1011:0;
        unsigned char body[5]={(unsigned char)(code>>8),(unsigned char)code,'b','y','e'};
        bool single=!strcmp(which,"close-single");
        bool cancel_final=strstr(which,"-cancel-final")!=NULL;
        bool status_present=code!=0||strstr(which,"close-wire-0000")!=NULL;
        size_t size=single?1:status_present?(strstr(which,"-reason")?5:2):0;
        add_frame(8,true,body,size);
        bool local=strstr(which,"-local")!=NULL;
        if(local)assert(bridge_transport_admit_control(t,esp_timer_get_time()+1000000));
        if(strstr(which,"-dispatch"))admit_on_data_dispatch=true;
        /* Injected prior terminal state checks precedence, not a claim that
         * a real peer normally sends multiple CLOSE frames on one connection. */
        bridge_transport_terminal_t seed=strstr(which,"-seed-protocol")?BR_TRANSPORT_PROTOCOL:
            strstr(which,"-seed-policy")?BR_TRANSPORT_POLICY:
            strstr(which,"-seed-payload")?BR_TRANSPORT_PAYLOAD_REJECTED:BR_TRANSPORT_NONE;
        atomic_store(&t->terminal,seed);
        if(cancel_final){controlled_payload_begin=2;cancel_payload_read=2;}
        assert(esp_websocket_client_recv(c->ws)==((single||cancel_final)?ESP_FAIL:ESP_OK));
        bridge_transport_terminal_t expected=single||(status_present&&(code<1000||code>=5000||code==1015))||
            code==1002||code==1004||code==1005||code==1006||code==1010?BR_TRANSPORT_PROTOCOL:
            seed!=BR_TRANSPORT_NONE?seed:
            code==1007||code==1009?BR_TRANSPORT_PAYLOAD_REJECTED:
            code==1003?BR_TRANSPORT_UNSUPPORTED:code==1000?BR_TRANSPORT_PEER_NORMAL:
            code==1008?BR_TRANSPORT_POLICY:BR_TRANSPORT_NONE;
        assert(atomic_load(&t->terminal)==(int)expected);
        bridge_transport_terminal_t result=BR_TRANSPORT_POLICY;
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+1000000,&result));
        assert(result==expected);
        c->transport=NULL;c->transport_terminal=result;c->model.converse=true;
        atomic_store(&c->exit_reason,BV_EXIT_DISCONNECTED);
        char line[112]={0};
        assert(converse_end(c,line,sizeof(line))==(result==BR_TRANSPORT_NONE?BV_END_RETRY:
            result==BR_TRANSPORT_PEER_NORMAL?BV_END_OFF:BV_END_ERROR));
        clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
        printf("PASS RECEIVE %s: complete close and final-access result.\n",which);return 0;
    }
    if(!strncmp(which,"ctrl-",5)){
        /* Real WS header/getters/recv/dispatch and actual wrapper throughout.
         * Only raw TCP/TLS read result/time/cancel are injected below it. */
        app_rx_size(8192);tcp_split=1;
        unsigned char payload[126];for(size_t i=0;i<sizeof(payload);i++)payload[i]=(unsigned char)(i*37+11);
        size_t n=4;unsigned opcode=9;bool fin=true;
        if(!strcmp(which,"ctrl-len1"))n=1;
        else if(!strcmp(which,"ctrl-len125"))n=125;
        else if(!strcmp(which,"ctrl-len0"))n=0;
        else if(!strcmp(which,"ctrl-bad-fin"))fin=false;
        else if(!strcmp(which,"ctrl-bad-opcode"))opcode=11;
        else if(!strcmp(which,"ctrl-oversize"))n=126;
        else if(!strcmp(which,"ctrl-small-buffer"))app_rx_size(2);
        bool streaming=!strcmp(which,"ctrl-text-streaming");
        if(streaming)opcode=1;
        add_frame(opcode,fin,payload,n);controlled_payload_begin=n<126?2:4;
        bool eof=!strcmp(which,"ctrl-partial-eof");
        if(eof){rx_size=controlled_payload_begin+1;fixture_eof=true;}
        if(!strcmp(which,"ctrl-zero-after-byte")||!strcmp(which,"ctrl-error-after-byte")){
            fault_payload_read=2;
            fault_payload_result=!strcmp(which,"ctrl-zero-after-byte")?0:-123;
        }
        if(!strcmp(which,"ctrl-cancel-accumulating"))cancel_payload_read=2;
        bool caller_budget=!strcmp(which,"ctrl-caller-budget");
        bool hard_budget=!strcmp(which,"ctrl-hard-budget");
        if(caller_budget||hard_budget){
            fake_clock=true;fake_now=1000000;payload_latency_us=3000;
            if(hard_budget){t->deadline=fake_now+10000;c->ws->config->network_timeout_ms=100;}
            else{t->deadline=fake_now+60000000;c->ws->config->network_timeout_ms=10;}
        }
        bool zero_probe=!strcmp(which,"ctrl-zero-probe");
        if(zero_probe)c->ws->config->network_timeout_ms=0;
        if(streaming){
            char first[8192]={0};
            assert(esp_transport_read(t->ws,first,sizeof(first),1000)==1);
            assert((unsigned char)first[0]==payload[0]&&payload_reads==1);
            assert(rx_at==controlled_payload_begin+1&&rx_at<rx_size&&!atomic_load(&t->cancelled));
            assert(!app_sends&&!tx_size); /* first bytes returned, NOT filled */
        }else{
            int result=esp_websocket_client_recv(c->ws);
            bool success=!strcmp(which,"ctrl-len1")||!strcmp(which,"ctrl-len125")||!strcmp(which,"ctrl-len0");
            if(success){
                assert(result==ESP_OK&&payload_reads==n&&!atomic_load(&t->cancelled));
                assert(atomic_load(&t->terminal)==BR_TRANSPORT_NONE&&!atomic_load(&t->data_lost)&&!t->ws_read_active);
                assert(rx_at==rx_size&&receive_queue.n==0);check_one_pong(payload,n);
            }else{
                fprintf(stderr,"CONTROL %s result=%d payload_reads=%u fake_us=%lld raw_consumed=%zu raw_remaining=%zu cancelled=%d\n",
                        which,result,payload_reads,(long long)(fake_clock?fake_now-1000000:0),
                        rx_at,rx_size-rx_at,atomic_load(&t->cancelled));
                assert(result==ESP_FAIL);failed_control(t);
                bool malformed=!strcmp(which,"ctrl-bad-fin")||!strcmp(which,"ctrl-bad-opcode")||!strcmp(which,"ctrl-oversize");
                assert(atomic_load(&t->terminal)==(malformed?BR_TRANSPORT_PROTOCOL:BR_TRANSPORT_NONE));
                if(fault_payload_read||eof||cancel_payload_read)assert(payload_reads==2);
                if(zero_probe)assert(payload_reads==1&&rx_at==controlled_payload_begin+1);
                if(caller_budget||hard_budget){
                    assert(fake_now==1010000&&payload_reads==4);
                    const int expected[]={10,7,4,1};
                    assert(!memcmp(payload_waits,expected,sizeof(expected)));
                    /* Caller's absolute budget was not renewed per fragment. */
                    assert(rx_at==controlled_payload_begin+3);
                }
                unsigned before=payload_reads;
                char late[8192];assert(esp_transport_read(t->ws,late,sizeof(late),1000)<0);
                assert(payload_reads==before&&!app_sends&&!tx_size); /* no retry/send */
            }
        }
        assert(!atomic_load(&connects)&&!atomic_load(&payload_sends));
        receive_finish(t);printf("PASS RECEIVE %s: exact SDK+app control wrapper; synthetic TCP/time boundary only.\n",which);return 0;
    }
    if(!strncmp(which,"rx-converse",11)){
        /* Conversation mode through the exact SDK receive path. Frames are the
         * design's golden bytes (conversation-mode.md), delivered byte-by-byte
         * and as RFC6455 continuations with interleaved pings in the -split case. */
        static const char began[]="{\"type\":\"pet.converse.began\",\"safe_session_label\":\"Example conversation\",\"session_id_short\":\"a1b2c3d4\"}";
        bool split=!strcmp(which,"rx-converse-split");
        if(split){app_rx_size(8192);tcp_split=1;}
        bridge_init(&c->model);bridge_command(&c->model,BR_CMD_CONVERSE,0);atomic_store(&c->converse,true);
        add_message(g_started,split);add_message(began,split);
        read_batch(false);consume_batch();empty_wire();
        assert(!atomic_load(&c->failed)&&c->model.phase==BR_CONVERSING&&c->actions==(BR_ACT_BEGIN|BR_ACT_CAPTURE));
        assert(!strcmp(c->model.label,"Example conversation")&&!strcmp(c->model.short_id,"a1b2c3d4"));
        assert(send_action(c,BR_ACT_BEGIN)&&app_sends==1&&!strcmp(sent_app[0],"{\"type\":\"pet.converse.begin\"}"));
        atomic_store(&c->play_gate,true); /* the owner opens it with the codec; not under test here */
        static const struct {const char *frame;bridge_talk_t talk;bridge_notice_t notice;} frames[]={
            {"{\"type\":\"pet.converse.state\",\"state\":\"listening\"}",BR_TALK_LISTENING,BR_NOTICE_NONE},
            {"{\"type\":\"pet.converse.state\",\"state\":\"asking\"}",BR_TALK_ASKING,BR_NOTICE_NONE},
            {"{\"type\":\"pet.converse.state\",\"state\":\"answering\"}",BR_TALK_ANSWERING,BR_NOTICE_NONE},
            {"{\"type\":\"pet.converse.notice\",\"code\":\"busy\"}",BR_TALK_ANSWERING,BR_NOTICE_BUSY},
            {"{\"type\":\"pet.converse.notice\",\"code\":\"not_sent\"}",BR_TALK_ANSWERING,BR_NOTICE_NOT_SENT},
            {"{\"type\":\"pet.converse.notice\",\"code\":\"send_uncertain\"}",BR_TALK_ANSWERING,BR_NOTICE_SEND_UNCERTAIN},
            {"{\"type\":\"pet.converse.notice\",\"code\":\"reply_unavailable\"}",BR_TALK_ANSWERING,BR_NOTICE_REPLY_UNAVAILABLE},
            {"{\"type\":\"pet.converse.notice\",\"code\":\"idle_timeout\"}",BR_TALK_ANSWERING,BR_NOTICE_IDLE_TIMEOUT},
            {"{\"type\":\"pet.converse.notice\",\"code\":\"time_limit\"}",BR_TALK_ANSWERING,BR_NOTICE_TIME_LIMIT}};
        for(size_t i=0;i<sizeof(frames)/sizeof(*frames);i++){
            add_message(frames[i].frame,split);add_message(g_audio,split); /* audio may arrive at any time */
            read_batch(false);consume_batch();empty_wire();
            assert(!atomic_load(&c->failed)&&c->model.phase==BR_CONVERSING&&c->model.talk==frames[i].talk);
            assert(frames[i].notice==BR_NOTICE_NONE||(c->model.notice==frames[i].notice&&c->model.notice_visible));
            assert(playback_buffer.n==(i+1)*sizeof(g_pcm)&&c->received_pcm==(i+1)*sizeof(g_pcm));
            assert(!memcmp(playback_buffer.bytes+i*sizeof(g_pcm),g_pcm,sizeof(g_pcm)));
        }
        /* Unknown values: ignored, still open. */
        add_message("{\"type\":\"pet.converse.state\",\"state\":\"dreaming\"}",split);
        add_message("{\"type\":\"pet.converse.notice\",\"code\":\"brand_new\"}",split);
        read_batch(false);consume_batch();empty_wire();
        assert(!atomic_load(&c->failed)&&c->model.phase==BR_CONVERSING&&c->model.ignored==2);
        /* The host's close ends it normally, with its stated reason kept. */
        add_message(g_closed,split);read_batch(false);consume_batch();empty_wire();
        assert(!atomic_load(&c->failed)&&c->model.phase==BR_FINISHED&&c->model.wire_closed);
        assert(c->model.end_notice==BR_NOTICE_TIME_LIMIT&&atomic_load(&c->exit_reason)==BV_EXIT_HOST);
        assert(!atomic_load(&c->play_gate)&&!atomic_load(&c->capture_gate));
        assert(app_sends==1&&!c->model.confirmed&&c->model.send==BR_SEND_NONE); /* nothing but the begin was sent */
        if(split)assert(continuation_events&&split_events&&control_events);
        receive_finish(t);
        printf("PASS RECEIVE %s: exact SDK WS/recv/dispatch -> literal callback/assembly/consumer/parser/model in conversation mode; synthetic TLS/RTOS/audio/send boundaries, NOT physical proof.\n",which);
        return 0;
    }
    if(!strcmp(which,"rx-tcp-byte-splits"))tcp_split=1;
    if(!strcmp(which,"rx-tcp-control-splits")){
        /* Keep the REAL app's 8192-byte RX size: this is short TCP payload
         * delivery, not an artificially tiny control buffer. The 4-byte ping
         * fits comfortably; every admitted byte arrives exactly once. */
        app_rx_size(8192);tcp_split=1;
        setup_preview(true);
        assert(!atomic_load(&c->failed)&&c->model.phase==BR_PREVIEW&&app_sends==3);
        /* The actual wrapper makes each complete ping atomic to SDK recv:
         * three PING callbacks plus three zero-byte PONG callbacks. */
        assert(continuation_events&&control_events==6&&zero_control_events==3&&split_events);
        check_control_replies(3); /* RFC6455 PONG must echo the whole ping */
        receive_finish(t);
        puts("PASS RECEIVE rx-tcp-control-splits: exact SDK receive/control echo with app-sized RX buffer.");
        return 0;
    }
    if(!strcmp(which,"rx-normal")||!strcmp(which,"rx-tcp-byte-splits")||
       !strcmp(which,"rx-continuations-controls")){
        bool fragments=!strcmp(which,"rx-continuations-controls");
        setup_preview(fragments);
        assert(split_events&&data_events>3);
        if(fragments){assert(continuation_events&&control_events==6);check_control_replies(3);}
    }else if(!strcmp(which,"rx-reply-coalesced-eof")){
        setup_preview(false);confirm_fixture_once();
        /* Four complete WS messages in ONE raw byte batch, all queued BEFORE
         * actual SDK abort dispatches EOF. Literal owner must drain in order. */
        add_text(g_reply_started);add_text(g_audio);add_text(g_done);add_text(g_closed);
        read_batch(false);assert(receive_queue.n==4);receive_eof();
        assert(!transport_lost(c));consume_batch();
        assert(!atomic_load(&c->failed)&&!receive_queue.n&&c->model.wire_closed&&c->model.draining);
        assert(c->model.reply==BR_REPLY_RECEIVED&&c->received_pcm==sizeof(g_pcm));
        assert(atomic_load(&c->play_gate)&&atomic_load(&c->drain));
        assert(playback_buffer.n==sizeof(g_pcm)&&!memcmp(playback_buffer.bytes,g_pcm,sizeof(g_pcm)));
        assert(app_sends==4); /* canonical close/EOF never confirms/replays */
    }else if(!strcmp(which,"rx-unsolicited-audio")){
        setup_preview(false);
        add_text(g_audio);read_batch(false);consume_batch();empty_wire();
        assert(!atomic_load(&c->failed)&&!c->received_pcm&&!playback_buffer.n&&!atomic_load(&c->play_gate));
        assert(c->model.phase==BR_PREVIEW&&app_sends==3&&!c->model.confirmed);
    }else if(!strcmp(which,"rx-zero-controls")){
        add_frame(9,true,NULL,0);add_frame(10,true,NULL,0);add_text(g_started);
        read_batch(false);consume_batch();
        assert(!atomic_load(&c->failed)&&c->model.phase==BR_BEGINNING&&zero_control_events==2);
        assert(!c->assembly.active&&app_sends==0);check_control_replies(1);
    }else if(!strcmp(which,"rx-empty-final-continuation")){
        add_frame(1,false,g_started,strlen(g_started));add_frame(0,true,NULL,0);
        read_batch(false);
        fprintf(stderr,"RX empty-final-continuation: SDK_DATA=%u continuation_DATA=%u queued=%u assembly_active=%d used=%zu remaining_raw=%zu\n",
                data_events,continuation_events,receive_queue.n,c->assembly.active,c->assembly.used,rx_size-rx_at);
        /* Valid RFC6455 terminal continuation MUST publish this message. */
        assert(receive_queue.n==1&&!c->assembly.active&&!atomic_load(&c->failed));
        assert(!atomic_load(&t->data_lost)&&!atomic_load(&t->data_pending)&&!t->ws_read_active);
        consume_batch();assert(c->model.phase==BR_BEGINNING);
    }else{
        if(!strcmp(which,"rx-binary"))add_frame(2,true,g_started,strlen(g_started));
        else if(!strcmp(which,"rx-nested-text")){
            add_frame(1,false,g_started,5);add_text(g_started);
        }else if(!strcmp(which,"rx-json-coalesced")){
            char joined[128];snprintf(joined,sizeof(joined),"%s%s",g_started,g_closed);add_text(joined);
        }else if(!strcmp(which,"rx-nul")){
            unsigned char bytes[128];size_t n=strlen(g_started);
            memcpy(bytes,g_started,n);bytes[n++]=0;bytes[n++]='x';add_frame(1,true,bytes,n);
        }else if(!strcmp(which,"rx-queue-overflow")){
            for(unsigned i=0;i<5;i++)add_text(g_started);
        }else{
            assert(!strcmp(which,"rx-truncated-eof"));add_text(g_started);rx_size-=3;fixture_eof=true;
        }
        bool truncated=!strcmp(which,"rx-truncated-eof");
        read_batch(truncated);consume_batch();
        if(truncated){
            /* Exact SDK recv error, then same abort path used by actual task. */
            assert(!receive_queue.n&&c->assembly.active);
            assert(esp_websocket_client_abort_connection(c->ws,WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT)==ESP_OK);
            production_consume_one(c);
        }
        assert(atomic_load(&c->failed)&&!c->model.confirmed&&app_sends==0);
        assert(!(c->actions&BR_ACT_CONFIRM)&&c->model.send==BR_SEND_NONE);
    }
    assert(!atomic_load(&connects)&&!atomic_load(&payload_sends));receive_finish(t);
    printf("PASS RECEIVE %s: exact SDK WS/recv/dispatch -> literal callback/assembly/consumer/parser/model; synthetic TLS/RTOS/audio/send boundaries, NOT physical proof.\n",which);
    return 0;
}
#endif

static bridge_transport_t *start_target;
static esp_err_t start_result;
static atomic_bool fixture_stop;
static atomic_uint fixture_stop_checks;
static bool fixture_stop_check(void *context){
    assert(context==&fixture_stop);atomic_fetch_add(&fixture_stop_checks,1);
    return atomic_load(&fixture_stop);
}
static bool slow_stop_check(void *context){
    (void)context;struct timespec pause={.tv_nsec=20000000};assert(!nanosleep(&pause,NULL));return false;
}
static unsigned delayed_stop_calls;
static bool first_slow_stop_check(void *context){
    (void)context;
    if(!delayed_stop_calls++){struct timespec pause={.tv_nsec=20000000};assert(!nanosleep(&pause,NULL));}
    return false;
}
static unsigned delayed_inner_check;
static atomic_bool delayed_inner_reached;
static bool budget_inner_stop_check(void *context){
    assert(context==&fixture_stop);
    unsigned call=atomic_fetch_add(&fixture_stop_checks,1)+1;
    if(call==delayed_inner_check){
        atomic_store(&delayed_inner_reached,true);atomic_fetch_add(&forced_clock,20000);
    }
    return false;
}
static void *start_entry(void *unused){
    (void)unused;start_result=bridge_transport_start(start_target);return NULL;
}
static bridge_transport_t *connect_target;
static int connect_result;
static void *connect_entry(void *unused){
    (void)unused;connect_result=esp_transport_connect(connect_target->ws,"192.168.1.50",9443,10000);
    return NULL;
}

int main(int argc,char **argv){
    assert(argc==2);const char *which=argv[1];
#ifdef BRIDGE_RECEIVE_TEST
    return run_receive(which);
#endif
    if(!strcmp(which,"hard-deadline-timer-start")){
        /* No Stop/retry cutoff: only the absolute hard deadline expires while
         * the actual timer-start seam is held. Zero means no start_by. */
        atomic_store(&forced_clock,100000000);arm(G_TIMER_START);
        start_target=bridge_transport_new(&cfg,synthetic_token,100100000);assert(start_target);
        pthread_t starter;assert(!pthread_create(&starter,NULL,start_entry,NULL));await(G_TIMER_START);
        assert(start_target->cutoff->active&&!atomic_load(&fixture_stop));
        atomic_store(&forced_clock,100100000);release(G_TIMER_START);
        assert(!pthread_join(starter,NULL));
        fprintf(stderr,"hard-timer: result=%d task_create=%u raw_connect=%u\n",
                start_result,atomic_load(&task_create_attempts),atomic_load(&connects));
        assert(start_result==ESP_ERR_TIMEOUT&&atomic_load(&start_target->cancelled));
        assert(!atomic_load(&task_create_attempts)&&!atomic_load(&connects));
        finish_and_dispose(start_target);no_automatic_work(0);
    }else if(!strcmp(which,"hard-deadline-timer-control")){
        atomic_store(&forced_clock,100000000);
        bridge_transport_t *t=bridge_transport_new(&cfg,synthetic_token,160000000);assert(t);
        assert(bridge_transport_start_before(t,0)==ESP_OK);
        assert(atomic_load(&task_create_attempts)==1);
        finish_and_dispose(t);no_automatic_work(1);
    }else if(!strcmp(which,"slow-inner-before-io-budget")||
             !strcmp(which,"slow-inner-owned-budget")){
        bool owned=!strcmp(which,"slow-inner-owned-budget");
        atomic_store(&forced_clock,100000000);delayed_inner_check=owned?3:2;
        bridge_transport_t *t=make();assert(t);
        assert(bridge_transport_set_stop_check(t,budget_inner_stop_check,&fixture_stop));
        int result=esp_transport_connect(t->ws,"192.168.1.50",9443,5);
        fprintf(stderr,"slow-inner-%s: checks=%u result=%d raw_connect=%u\n",
                owned?"owned":"before",atomic_load(&fixture_stop_checks),result,atomic_load(&connects));
        /* The owned case makes one final stop check after releasing the no-I/O
         * path; count may therefore exceed the index of the injected delay. */
        assert(result<0&&atomic_load(&delayed_inner_reached)&&
               atomic_load(&fixture_stop_checks)>=delayed_inner_check);
        assert(!atomic_load(&connects)&&!atomic_load(&payload_sends));
        finish_and_dispose(t);no_automatic_work(0);
    }else if(!strcmp(which,"stop-inner-io-admission")){
        connect_target=make();assert(connect_target);
        assert(bridge_transport_set_stop_check(connect_target,fixture_stop_check,&fixture_stop));
        assert(xSemaphoreTakeRecursive(connect_target->io,1)==pdTRUE);
        atomic_store(&observe_io_contention,true);
        pthread_t connector;assert(!pthread_create(&connector,NULL,connect_entry,NULL));
        int64_t until=esp_timer_get_time()+5000000;
        while(!atomic_load(&io_contended)){assert(esp_timer_get_time()<until);sched_yield();}
        /* Both outer WS and inner pre-lock checks have passed, and another
         * thread demonstrably owns the I/O lock. */
        assert(atomic_load(&fixture_stop_checks)>=2&&!atomic_load(&connects));
        atomic_store(&fixture_stop,true);atomic_store(&observe_io_contention,false);
        assert(xSemaphoreGiveRecursive(connect_target->io)==pdTRUE);
        assert(!pthread_join(connector,NULL));
        fprintf(stderr,"inner-stop: checks=%u result=%d raw_connect=%u sends=%u\n",
                atomic_load(&fixture_stop_checks),connect_result,atomic_load(&connects),atomic_load(&payload_sends));
        assert(connect_result<0&&atomic_load(&fixture_stop_checks)>=3);
        assert(!atomic_load(&connects)&&!atomic_load(&payload_sends));
        finish_and_dispose(connect_target);no_automatic_work(0);
    }else if(!strcmp(which,"inner-io-admission-control")){
        bridge_transport_t *t=make();assert(t);
        assert(bridge_transport_set_stop_check(t,fixture_stop_check,&fixture_stop));
        assert(esp_transport_connect(t->ws,"192.168.1.50",9443,10000)==0);
        assert(atomic_load(&fixture_stop_checks)>=3&&atomic_load(&connects)==1);
        finish_and_dispose(t);no_automatic_work(1);
    }else if(!strcmp(which,"cancel-during-timer-start")||!strcmp(which,"stop-during-timer-start")){
        bool use_callback=!strcmp(which,"stop-during-timer-start");
        arm(G_TIMER_START);start_target=make();assert(start_target);
        if(use_callback)assert(bridge_transport_set_stop_check(start_target,fixture_stop_check,&fixture_stop));
        pthread_t starter;assert(!pthread_create(&starter,NULL,start_entry,NULL));
        await(G_TIMER_START);
        if(use_callback)atomic_store(&fixture_stop,true);else bridge_transport_cancel(start_target);
        release(G_TIMER_START);
        assert(!pthread_join(starter,NULL));
        fprintf(stderr,"timer-stop: result=%d SDK_task_create=%u connect=%u\n",
                start_result,atomic_load(&task_create_attempts),atomic_load(&connects));
        assert(start_result!=ESP_OK&&!atomic_load(&task_create_attempts)&&!atomic_load(&connects));
        finish_and_dispose(start_target);no_automatic_work(0);
    }else if(!strcmp(which,"stop-check-retry-deadline")){
        bridge_transport_t *t=make();assert(t&&bridge_transport_set_stop_check(t,slow_stop_check,NULL));
        int result=bridge_transport_start_before(t,esp_timer_get_time()+25000);
        assert(result==ESP_ERR_TIMEOUT&&!atomic_load(&task_create_attempts)&&!atomic_load(&connects));
        finish_and_dispose(t);no_automatic_work(0);
    }else if(!strcmp(which,"stop-check-connect-budget")){
        bridge_transport_t *t=make();assert(t&&bridge_transport_set_stop_check(t,slow_stop_check,NULL));
        assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5)<0);
        assert(!atomic_load(&connects));finish_and_dispose(t);no_automatic_work(0);
    }else if(!strcmp(which,"stop-check-first-connect-budget")){
        bridge_transport_t *t=make();assert(t&&bridge_transport_set_stop_check(t,first_slow_stop_check,NULL));
        assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5)<0);
        fprintf(stderr,"first-check-budget: callbacks=%u connects=%u\n",delayed_stop_calls,atomic_load(&connects));
        assert(!atomic_load(&connects));finish_and_dispose(t);no_automatic_work(0);
    }else if(!strcmp(which,"stop-before-connect")){
        early_exit=false;arm(G_BEGIN);
        bridge_transport_t *t=make();assert(t&&bridge_transport_set_stop_check(t,fixture_stop_check,&fixture_stop));
        assert(bridge_transport_start(t)==ESP_OK);await(G_BEGIN);
        atomic_store(&fixture_stop,true);release(G_BEGIN);wait_finishing(t);
        assert(atomic_load(&task_create_attempts)==1&&!atomic_load(&connects));
        finish_and_dispose(t);no_automatic_work(0);
    }else if(!strcmp(which,"key-create-fail")){
        fail_key=true;assert(!make()&&!atomic_load(&chain)&&!app_slot);no_automatic_work(0);
    }else if(!strcmp(which,"timer-start-fail")||!strcmp(which,"task-create-fail")){
        fail_timer_start=!strcmp(which,"timer-start-fail");fail_create=!fail_timer_start;
        bridge_transport_t *t=make();assert(t);assert(bridge_transport_start(t)!=ESP_OK&&!t->task_created);
        assert(bridge_transport_started(t));no_automatic_work(0);finish_and_dispose(t);
    }else if(!strcmp(which,"setspecific-fail")||!strcmp(which,"self-destroy-fail")){
        fail_setspecific=!strcmp(which,"setspecific-fail");fail_self_destroy=!fail_setspecific;
        bridge_transport_t *t=make();assert(t&&bridge_transport_start(t)==ESP_OK);
        assert(!bridge_transport_dispose(t,esp_timer_get_time()+20000));join_worker();
        assert(atomic_load(&t->hook_failed));assert_retained(t);
        assert(atomic_load(&t->task_exited)==fail_self_destroy);
        assert(atomic_load(&clients_live)==(fail_self_destroy?1U:0U));
        no_automatic_work(fail_setspecific?0:1);
    }else if(!strcmp(which,"abandoned-ingress")||!strcmp(which,"abandoned-close")||!strcmp(which,"abandoned-empty")){
        /* Fixture-injected final parser record: prove no decision/free before
         * the actual SDK task destructor, then classify only positive ingress.
         * This does not pretend the SDK task actually exited in its parser. */
        arm(G_DESTRUCTOR);
        bridge_transport_t *t=make();assert(t&&bridge_transport_start(t)==ESP_OK);wait_finishing(t);
        portENTER_CRITICAL(&cutoff_mux);
        t->ws_read_active=true;t->ws_read_task=xTaskGetCurrentTaskHandle();
        t->ws_read_ingress=!strcmp(which,"abandoned-ingress");
        t->ws_close_incomplete=!strcmp(which,"abandoned-close");
        portEXIT_CRITICAL(&cutoff_mux);
        bridge_transport_terminal_t outcome=BR_TRANSPORT_POLICY;
        assert(!bridge_transport_dispose_result(t,esp_timer_get_time()+20000,&outcome));
        assert(outcome==BR_TRANSPORT_POLICY);await(G_DESTRUCTOR);assert_retained(t);
        release(G_DESTRUCTOR);join_worker();
        assert(bridge_transport_dispose_result(t,esp_timer_get_time()+3000000,&outcome));
        assert(outcome==(!strcmp(which,"abandoned-empty")?BR_TRANSPORT_NONE:BR_TRANSPORT_EVENT_LOSS));
        clean();no_automatic_work(1);
    }else if(!strcmp(which,"finish-stopped-destructor")){
        arm(G_STOPPED);arm(G_DESTRUCTOR);
        bridge_transport_t *t=make();assert(t&&bridge_transport_start(t)==ESP_OK);wait_finishing(t);
        assert(!atomic_load(&t->task_exited)&&!atomic_load(&client_frees));
        assert(!bridge_transport_dispose(t,esp_timer_get_time()+20000));await(G_STOPPED);
        assert(!atomic_load(&client_frees)&&!atomic_load(&t->task_exited));assert_retained(t);
        release(G_STOPPED);await(G_DESTRUCTOR);
        assert(atomic_load(&client_frees)==1&&!atomic_load(&t->task_exited));
        assert(!bridge_transport_dispose(t,esp_timer_get_time()+20000));assert_retained(t);
        release(G_DESTRUCTOR);finish_and_dispose(t);no_automatic_work(1);
    }else if(!strcmp(which,"dispose-timeout")){
        early_exit=false;arm(G_BEGIN);
        bridge_transport_t *t=make();assert(t&&bridge_transport_start(t)==ESP_OK);await(G_BEGIN);
        int64_t until=esp_timer_get_time()+20000,entry=esp_timer_get_time();
        assert(!bridge_transport_dispose(t,until));
        assert(esp_timer_get_time()>=until&&esp_timer_get_time()-entry<500000);
        assert_retained(t);no_automatic_work(0);
        release(G_BEGIN);finish_and_dispose(t);no_automatic_work(0);
    }else if(!strcmp(which,"delayed-cutoff-reuse")){
        bridge_transport_t *t=make();assert(t);delayed=t->cutoff->args;uintptr_t first=t->generation;
        assert(bridge_transport_start(t)==ESP_OK);finish_and_dispose(t);
        delayed.callback(delayed.arg); /* app allocation is ASan-poisoned */
        reset_observers();bridge_transport_t *next=make();assert(next==t&&next->generation!=first);
        delayed.callback(delayed.arg);assert(!atomic_load(&next->cancelled));
        assert(bridge_transport_start(next)==ESP_OK);finish_and_dispose(next);no_automatic_work(2);
    }else if(!strcmp(which,"active-cutoff-join")||!strcmp(which,"active-cutoff-timeout")){
        arm(G_TIMER);bridge_transport_t *t=make();assert(t);delayed=t->cutoff->args;
        pthread_t timer;assert(!pthread_create(&timer,NULL,timer_entry,NULL));await(G_TIMER);
        assert(t->timer_users==1);
        if(!strcmp(which,"active-cutoff-timeout")){
            assert(!bridge_transport_dispose(t,esp_timer_get_time()+20000));assert_retained(t);
            assert(!atomic_load(&app_frees)&&atomic_load(&timers_live)==1);
            release(G_TIMER);assert(!pthread_join(timer,NULL));finish_and_dispose(t);
        }else{
            dispose_target=t;pthread_t disposer;assert(!pthread_create(&disposer,NULL,dispose_entry,NULL));
            int64_t until=esp_timer_get_time()+5000000;
            for(;;){portENTER_CRITICAL(&cutoff_mux);bool withdrawn=cutoff_owner==NULL;portEXIT_CRITICAL(&cutoff_mux);if(withdrawn)break;assert(esp_timer_get_time()<until);sched_yield();}
            assert(!atomic_load(&dispose_done)&&!atomic_load(&app_frees)&&atomic_load(&timers_live)==1);
            release(G_TIMER);assert(!pthread_join(timer,NULL));assert(!pthread_join(disposer,NULL));
            assert(atomic_load(&dispose_done)&&dispose_result);clean();
        }
        no_automatic_work(0);
    }else if(!strcmp(which,"timer-stop-fail")||!strcmp(which,"timer-delete-fail")){
        fail_timer_stop=!strcmp(which,"timer-stop-fail");fail_timer_delete=!fail_timer_stop;
        bridge_transport_t *t=make();assert(t);
        assert(!bridge_transport_dispose(t,esp_timer_get_time()+20000));assert_retained(t);
        assert(t->cutoff&&atomic_load(&timers_live)==1&&!atomic_load(&client_frees));
        fail_timer_stop=fail_timer_delete=false;finish_and_dispose(t);no_automatic_work(0);
    }else{
        assert(!strcmp(which,"immediate-exit")||!strcmp(which,"normal-close")||!strcmp(which,"peer-failure"));
        connect_failure=!strcmp(which,"immediate-exit");peer_failure=!strcmp(which,"peer-failure");
        for(unsigned i=0;i<2;i++){
            bridge_transport_t *t=make();assert(t&&bridge_transport_start(t)==ESP_OK);
            assert(atomic_load(&start_tail)==1&&bridge_transport_finished(t));
            assert(!atomic_load(&t->owner_done)&&!atomic_load(&t->task_exited)&&!atomic_load(&client_frees));
            assert(bridge_transport_start(t)==ESP_ERR_INVALID_STATE);
            finish_and_dispose(t);assert(atomic_load(&destructors)==1);
            no_automatic_work(i+1);
            assert(atomic_load(&disconnect_events)==((connect_failure||peer_failure)?i+1:0));
            assert(atomic_load(&closed_events)==((connect_failure||peer_failure)?0:i+1));
            if(!i)reset_observers();
        }
    }
    if(!app_slot_live&&app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
    printf("PASS LIFECYCLE %s: actual adapter + complete pinned SDK client task/start/abort/destroy; synthetic RTOS/events/TLS/recv, real host pthread destructor; NOT device proof.\n",which);
    return 0;
}
