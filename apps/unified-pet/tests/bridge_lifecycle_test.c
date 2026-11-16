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
#endif

typedef void *TaskHandle_t;
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
enum gate_id {G_BEGIN,G_FINISH,G_STOPPED,G_DESTRUCTOR,G_TIMER,G_COUNT};
static struct {bool armed,reached,released;} gates[G_COUNT];
static pthread_mutex_t control=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed=PTHREAD_COND_INITIALIZER;
static _Thread_local bool sdk_thread,timer_thread;
static pthread_t worker;
static bool worker_exists,early_exit=true,creating_client;
static bool fail_create,fail_timer_start,fail_key,fail_setspecific,fail_self_destroy;
static bool fail_timer_stop,fail_timer_delete,connect_failure,peer_failure;
static atomic_uint connects,control_sends,payload_sends,closed_events,disconnect_events;
static atomic_uint clients_live,ws_live,ssl_live,locks_live,timers_live,destructors,start_tail;
static atomic_uint client_frees,app_frees;
static esp_event_handler_t event_handler;
static void *event_arg;
static esp_websocket_client_handle_t last_client;
static void (*app_destructor)(void*);
static char order[32];static size_t order_at;
static void *app_slot;static size_t app_size;static bool app_slot_live;
#ifdef BRIDGE_RECEIVE_TEST
static unsigned char rx_wire[200000],tx_wire[10000];
static size_t rx_size,rx_at,tx_size,tcp_split=8192;
static bool fixture_eof,fixture_socket=true;
static unsigned data_events,control_events,zero_control_events,continuation_events,split_events;
static size_t controlled_payload_begin;
static unsigned payload_reads,fault_payload_read,cancel_payload_read;
static int fault_payload_result,payload_waits[130];
static int64_t payload_latency_us;
static bool fake_clock;
static int64_t fake_now;
static esp_websocket_event_data_t pending_event;
static int pending_id;static bool pending_valid;
static struct {esp_event_handler_t fn;void *arg;} handlers[2];
static unsigned handler_count;
static const char *WEBSOCKET_EVENTS="fixture websocket";
static int (*vendor_ws_destroy)(esp_transport_handle_t);
static esp_err_t esp_event_post_to(void *loop,esp_event_base_t base,int id,
                                  const void *event,size_t n,unsigned ticks){
    (void)loop;(void)ticks;assert(base==WEBSOCKET_EVENTS&&!pending_valid&&n==sizeof(pending_event));
    pending_event=*(const esp_websocket_event_data_t*)event;pending_id=id;pending_valid=true;return ESP_OK;
}
static int esp_tls_get_and_clear_last_error(void *e,int *stack,int *flags){
    (void)e;*stack=*flags=0;return 0; /* synthetic TLS error boundary only */
}
#endif

int64_t esp_timer_get_time(void){
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
#include "../main/bridge_transport.c"
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
        if(pending_id==WEBSOCKET_EVENT_DATA){
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
esp_err_t esp_timer_start_once(esp_timer_handle_t t,uint64_t us){assert(t&&us);if(fail_timer_start)return ESP_FAIL;t->active=true;return ESP_OK;}
esp_err_t esp_timer_stop(esp_timer_handle_t t){assert(t);if(fail_timer_stop)return ESP_FAIL;bool active=t->active;t->active=false;return active?ESP_OK:ESP_ERR_INVALID_STATE;}
esp_err_t esp_timer_delete(esp_timer_handle_t t){assert(t&&!t->active);if(fail_timer_delete)return ESP_FAIL;atomic_fetch_sub(&timers_live,1);free(t);return ESP_OK;}
void esp_log_level_set(const char *tag,int level){(void)tag;assert(level==ESP_LOG_NONE);}
void fixture_log(const char *tag,const char *fmt,...){(void)tag;(void)fmt;} /* no tokens/transcripts emitted */
esp_err_t esp_crt_bundle_attach(void *p){(void)p;return ESP_OK;}

static int raw_connect(esp_transport_handle_t t,const char *h,int p,int ms){(void)t;assert(h&&p==9443&&ms>0);atomic_fetch_add(&connects,1);return connect_failure?-1:0;}
static int raw_read(esp_transport_handle_t t,char *b,int n,int ms){
    (void)t;(void)ms;
#ifdef BRIDGE_RECEIVE_TEST
    assert(n>0&&b);
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
    if(count>tcp_split)count=tcp_split;
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
typedef struct {char *items[4];unsigned n;} *QueueHandle_t;
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
    assert(!ticks);if(q->n==4)return 0;q->items[q->n++]=*(char *const*)p;return pdTRUE;
}
static int xQueueReceive(QueueHandle_t q,void *out,unsigned ticks){
    (void)ticks;if(!q->n)return 0;*(char**)out=q->items[0];
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
static struct {char *items[4];unsigned n;} receive_queue;
static struct {unsigned char bytes[96000];size_t n;} capture_buffer,playback_buffer;
static struct fixture_events audio_events={.bits=BV_IDLE};
static char assembly_storage[BV_FRAME],append_json[7000];
static unsigned char append_storage[BV_APPEND],decode_storage[BV_APPEND];
static void add_frame(unsigned op,bool fin,const void *payload,size_t n){
    assert(rx_at==0&&n<65536&&rx_size+n+4<sizeof(rx_wire));
    rx_wire[rx_size++]=(unsigned char)((fin?0x80:0)|op);
    if(n<126)rx_wire[rx_size++]=(unsigned char)n;
    else{rx_wire[rx_size++]=126;rx_wire[rx_size++]=(unsigned char)(n>>8);rx_wire[rx_size++]=(unsigned char)n;}
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
static void empty_wire(void){assert(rx_at==rx_size);rx_at=rx_size=0;fixture_eof=false;}
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
    char *p=NULL;while(xQueueReceive((void*)&receive_queue,&p,0)==pdTRUE){setup_bridge_wipe(p,strlen(p));free(p);}
    assert(bridge_transport_dispose(t,esp_timer_get_time()+1000000));
    clean();if(app_slot){__asan_unpoison_memory_region(app_slot,app_size);free(app_slot);app_slot=NULL;}
}
static int run_receive(const char *which){
    bridge_transport_t *t=receive_init();bridge_voice_t *c=&receive_context;
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
                assert(rx_at==rx_size&&receive_queue.n==0);check_one_pong(payload,n);
            }else{
                fprintf(stderr,"CONTROL %s result=%d payload_reads=%u fake_us=%lld raw_consumed=%zu raw_remaining=%zu cancelled=%d\n",
                        which,result,payload_reads,(long long)(fake_clock?fake_now-1000000:0),
                        rx_at,rx_size-rx_at,atomic_load(&t->cancelled));
                assert(result==ESP_FAIL);failed_control(t);
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

int main(int argc,char **argv){
    assert(argc==2);const char *which=argv[1];
#ifdef BRIDGE_RECEIVE_TEST
    return run_receive(which);
#endif
    if(!strcmp(which,"key-create-fail")){
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
