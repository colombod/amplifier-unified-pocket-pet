#include "bridge_transport.h"
#include <stdatomic.h>
#include <limits.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <pthread.h>
#include "esp_crt_bundle.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_timer.h"
#include <sys/socket.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_transport_ssl.h"
#include "esp_transport_ws.h"
/* Pinned IDF 5.5.4 private ABI is deliberately explicit. Public set_func has no
 * get-socket setter: a new proxy would yield -1 and vendor FD_SET(-1). Preserve
 * the real SSL object's context/foundation/I/O/socket functions; replace ONLY
 * its connect hooks plus serialized I/O/close wrappers, and the WS connect hook.
 * No vendor bytes are modified.
 * This ABI dependency must pass pinned-source tests before physical use. */
#include "esp_transport_internal.h"
#if ESP_IDF_VERSION != ESP_IDF_VERSION_VAL(5,5,4)
#error Bridge transport requires inspected ESP-IDF 5.5.4
#endif
#if !CONFIG_LOG_DYNAMIC_LEVEL_CONTROL
#error Dynamic log control is required before introducing a pet token
#endif
struct bridge_transport {
    setup_bridge_t cfg;
    char ip[16];
    esp_transport_handle_t ssl,ws;
    esp_websocket_client_handle_t client;
    connect_func ssl_connect,ws_connect;
    io_read_func read,ws_read;
    io_func write;
    poll_func poll_read,poll_write;
    trans_func close;
    SemaphoreHandle_t io;
    int64_t deadline;
    /* Access ONLY under io. A recursive SDK poll inherits its caller's fixed
     * deadline, not a new timeout. Zero means no enclosing SSL read/write. */
    int64_t operation_deadline;
    bool operation_probe,shutdown_done;
    esp_timer_handle_t cutoff;
    atomic_bool attempted,started,upgraded,cancelled;
    atomic_bool finishing,owner_done,task_exited,hook_failed;
    bool task_created;
    uintptr_t generation;
    unsigned timer_users; /* cutoff_mux only */
};
static _Atomic(bridge_transport_t*) chain;
static portMUX_TYPE cutoff_mux=portMUX_INITIALIZER_UNLOCKED;
static bridge_transport_t *cutoff_owner;
static uintptr_t cutoff_generation;
static pthread_key_t exit_key;
static pthread_once_t exit_once=PTHREAD_ONCE_INIT;
static int exit_key_error;
/* Pinned IDF calls pthread key destructors for raw xTaskCreate tasks from TCB
 * cleanup, after task deletion. Use pthread's key API, not its reserved slot 0.
 * One key per boot; IDF frees each task's value/list. A boot-lifetime mux also
 * fences the END of the atomic primitive: byte atomics are not lock-free on
 * this Xtensa target. No event-group or allocated lock is signalled/deleted. */
static void task_deleted(void *arg){
    bridge_transport_t *t=arg;
    portENTER_CRITICAL(&cutoff_mux);
    atomic_store_explicit(&t->task_exited,true,memory_order_release);
    portEXIT_CRITICAL(&cutoff_mux); /* no t access after unlock */
}
static void create_exit_key(void){exit_key_error=pthread_key_create(&exit_key,task_deleted);}
static void lifecycle_event(void *arg,esp_event_base_t base,int32_t id,void *data){
    (void)base;(void)data;bridge_transport_t *t=arg;
    if(id==WEBSOCKET_EVENT_BEGIN){
        if(pthread_setspecific(exit_key,t)){
            atomic_store(&t->hook_failed,true);bridge_transport_cancel(t);
        }
    }else if(id==WEBSOCKET_EVENT_FINISH){
        atomic_store(&t->finishing,true);
        /* FINISH is before the SDK's last client/transport accesses. Keep it
         * alive until start's post-xTaskCreate tail and all owner API use end.
         * Self-destruction then happens on the SDK task; only its TCB destructor
         * above proves final access. No public stop/destroy wait is used. */
        while(!atomic_load(&t->owner_done))vTaskDelay(1);
        if(esp_websocket_client_destroy_on_exit(t->client)!=ESP_OK)
            atomic_store(&t->hook_failed,true);
    }
}
static int64_t call_deadline(bridge_transport_t *t,int requested){
    int64_t entry=esp_timer_get_time(),d=t->deadline;
    /* Compare before adding, including for arbitrarily large caller timeouts. */
    if(requested>=0&&d>entry&&(int64_t)requested*1000<d-entry)
        d=entry+(int64_t)requested*1000;
    return d;
}
/* -1 denies new I/O; 0 exhausts this caller's budget. A zero-time call has one
 * nonblocking probe, never a sleep/retry. Sub-ms residue cannot fund a wait. */
static int budget(bridge_transport_t *t,int64_t d,bool probe){
    int64_t now=esp_timer_get_time();
    if(atomic_load(&t->cancelled)||t->deadline-now<1000)return -1;
    int64_t left=d-now;
    if(left<=0||probe)return 0;
    int64_t ms=left/1000;
    return ms>INT_MAX?INT_MAX:(int)ms;
}
static bool call_expired(int64_t d,bool probe){
    /* Zero is the explicit single-probe exception: CPU time between entry and
     * the probe must not suppress it. It never waits/retries, and cancellation
     * plus the hard deadline are still checked by budget() at every admission. */
    return !probe&&d-esp_timer_get_time()<1000;
}
static void shutdown_cancelled_owned(bridge_transport_t *t){
    /* Never retain an FD outside io, and never close/free beneath vendor TLS.
     * A busy cancel leaves the latch for the current owner to service on exit. */
    if(atomic_load(&t->cancelled)&&!t->shutdown_done){
        int fd=t->ssl?esp_transport_get_socket(t->ssl):-1;
        if(fd>=0){shutdown(fd,SHUT_RDWR);t->shutdown_done=true;}
    }
}
static void give_io(bridge_transport_t *t){
    shutdown_cancelled_owned(t);
    xSemaphoreGiveRecursive(t->io);
    /* Cancellation may latch after the last owned check but before this give.
     * Retry ownership without waiting; if another task acquired it, that owner
     * must perform the same handoff on release. Never observe an unowned FD.
     * Use a direct give here: the latch is permanent and shutdown once-only. */
    if(atomic_load(&t->cancelled)&&xSemaphoreTakeRecursive(t->io,0)==pdTRUE){
        shutdown_cancelled_owned(t);
        xSemaphoreGiveRecursive(t->io);
    }
}
static int take_io(bridge_transport_t *t,int64_t d,bool probe){
    for(;;){
        int wait=budget(t,d,probe);
        if(wait<0)return -1;
        if(!wait&&call_expired(d,probe))return 0;
        if(wait>50)wait=50;
        TickType_t ticks=pdMS_TO_TICKS(wait);
        /* At the pinned 1000 Hz, every positive ms is at least one tick.
         * At slower rates do not turn a positive wait into a spinning probe.
         * Scheduler granularity may overshoot D; recheck before any admission. */
        if(wait&&!ticks)ticks=1;
        int64_t before=esp_timer_get_time();
        if(xSemaphoreTakeRecursive(t->io,ticks)==pdTRUE)return 1;
        if(probe)return 0;
        if(esp_timer_get_time()==before)vTaskDelay(1);
    }
}
static bool socket_valid_owned(bridge_transport_t *t){
    return esp_transport_get_socket(t->ssl)>=0;
}
void bridge_transport_cancel(bridge_transport_t *t){
    if(!t||atomic_exchange(&t->cancelled,true))return;
    /* Zero-wait cancellation, not a join or a 50ms bound on admitted TLS. */
    if(t->io&&xSemaphoreTakeRecursive(t->io,0)==pdTRUE){
        give_io(t);
    }
}
static void cutoff(void *arg){
    /* stop/delete does not join an already-selected timer callback. Never put
     * an allocated pointer in its arg. Resolve and pin a non-reused generation
     * under this boot-lifetime mux, so delayed callbacks cannot cancel a new
     * connection even if malloc reuses its address. */
    portENTER_CRITICAL(&cutoff_mux);
    bridge_transport_t *t=cutoff_owner;
    if(t&&t->generation==(uintptr_t)arg)t->timer_users++;
    else t=NULL;
    portEXIT_CRITICAL(&cutoff_mux);
    if(!t)return;
    bridge_transport_cancel(t);
    portENTER_CRITICAL(&cutoff_mux);
    t->timer_users--;
    portEXIT_CRITICAL(&cutoff_mux); /* no t access after unlock */
}
static int bounded_read(esp_transport_handle_t ssl,char *b,int n,int ms){
    bridge_transport_t *t=atomic_load(&chain);
    if(!t||ssl!=t->ssl||n<0||(!b&&n))return -1;
    int64_t d=call_deadline(t,ms);bool probe=ms==0;
    int held=take_io(t,d,probe);
    if(held<=0)return held<0?-1:ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT;
    int64_t saved=t->operation_deadline;bool saved_probe=t->operation_probe;
    if(saved){d=saved<d?saved:d;probe=probe&&saved_probe;}
    t->operation_deadline=d;t->operation_probe=probe;
    int wait=budget(t,d,probe),result;
    if(wait<0||!socket_valid_owned(t))result=-1;
    else if(!wait&&call_expired(d,probe))result=ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT;
    else result=t->read(ssl,b,n,wait); /* remaining CALLER budget, not 50 */
    t->operation_deadline=saved;t->operation_probe=saved_probe;
    give_io(t);return result;
}
static int bounded_write(esp_transport_handle_t ssl,const char *b,int n,int ms){
    bridge_transport_t *t=atomic_load(&chain);
    if(!t||ssl!=t->ssl||n<0||(!b&&n))return -1;
    int64_t d=call_deadline(t,ms);bool probe=ms==0;
    int held=take_io(t,d,probe);
    if(held<=0)return held;
    int64_t saved=t->operation_deadline;bool saved_probe=t->operation_probe;
    if(saved){d=saved<d?saved:d;probe=probe&&saved_probe;}
    t->operation_deadline=d;t->operation_probe=probe;
    int total=0,result;
    for(;;){
        int wait=budget(t,d,probe);
        if(wait<0||!socket_valid_owned(t)){result=-1;break;}
        if(!wait&&call_expired(d,probe)){result=0;break;}
        result=t->write(ssl,b?b+total:NULL,n-total,wait);
        /* Only an EXACT known positive byte count permits continuation of the
         * remaining bytes under this SAME D and lock. Never retry zero/error,
         * or return a short positive payload for SDK WS to refragment. */
        if(result<=0)break;
        if(result>n-total){result=-1;break;}
        total+=result;
        if(total==n){result=total;break;}
        if(probe){result=0;break;}
    }
    t->operation_deadline=saved;t->operation_probe=saved_probe;
    give_io(t);return result;
}
static int bounded_poll(esp_transport_handle_t ssl,int ms,bool read){
    bridge_transport_t *t=atomic_load(&chain);
    if(!t||ssl!=t->ssl)return -1;
    int64_t d=call_deadline(t,ms);bool probe=ms==0;
    for(;;){
        int held=take_io(t,d,probe);
        if(held<=0)return held;
        /* Absolute cancellation/cutoff is an error, not an ordinary caller
         * timeout, including when it happened during recursive acquisition. */
        if(budget(t,d,probe)<0){give_io(t);return -1;}
        bool nested=t->operation_deadline!=0;
        if(nested){
            /* A shorter nested request may narrow, never replace/enlarge, its
             * enclosing allowance. Either explicit zero means one probe, but
             * cannot revive an already-expired positive enclosing budget. */
            if(!t->operation_probe&&call_expired(t->operation_deadline,false)){
                give_io(t);return 0;
            }
            if(t->operation_deadline<d)d=t->operation_deadline;
            probe=probe||t->operation_probe;
        }
        int wait=budget(t,d,probe),result;
        int64_t before=esp_timer_get_time();
        if(wait<0||!socket_valid_owned(t))result=-1;
        else if(!wait&&call_expired(d,probe))result=0;
        else{
            /* Idle RX shares io with every WS preflight/header/payload write.
             * Never spend a socket-readiness wait holding that mutex: probe
             * under ownership, then wait outside it below. Nested SSL reads
             * retain their enclosing owner and normal remaining-time slices. */
            if(read&&!nested)wait=0;
            else if(wait>50)wait=50;
            result=(read?t->poll_read:t->poll_write)(ssl,wait);
            /* Pinned ssl_read tests exactly -1 and 0. Never leak another
             * negative poll value through to its TLS branch. Preserve positive
             * cached-byte availability, unless the admission fence changed. */
            if(result<0)result=-1;
            else{
                wait=budget(t,d,probe);
                if(wait<0||!socket_valid_owned(t))result=-1;
                else if(!wait&&call_expired(d,probe))result=0;
            }
        }
        bool again=result==0&&!probe&&budget(t,d,probe)>0;
        give_io(t); /* nested give retains the enclosing SSL owner */
        if(!again)return result;
        /* Idle standalone RX must let Start TX run, even at a lower priority.
         * Nested polls may NOT release the outer TLS lock. An unexpectedly
         * immediate zero still advances RTOS time instead of busy-spinning. */
        if((read&&!nested)||esp_timer_get_time()==before)vTaskDelay(1);
    }
}
static int bounded_poll_read(esp_transport_handle_t ssl,int ms){return bounded_poll(ssl,ms,true);}
static int bounded_poll_write(esp_transport_handle_t ssl,int ms){return bounded_poll(ssl,ms,false);}
static int bounded_close(esp_transport_handle_t ssl){
    bridge_transport_t *t=atomic_load(&chain);
    if(!t||ssl!=t->ssl||xSemaphoreTakeRecursive(t->io,pdMS_TO_TICKS(50))!=pdTRUE)return -1;
    shutdown_cancelled_owned(t);
    int result=t->close(ssl);give_io(t);return result;
}
static int deny_async(esp_transport_handle_t t,const char *h,int p,int ms){(void)t;(void)h;(void)p;(void)ms;return -1;}
static int one_connect(esp_transport_handle_t ssl,const char *host,int port,int timeout){
    bridge_transport_t *t=atomic_load(&chain);
    if(!t||ssl!=t->ssl||!host||strcmp(host,t->ip)||port!=t->cfg.port||timeout<=0||
       atomic_load(&t->cancelled)||esp_timer_get_time()>=t->deadline)return -1;
    /* Burn before TLS, including failed attempts. No close/restart resets it. */
    if(atomic_exchange(&t->attempted,true))return -1;
    int64_t d=call_deadline(t,timeout);
    if(take_io(t,d,false)!=1)return -1;
    timeout=budget(t,d,false);
    int result=timeout>0?t->ssl_connect(ssl,t->ip,t->cfg.port,timeout):-1;
    if(atomic_load(&t->cancelled)||esp_timer_get_time()>=t->deadline){
        if(esp_timer_get_time()>=t->deadline)atomic_store(&t->cancelled,true);
        give_io(t);
        return -1;
    }
    give_io(t);
    return result;
}
static int strict_upgrade(esp_transport_handle_t ws,const char *host,int port,int timeout){
    bridge_transport_t *t=atomic_load(&chain);
    if(!t||ws!=t->ws||!host||strcmp(host,t->ip)||port!=t->cfg.port||
       atomic_load(&t->cancelled)||esp_timer_get_time()>=t->deadline)return -1;
    int result=t->ws_connect(ws,t->ip,t->cfg.port,timeout);
    int status=esp_transport_ws_get_upgrade_request_status(ws);
    /* Return negative for ALL 3xx, including Location/no-Location, so the
     * client's redirect branch never runs. A verified 101 is the only success. */
    if(result!=0||status!=101||atomic_load(&t->cancelled)||esp_timer_get_time()>=t->deadline)return -1;
    atomic_store(&t->upgraded,true);return 0;
}
static int complete_control_read(esp_transport_handle_t ws,char *b,int n,int ms){
    bridge_transport_t *t=atomic_load(&chain);
    if(!t||ws!=t->ws||!b||n<=0||atomic_load(&t->cancelled))return -1;
    int64_t d=call_deadline(t,ms);bool probe=ms==0;
    int wait=budget(t,d,probe);
    if(wait<0)return -1;
    if(!wait&&call_expired(d,probe))return ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT;
    int r=t->ws_read(ws,b,n,wait);
    if(r<0)return r;
    ws_transport_opcodes_t opcode=esp_transport_ws_get_read_opcode(ws);
    if(!(opcode&8))return r; /* data stays streaming; never fill an 8192B request */
    int payload=esp_transport_ws_get_read_payload_len(ws);
    /* SDK1.6.1 reuses rx_buffer's start for each short read, then sends the
     * final buffer as the WHOLE pong. Return a control payload atomically,
     * using only known-positive byte counts; no vendor/cache edit is needed.
     * RFC6455 controls fit the caller's buffer, are final and <=125 bytes. */
    if((opcode!=8&&opcode!=9&&opcode!=10)||!esp_transport_ws_get_fin_flag(ws)||
       payload<0||payload>125||payload>n||r>payload||(!r&&payload))goto invalid;
    int total=r;
    while(total<payload){
        wait=budget(t,d,probe);
        if(probe||wait<=0||call_expired(d,false))goto invalid;
        r=t->ws_read(ws,b+total,payload-total,wait);
        if(r<=0||r>payload-total||
           esp_transport_ws_get_read_opcode(ws)!=opcode||
           esp_transport_ws_get_read_payload_len(ws)!=payload||
           !esp_transport_ws_get_fin_flag(ws))goto invalid;
        total+=r;
    }
    if(budget(t,d,probe)<0)goto invalid;
    return total;
invalid:
    /* A partial control is never exposed to SDK automatic PONG handling.
     * Error/zero/timeout is terminal, not permission to replay a read/call. */
    bridge_transport_cancel(t);return -1;
}
bridge_transport_t *bridge_transport_new(const setup_bridge_t *cfg,const char *token,int64_t deadline){
    if(!setup_bridge_valid(cfg)||!cfg->enabled||!setup_bridge_token_valid(token)||
       time(NULL)<=1760000000||deadline<=esp_timer_get_time())return NULL;
    if(pthread_once(&exit_once,create_exit_key)||exit_key_error)return NULL;
    bridge_transport_t *t=calloc(1,sizeof(*t));if(!t)return NULL;
    bridge_transport_t *none=NULL;
    if(!atomic_compare_exchange_strong(&chain,&none,t)){free(t);return NULL;}
    t->cfg=*cfg;t->deadline=deadline;snprintf(t->ip,sizeof(t->ip),"%u.%u.%u.%u",cfg->ip[0],cfg->ip[1],cfg->ip[2],cfg->ip[3]);
    /* Pinned SSL read/write call the transport's poll hooks on this same task.
     * Keep I/O serialized against other tasks, but permit that nested polling. */
    t->io=xSemaphoreCreateRecursiveMutex();if(!t->io)goto fail;
    /* Exact pinned tags. Request/response logs can contain authorization or a
     * malicious redirect. These tags remain suppressed after this operation. */
    esp_log_level_set("transport_ws",ESP_LOG_NONE);
    esp_log_level_set("websocket_client",ESP_LOG_NONE);
    esp_log_level_set("transport_base",ESP_LOG_NONE);
    t->ssl=esp_transport_ssl_init();if(!t->ssl)goto fail;
    esp_transport_ssl_crt_bundle_attach(t->ssl,esp_crt_bundle_attach);
    esp_transport_ssl_set_common_name(t->ssl,t->cfg.fqdn);
    t->ssl_connect=t->ssl->_connect;
    t->read=t->ssl->_read;t->write=t->ssl->_write;
    t->poll_read=t->ssl->_poll_read;t->poll_write=t->ssl->_poll_write;
    t->close=t->ssl->_close;
    if(!t->ssl_connect||!t->ssl->_get_socket||!t->ssl->foundation)goto fail;
    if(!t->read||!t->write||!t->poll_read||!t->poll_write||!t->close)goto fail;
    t->ssl->_connect=one_connect;t->ssl->_connect_async=deny_async;
    t->ssl->_read=bounded_read;t->ssl->_write=bounded_write;
    t->ssl->_poll_read=bounded_poll_read;t->ssl->_poll_write=bounded_poll_write;
    t->ssl->_close=bounded_close;
    t->ws=esp_transport_ws_init(t->ssl);if(!t->ws)goto fail;
    t->ws_connect=t->ws->_connect;t->ws_read=t->ws->_read;
    if(!t->ws_connect||!t->ws_read)goto fail;
    t->ws->_connect=strict_upgrade;t->ws->_connect_async=deny_async;
    t->ws->_read=complete_control_read;
    t->ws->foundation=t->ssl->foundation; /* borrowed; WS destroy does not free foundation */
    char auth[52]={0};snprintf(auth,sizeof(auth),"Bearer %s",token);
    esp_transport_ws_config_t w={.ws_path="/v1/pet/live",.auth=auth,.propagate_control_frames=true};
    esp_err_t e=esp_transport_ws_set_config(t->ws,&w);
    setup_bridge_wipe(auth,sizeof(auth)); /* pinned implementation strdup at setter */
    if(e!=ESP_OK)goto fail;
    esp_websocket_client_config_t client={.host=t->ip,.port=cfg->port,.path="/v1/pet/live",
        .transport=WEBSOCKET_TRANSPORT_OVER_SSL,.disable_auto_reconnect=true,.enable_close_reconnect=false,
        .ext_transport=t->ws,.network_timeout_ms=5000,.buffer_size=8192,.task_stack=8192,
        .task_prio=5,.ping_interval_sec=15,.pingpong_timeout_sec=30};
    t->client=esp_websocket_client_init(&client);if(!t->client)goto fail;
    if(esp_websocket_register_events(t->client,WEBSOCKET_EVENT_ANY,lifecycle_event,t)!=ESP_OK)goto fail;
    portENTER_CRITICAL(&cutoff_mux);
    if(cutoff_generation!=UINTPTR_MAX){
        t->generation=++cutoff_generation;cutoff_owner=t;
    }
    portEXIT_CRITICAL(&cutoff_mux);
    if(!t->generation)goto fail;
    esp_timer_create_args_t timer={.callback=cutoff,.arg=(void*)t->generation,.name="pet_bridge_cutoff"};
    if(esp_timer_create(&timer,&t->cutoff)!=ESP_OK)goto fail;
    return t;
fail:
    bridge_transport_dispose_unstarted(t);return NULL;
}
esp_websocket_client_handle_t bridge_transport_client(bridge_transport_t *t){return t?t->client:NULL;}
esp_err_t bridge_transport_start(bridge_transport_t *t){
    if(!t||!t->client||atomic_load(&t->cancelled)||atomic_load(&t->owner_done)||
       atomic_exchange(&t->started,true))return ESP_ERR_INVALID_STATE;
    int64_t left=t->deadline-esp_timer_get_time();
    if(left<=0||esp_timer_start_once(t->cutoff,(uint64_t)left)!=ESP_OK){
        bridge_transport_cancel(t);return ESP_ERR_INVALID_STATE;
    }
    esp_err_t result=esp_websocket_client_start(t->client);
    t->task_created=result==ESP_OK; /* pinned failed xTaskCreate creates no task */
    return result;
}
bool bridge_transport_started(const bridge_transport_t *t){return t&&atomic_load(&t->started);}
bool bridge_transport_finished(const bridge_transport_t *t){return t&&atomic_load(&t->finishing);}
bool bridge_transport_upgraded(bridge_transport_t *t){return t&&!atomic_load(&t->cancelled)&&esp_timer_get_time()<t->deadline&&atomic_load(&t->upgraded)&&esp_transport_ws_get_upgrade_request_status(t->ws)==101;}
void bridge_transport_dispose_unstarted(bridge_transport_t *t){
    if(!t||atomic_load(&t->started))return;
    (void)bridge_transport_dispose(t,esp_timer_get_time());
}
bool bridge_transport_dispose(bridge_transport_t *t,int64_t until){
    if(!t)return true;
    bridge_transport_cancel(t);
    /* Caller has ended ALL client API use, including start's tail. FINISH may
     * now use supported self-destruction. A false result retains every object
     * still reachable; it never invents an exit or authorizes another call. */
    atomic_store(&t->owner_done,true);
    portENTER_CRITICAL(&cutoff_mux);
    if(cutoff_owner==t)cutoff_owner=NULL;
    portEXIT_CRITICAL(&cutoff_mux);
    for(;;){
        portENTER_CRITICAL(&cutoff_mux);
        bool idle=t->timer_users==0;
        portEXIT_CRITICAL(&cutoff_mux);
        if(idle)break;
        if(esp_timer_get_time()>=until)return false;
        vTaskDelay(1);
    }
    if(t->cutoff){
        esp_err_t stopped=esp_timer_stop(t->cutoff);
        if(stopped!=ESP_OK&&stopped!=ESP_ERR_INVALID_STATE)return false;
        if(esp_timer_delete(t->cutoff)!=ESP_OK)return false;
        t->cutoff=NULL;
    }
    if(t->task_created){
        for(;;){
            portENTER_CRITICAL(&cutoff_mux);
            bool exited=atomic_load_explicit(&t->task_exited,memory_order_acquire);
            portEXIT_CRITICAL(&cutoff_mux);
            if(exited)break;
            if(esp_timer_get_time()>=until)return false;
            vTaskDelay(1);
        }
        if(atomic_load(&t->hook_failed))return false;
    }else if(t->client&&esp_websocket_client_destroy(t->client)!=ESP_OK)return false;
    if(t->ws)esp_transport_destroy(t->ws);
    if(t->ssl)esp_transport_destroy(t->ssl);
    if(t->io)vSemaphoreDelete(t->io);
    bridge_transport_t *same=t;atomic_compare_exchange_strong(&chain,&same,NULL);
    setup_bridge_wipe(t,sizeof(*t));free(t);
    return true;
}
