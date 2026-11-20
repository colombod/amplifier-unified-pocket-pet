/* Exact app adapter, SDK transport/WS and extracted SDK SSL read/write.
 * Only the TLS/error I/O, base readiness and RTOS boundaries are synthetic.
 * No network/socket is opened. Compile/run only in the authorized SDK container. */
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <sys/time.h>
#include <sys/socket.h>
#include "bridge_transport.h"
#include "esp_transport_internal.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "mbedtls/sha1.h"
#include "mbedtls/base64.h"
#include "mbedtls/x509.h"
#include "freertos/semphr.h"
struct fixture_lock{bool recursive;unsigned owner,depth;};
struct fixture_timer{esp_timer_create_args_t args;uint64_t us;};
struct fixture_client{esp_websocket_client_config_t cfg;esp_event_handler_t handler;void *arg;};
static int64_t now=1000000;
static unsigned clock_step_us;
static unsigned connects,auth_writes,raw_writes,polls,client_starts,client_destroys,closes;
static unsigned raw_reads,nested_read_polls,nested_write_polls,error_handle_reads,socket_reads;
static unsigned task=1,live_locks,takes,gives,max_depth,nonrecursive_rejections,contended_takes;
void *xTaskGetCurrentTaskHandle(void){return (void*)(uintptr_t)task;}
static unsigned shutdowns,delays,schedule_tx,max_idle_poll_ms;
static unsigned cancel_give_depth,cancel_give_fired;
static int fixture_fd=12345; /* synthetic identifier; NEVER a host descriptor */
static int64_t release_lock_at;
static int64_t cancel_lock_at,expected_operation_deadline;
static int64_t scheduled_tx_at;
static bridge_transport_t *control_probe_target;
static bool probe_control_during_tls,control_probe_denied;
static bool start_tx_on_delay,cancel_after_ready,close_after_ready,idle_read_only,immediate_zero;
static int write_script[4];static size_t write_script_len,write_script_at;
static const char *expected_write_base;
static size_t expected_write_size,known_written;
static unsigned char observed_write[64];
static bool stall_after_partial;
static char last_plain_write[16];
static int fixture_shutdown(int fd,int how);
static void lock_wait_event(void);
static int tls_read_result=INT_MAX,tls_write_result=INT_MAX,base_poll_result=1,last_poll_ms;
static bool probe_exclusion;
static unsigned exclusion_probes;
enum lock_action{LOCK_NONE,LOCK_CANCEL,LOCK_EXPIRE,LOCK_SHRINK};
static enum lock_action lock_action;
static unsigned action_depth,actions_fired;
static void after_lock_take(SemaphoreHandle_t s);
static void before_lock_give(SemaphoreHandle_t s);
static void check_io_owner(unsigned depth);
static void check_socket_owner(void);
static void check_exclusion(void);
static int status=101;static bool fail_write,fail_connect,cancel_during_connect,location=true;
static esp_tls_last_error_t connect_error;
static const char *gateway_refusal;
static char redirect[256],response[2048],cn[64];static size_t response_at;
static bool muted_ws,muted_client,muted_base,bundle;
static unsigned leaked_logs;
static const char *T="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
/* Opt-in synthetic select/TLS-byte seam for the named WS contract cases.
 * The original 32-I/O/110-HTTP meanings are retained (HTTP is now 150 with
 * 403/409/429/503 added for the reported upgrade status); SHRINK now targets the
 * caller D and exclusion probes use zero-wait (documented in the handoff).
 * There is no
 * fixture retry loop around WS send/read: each operation calls the real SDK
 * exactly once, so a short-poll timeout cannot be concealed by replaying it. */
enum wire_stall{WIRE_TX_READY,WIRE_TX_PAYLOAD,WIRE_TX_PARTIAL,WIRE_RX_HEADER};
static bool wire_enabled,wire_cancel_fired;
static enum wire_stall wire_stall;
static int64_t wire_started,wire_read_ready,wire_write_ready,wire_cancel_at;
static unsigned wire_polls;
static unsigned char wire_tx[256],wire_rx[64];
static size_t wire_tx_len,wire_rx_len,wire_rx_at;
static const unsigned char first_payload[]={0x10,0x21,0x32,0x00,0x54,0x65,0x86,0xf7,0x98};
static const unsigned char second_payload[]={0x7b,0x03,0xf0,0x6e};
static int wire_poll(int ms,bool read);
int64_t esp_timer_get_time(void){int64_t result=now;now+=clock_step_us;return result;}
static SemaphoreHandle_t new_lock(bool recursive){
    SemaphoreHandle_t s=calloc(1,sizeof(*s));assert(s);s->recursive=recursive;live_locks++;return s;
}
SemaphoreHandle_t xSemaphoreCreateMutex(void){return new_lock(false);}
SemaphoreHandle_t xSemaphoreCreateRecursiveMutex(void){return new_lock(true);}
static int take_lock(SemaphoreHandle_t s,unsigned ms,bool recursive){
    /* Wrong API/type, double give, wrong owner and deletion while held are
     * fixture failures, not silently successful mutex operations. */
    int64_t wait_us=(int64_t)ms*1000000/configTICK_RATE_HZ;
    assert(s&&s->recursive==recursive&&wait_us<=50000);
    if(s->depth){
        if(s->owner!=task){
            contended_takes++;
            if(release_lock_at&&release_lock_at<=now+wait_us){
                assert(release_lock_at>=now&&s->depth==1);
                now=release_lock_at;release_lock_at=0;
                s->owner=s->depth=0;gives++; /* scheduled synthetic owner's give */
            }else{
                int64_t until=now+wait_us;
                if(cancel_lock_at&&cancel_lock_at<=until){
                    now=cancel_lock_at;cancel_lock_at=0;lock_wait_event();
                }
                now=until;return 0;
            }
        }
    }
    if(s->depth){
        if(!recursive){nonrecursive_rejections++;return 0;}
    }else{s->owner=task;}
    s->depth++;takes++;if(s->depth>max_depth)max_depth=s->depth;
    after_lock_take(s);
    return 1;
}
int xSemaphoreTake(SemaphoreHandle_t s,unsigned ms){return take_lock(s,ms,false);}
int xSemaphoreTakeRecursive(SemaphoreHandle_t s,unsigned ms){return take_lock(s,ms,true);}
static void give_lock(SemaphoreHandle_t s,bool recursive){
    assert(s&&s->recursive==recursive&&s->depth&&s->owner==task);
    before_lock_give(s); /* inject after the owner's last latch check */
    gives++;if(!--s->depth)s->owner=0;
}
int xSemaphoreGive(SemaphoreHandle_t s){give_lock(s,false);return 1;}
int xSemaphoreGiveRecursive(SemaphoreHandle_t s){give_lock(s,true);return 1;}
void vSemaphoreDelete(SemaphoreHandle_t s){assert(s&&!s->depth&&!s->owner&&live_locks);live_locks--;free(s);}
esp_err_t esp_timer_create(const esp_timer_create_args_t*a,esp_timer_handle_t*out){*out=calloc(1,sizeof(**out));(*out)->args=*a;return ESP_OK;}
esp_err_t esp_timer_start_once(esp_timer_handle_t t,uint64_t us){t->us=us;return ESP_OK;}
esp_err_t esp_timer_stop(esp_timer_handle_t t){t->us=0;return ESP_OK;}
esp_err_t esp_timer_delete(esp_timer_handle_t t){free(t);return ESP_OK;}
esp_err_t esp_crt_bundle_attach(void*p){(void)p;return ESP_OK;}
void esp_log_level_set(const char*s,int level){assert(level==ESP_LOG_NONE);if(!strcmp(s,"transport_ws"))muted_ws=true;else if(!strcmp(s,"websocket_client"))muted_client=true;else if(!strcmp(s,"transport_base"))muted_base=true;}
void fixture_log(const char*tag,const char*fmt,...){
    if((!strcmp(tag,"transport_ws")&&muted_ws)||(!strcmp(tag,"websocket_client")&&muted_client)||(!strcmp(tag,"transport_base")&&muted_base))return;
    char text[8192];va_list a;va_start(a,fmt);vsnprintf(text,sizeof(text),fmt,a);va_end(a);if(strstr(text,T))leaked_logs++;
}
struct timeval*esp_transport_utils_ms_to_timeval(int ms,struct timeval*t){t->tv_sec=ms/1000;t->tv_usec=(ms%1000)*1000;return t;}
static int socket_fn(esp_transport_handle_t t){(void)t;check_socket_owner();socket_reads++;return fixture_fd;}
static int ssl_connect(esp_transport_handle_t t,const char*host,int port,int ms);
/* Deliberately minimal synthetic context; no claim to qualify its SDK layout.
 * The generated SDK functions use only tls/sockfd and the real t->data seam. */
typedef struct transport_esp_tls{
    esp_tls_t *tls;int sockfd;esp_tls_t fixture_tls;
    esp_tls_cfg_t cfg;bool ssl_initialized;
}transport_esp_tls_t;
ssize_t esp_tls_conn_read(esp_tls_t*tls,void*buffer,size_t n){
    assert(tls&&tls->transport);check_io_owner(1);raw_reads++;
    if(probe_control_during_tls){
        probe_control_during_tls=false;
        task=2;control_probe_denied=!bridge_transport_admit_control(control_probe_target,now+1000);task=1;
        assert(control_probe_denied); /* the other task cannot pass the held io */
    }
    if(probe_exclusion)check_exclusion();
    if(wire_enabled){
        assert(wire_stall==WIRE_RX_HEADER&&now>=wire_read_ready&&wire_rx_at<wire_rx_len);
        size_t count=wire_rx_len-wire_rx_at;if(count>n)count=n;
        /* Only byte 0 of the WS header exists at t=0. The remaining header
         * byte and both distinct binary payloads become readable at t=60ms. */
        if(!wire_rx_at&&count>1)count=1;
        memcpy(buffer,wire_rx+wire_rx_at,count);wire_rx_at+=count;
        if(wire_rx_at==1)wire_read_ready=wire_started+60000;
        return (ssize_t)count;
    }
    if(tls_read_result!=INT_MAX)return tls_read_result;
    size_t left=strlen(response)-response_at;if(left>n)left=n;
    memcpy(buffer,response+response_at,left);response_at+=left;return (ssize_t)left;
}
ssize_t esp_tls_conn_write(esp_tls_t*tls,const void*buffer,size_t n){
    assert(tls&&tls->transport);check_io_owner(1);raw_writes++;
    if(probe_exclusion)check_exclusion();
    if(wire_enabled){
        assert(wire_stall!=WIRE_RX_HEADER&&now>=wire_write_ready);
        size_t before=wire_tx_len,count=n;
        /* The real SDK writes a six-byte masked header separately from this
         * short payload. Accept distinct bytes, not just a write-call count. */
        if(wire_stall==WIRE_TX_PARTIAL&&before==6&&count>3)count=3;
        assert(count&&count<=sizeof(wire_tx)-wire_tx_len);
        memcpy(wire_tx+wire_tx_len,buffer,count);wire_tx_len+=count;
        if((wire_stall==WIRE_TX_PAYLOAD&&before==0&&wire_tx_len==6)||
           (wire_stall==WIRE_TX_PARTIAL&&before==6))
            wire_write_ready=wire_started+60000;
        return (ssize_t)count;
    }
    if(expected_write_base){
        assert(buffer==expected_write_base+known_written&&n==expected_write_size-known_written);
        assert(write_script_at<write_script_len);
        int result=write_script[write_script_at++];
        if(result>0&&(size_t)result<=n){
            assert(known_written+(size_t)result<=sizeof(observed_write));
            memcpy(observed_write+known_written,buffer,(size_t)result);known_written+=(size_t)result;
            if(stall_after_partial){now+=7000;base_poll_result=0;}
        }
        return result;
    }
    if(n<=sizeof(last_plain_write))memcpy(last_plain_write,buffer,n);
    const char*b=buffer;
    if(n>4&&!memcmp(b,"GET ",4)){
        assert(muted_ws&&muted_client&&muted_base&&bundle&&!strcmp(cn,"pet.example.net"));
        assert(strstr(b,"GET /v1/pet/live HTTP/1.1\r\n")&&strstr(b,"Host: 192.168.1.50:9443\r\n"));
        assert(strstr(b,"Authorization: Bearer AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\r\n"));auth_writes++;
        const char *start=strstr(b,"Sec-WebSocket-Key: ");assert(start);start+=19;const char*end=strstr(start,"\r\n");assert(end);
        char key[128];size_t size=(size_t)(end-start);memcpy(key,start,size);strcpy(key+size,"258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
        unsigned char sha[20],accept[64];size_t written;
        assert(!mbedtls_sha1((unsigned char*)key,strlen(key),sha));assert(!mbedtls_base64_encode(accept,sizeof(accept),&written,sha,sizeof(sha)));accept[written]=0;
        snprintf(response,sizeof(response),"HTTP/1.1 %d Fixture\r\nSec-WebSocket-Accept: %s\r\n%s%s%s\r\n",status,accept,location?"Location: ":"",location?redirect:"",location?"\r\n":"");response_at=0;
        /* The gateway's own refusal (reject_response in bridge/pet_bridge/
         * gateway_server.py): a fixed JSON body, Connection: close, and no
         * Sec-WebSocket-Accept or Location header at all. */
        if(gateway_refusal)snprintf(response,sizeof(response),"HTTP/1.1 %d %s\r\nContent-Type: application/json; charset=utf-8\r\n"
            "Connection: close\r\nContent-Length: %u\r\n\r\n{\"error\":\"%s\"}",status,gateway_refusal,
            (unsigned)strlen(gateway_refusal)+12,gateway_refusal);
    }
    return fail_write?-1:tls_write_result!=INT_MAX?tls_write_result:(ssize_t)n;
}
esp_err_t esp_tls_get_error_handle(esp_tls_t*tls,esp_tls_error_handle_t*out){
    assert(tls&&out);error_handle_reads++;*out=&tls->error;return ESP_OK;
}
static esp_transport_handle_t ssl_for_new_tls;
esp_tls_t *esp_tls_init(void){
    esp_tls_t *tls=calloc(1,sizeof(*tls));assert(tls);tls->transport=ssl_for_new_tls;return tls;
}
int esp_tls_conn_new_sync(const char *host,int len,int port,const esp_tls_cfg_t *cfg,esp_tls_t *tls){
    assert(tls&&host&&len==(int)strlen(host)&&port==9443&&cfg->timeout_ms>0);
    check_io_owner(1);connects++;
    tls->error=connect_error;return fail_connect?-1:1;
}
int esp_tls_conn_destroy(esp_tls_t *tls){assert(tls);free(tls);return 0;}
esp_err_t esp_tls_get_conn_sockfd(esp_tls_t *tls,int *fd){assert(tls&&fd);*fd=12345;return ESP_OK;}
/* Generated ONLY by the runner after the complete SDK SSL source hash matches.
 * Do not replace these functions with a handwritten poll+I/O approximation. */
#define INVALID_SOCKET (-1) /* pinned transport_ssl.c:20 */
#include "pinned_transport_ssl_io.inc"
static int ssl_poll(esp_transport_handle_t t,int ms,bool read);
static int ssl_poll_read(esp_transport_handle_t t,int ms){return ssl_poll(t,ms,true);}
static int ssl_poll_write(esp_transport_handle_t t,int ms){return ssl_poll(t,ms,false);}
static int ssl_close(esp_transport_handle_t t){(void)t;check_io_owner(1);closes++;fixture_fd=-1;return 0;}
static int ssl_destroy(esp_transport_handle_t t){free(t->data);free(t->foundation->error_handle);free(t->foundation);return 0;}
esp_transport_handle_t esp_transport_ssl_init(void){
    esp_transport_handle_t t=esp_transport_init();assert(t);t->foundation=calloc(1,sizeof(*t->foundation));t->foundation->error_handle=calloc(1,sizeof(*t->foundation->error_handle));
    transport_esp_tls_t*ctx=calloc(1,sizeof(*ctx));assert(ctx);
    ctx->tls=&ctx->fixture_tls;ctx->fixture_tls.transport=t;ctx->sockfd=-1;t->data=ctx;
    esp_transport_set_func(t,ssl_connect,ssl_read,ssl_write,ssl_close,ssl_poll_read,ssl_poll_write,ssl_destroy);t->_get_socket=socket_fn;ssl_for_new_tls=t;return t;
}
void esp_transport_ssl_crt_bundle_attach(esp_transport_handle_t t,esp_err_t(*fn)(void*)){assert(t&&fn==esp_crt_bundle_attach);bundle=true;}
void esp_transport_ssl_set_common_name(esp_transport_handle_t t,const char*s){assert(t);snprintf(cn,sizeof(cn),"%s",s);}
esp_websocket_client_handle_t esp_websocket_client_init(const esp_websocket_client_config_t*c){
    assert(c->disable_auto_reconnect&&!c->enable_close_reconnect&&c->ext_transport&&c->transport==2);assert(!strcmp(c->host,"192.168.1.50")&&!strcmp(c->path,"/v1/pet/live"));
    esp_websocket_client_handle_t h=calloc(1,sizeof(*h));h->cfg=*c;return h;
}
esp_err_t esp_websocket_client_destroy(esp_websocket_client_handle_t h){client_destroys++;free(h);return ESP_OK;}
esp_err_t esp_websocket_client_start(esp_websocket_client_handle_t h){assert(h);client_starts++;return ESP_OK;}
esp_err_t esp_websocket_register_events(esp_websocket_client_handle_t h,int id,esp_event_handler_t fn,void *arg){
    assert(h&&id==WEBSOCKET_EVENT_ANY&&fn);h->handler=fn;h->arg=arg;return ESP_OK;
}
esp_err_t esp_websocket_client_destroy_on_exit(esp_websocket_client_handle_t h){assert(h);return ESP_OK;}
/* Every app shutdown resolves to this guard; no synthetic FD can reach libc. */
#define shutdown fixture_shutdown
#ifdef BRIDGE_TEST_TRANSPORT_MUTANT
#include "bridge-transport-mutant.inc"
#else
#include "../main/bridge_transport.c"
#endif
#undef shutdown
static int fixture_shutdown(int fd,int how){
    bridge_transport_t*t=atomic_load(&chain);
    assert(t&&t->io->owner==task&&t->io->depth>0);
    assert(fd==12345&&fixture_fd==fd&&how==SHUT_RDWR);
    shutdowns++;return 0;
}
static void before_lock_give(SemaphoreHandle_t s){
    if(!cancel_give_depth||s->depth!=cancel_give_depth)return;
    bridge_transport_t*t=atomic_load(&chain);
    assert(t&&s==t->io&&s->owner==task);
    cancel_give_depth=0;cancel_give_fired++;
    unsigned before=socket_reads,saved_task=task,depth=s->depth;
    task=3;bridge_transport_cancel(t);task=saved_task;
    assert(socket_reads==before&&s->depth==depth&&s->owner==task);
}
static void lock_wait_event(void){
    bridge_transport_t*t=atomic_load(&chain);assert(t&&t->io->owner==2);
    unsigned before=socket_reads;task=3;bridge_transport_cancel(t);task=1;
    assert(socket_reads==before); /* zero-wait cancel cannot observe foreign FD */
}
void vTaskDelay(TickType_t ticks){
    assert(ticks==1);delays++;now+=(int64_t)ticks*1000000/configTICK_RATE_HZ;
    if(start_tx_on_delay){
        bridge_transport_t*t=atomic_load(&chain);
        assert(t&&!t->io->depth&&!t->io->owner&&!t->operation_deadline);
        start_tx_on_delay=false;
        scheduled_tx_at=now;
        unsigned saved_task=task;task=2;
        assert(esp_transport_write(t->ssl,"START",5,1000)==5);
        task=saved_task;schedule_tx++;
    }
}
static void check_io_owner(unsigned depth){
    bridge_transport_t*t=atomic_load(&chain);
    assert(t&&t->io&&t->io->owner==task&&t->io->depth==depth);
}
static void check_socket_owner(void){
    bridge_transport_t*t=atomic_load(&chain);
    assert(t&&t->io&&t->io->owner==task&&t->io->depth>0);
}
static void after_lock_take(SemaphoreHandle_t s){
    if(lock_action==LOCK_NONE||s->depth!=action_depth)return;
    bridge_transport_t*t=atomic_load(&chain);
    assert(t&&s==t->io&&task==1);
    enum lock_action action=lock_action;lock_action=LOCK_NONE;actions_fired++;
    if(action==LOCK_CANCEL){
        /* Cancellation is another task, not a magically recursive owner.
         * Its latch must win without observing a socket owned by active I/O. */
        unsigned before=socket_reads;task=2;bridge_transport_cancel(t);task=1;
        assert(socket_reads==before&&s->owner==1&&s->depth==action_depth);
    }else{
        /* SHRINK now targets the real enclosing caller deadline. The old
         * hard-minus-7ms oracle silently outlived the caller's 1000ms budget. */
        if(action==LOCK_EXPIRE)now=t->deadline-999;
        else{assert(t->operation_deadline);now=t->operation_deadline-7000;}
    }
}
static void check_exclusion(void){
    bridge_transport_t*t=atomic_load(&chain);assert(t&&t->io);
    unsigned depth=t->io->depth;
    int64_t operation=t->operation_deadline;bool operation_probe=t->operation_probe;
    assert(depth==1||depth==2);check_io_owner(depth);
    unsigned reads_before=raw_reads,writes_before=raw_writes,polls_before=polls;
    unsigned closes_before=closes,sockets_before=socket_reads,contention_before=contended_takes;
    char b[8];task=2;
    /* The pinned esp_transport.h defines its read timeout as zero. The
     * no-I/O census below, not a negative-only assumption, proves exclusion. */
    assert(esp_transport_read(t->ssl,b,sizeof(b),0)==ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT);
    assert(esp_transport_write(t->ssl,"FOREIGN",7,0)<=0);
    assert(esp_transport_poll_read(t->ssl,0)<=0);
    assert(esp_transport_poll_write(t->ssl,0)<=0);
    assert(esp_transport_close(t->ssl)<0);
    task=1;check_io_owner(depth);
    assert(t->operation_deadline==operation&&t->operation_probe==operation_probe);
    assert(raw_reads==reads_before&&raw_writes==writes_before&&polls==polls_before);
    assert(closes==closes_before&&socket_reads==sockets_before);
    assert(contended_takes==contention_before+5);exclusion_probes++;
}
static int ssl_poll(esp_transport_handle_t ssl,int ms,bool read){
    bridge_transport_t*t=atomic_load(&chain);assert(t&&ssl==t->ssl);
    unsigned depth=t->io->depth;assert(depth==1||depth==2);check_io_owner(depth);
    if(expected_operation_deadline&&depth==2)
        assert(t->operation_deadline==expected_operation_deadline);
    assert(ms>=0&&ms<=50);last_poll_ms=ms;polls++;
    if(read&&!t->operation_deadline&&(unsigned)ms>max_idle_poll_ms)max_idle_poll_ms=(unsigned)ms;
    if(depth==2){if(read)nested_read_polls++;else nested_write_polls++;}
    if(probe_exclusion)check_exclusion();
    int result;
    if(wire_enabled)result=wire_poll(ms,read);
    else{
        result=idle_read_only&&!read?1:base_poll_result;
        if(!result&&!immediate_zero)now+=(int64_t)ms*1000; /* sustained timeout consumes time */
    }
    if(result>0&&cancel_after_ready){
        cancel_after_ready=false;
        unsigned before=socket_reads;
        unsigned saved_task=task;task=3;bridge_transport_cancel(t);task=saved_task;
        assert(socket_reads==before);
    }
    if(result>0&&close_after_ready){close_after_ready=false;fixture_fd=-1;}
    return result;
}
static int wire_poll(int ms,bool read){
    /* select-like semantics: readiness at an absolute timestamp wins only if
     * it falls within the ACTUAL requested wait. An expired 50ms slice really
     * consumes 50ms, then returns 0; it never returns scripted future data.
     * This models readiness, not a real TLS stack or FreeRTOS scheduler. */
    assert(ms>=0&&ms<=50&&++wire_polls<=128);
    int64_t ready=read?wire_read_ready:wire_write_ready;
    if(read&&wire_rx_at==wire_rx_len)ready=INT64_MAX;
    int64_t until=now+(int64_t)ms*1000;
    if(ready<until&&ready>now)until=ready;
    if(ready<=now)until=now;
    if(wire_cancel_at&&wire_cancel_at<=until&&!wire_cancel_fired){
        now=wire_cancel_at;wire_cancel_fired=true;
        bridge_transport_t*t=atomic_load(&chain);
        unsigned sockets_before=socket_reads,depth=t->io->depth;
        task=2;bridge_transport_cancel(t);task=1;
        assert(socket_reads==sockets_before);check_io_owner(depth);
    }
    assert(until>=now);now=until;
    return ready<=now?1:0;
}
static int ssl_connect(esp_transport_handle_t t,const char*host,int port,int ms){
    (void)t;check_io_owner(1);assert(!strcmp(host,"192.168.1.50")&&port==9443&&ms>0&&ms<=5000);connects++;
    fixture_fd=12345;
    if(cancel_during_connect)bridge_transport_cancel(atomic_load(&chain));
    if(fail_connect)esp_transport_set_errors(t,&connect_error);
    return fail_connect?-1:0;
}
static setup_bridge_t cfg={.version=1,.enabled=1,.ip={192,168,1,50},.port=9443,.fqdn="pet.example.net"};
static void reset(void){
    assert(!atomic_load(&chain)&&!live_locks&&takes==gives&&task==1);
    now=1000000;clock_step_us=0;connects=auth_writes=raw_writes=polls=closes=0;
    raw_reads=nested_read_polls=nested_write_polls=error_handle_reads=socket_reads=0;
    takes=gives=max_depth=nonrecursive_rejections=contended_takes=0;
    tls_read_result=tls_write_result=INT_MAX;base_poll_result=1;last_poll_ms=-1;
    probe_exclusion=false;exclusion_probes=0;lock_action=LOCK_NONE;action_depth=actions_fired=0;
    status=101;fail_write=fail_connect=cancel_during_connect=false;location=true;gateway_refusal=NULL;
    memset(&connect_error,0,sizeof(connect_error));
    strcpy(redirect,"https://AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA@example.net/evil");
    muted_ws=muted_client=muted_base=bundle=false;response_at=0;response[0]=cn[0]=0;
    wire_enabled=wire_cancel_fired=false;wire_cancel_at=0;wire_polls=0;
    wire_tx_len=wire_rx_len=wire_rx_at=0;
    fixture_fd=12345;shutdowns=delays=schedule_tx=max_idle_poll_ms=0;release_lock_at=0;
    cancel_give_depth=cancel_give_fired=0;
    cancel_lock_at=expected_operation_deadline=scheduled_tx_at=0;
    start_tx_on_delay=cancel_after_ready=close_after_ready=idle_read_only=immediate_zero=false;
    write_script_len=write_script_at=expected_write_size=known_written=0;
    expected_write_base=NULL;stall_after_partial=false;
    memset(observed_write,0xa5,sizeof(observed_write));memset(last_plain_write,0,sizeof(last_plain_write));
}
static void fixture_power_cycle(bridge_transport_t*t){
    /* Historical name only: no reset of production's started latch. This I/O
     * fixture has NO SDK task. Manually publish its synthetic completion and
     * destroy its stub client, solely for allocation hygiene. Lifecycle proof
     * belongs to bridge_lifecycle_test.c, never to this forced stub completion. */
    assert(!t->io->depth&&!t->io->owner&&takes==gives);
    if(t->task_created){
        esp_websocket_client_destroy(t->client);
        atomic_store(&t->task_exited,true); /* SYNTHETIC, not SDK exit evidence */
    }
    assert(bridge_transport_dispose(t,now));
    assert(!atomic_load(&chain)&&!live_locks&&takes==gives);
}
static void fixture_mutex_selfcheck(void){
    SemaphoreHandle_t plain=xSemaphoreCreateMutex();
    assert(xSemaphoreTake(plain,0)==1&&xSemaphoreTake(plain,0)==0);
    assert(nonrecursive_rejections==1&&plain->depth==1&&plain->owner==1);
    task=2;assert(xSemaphoreTake(plain,0)==0);task=1;
    xSemaphoreGive(plain);vSemaphoreDelete(plain);
    SemaphoreHandle_t recursive=xSemaphoreCreateRecursiveMutex();
    assert(xSemaphoreTakeRecursive(recursive,0)==1);
    assert(xSemaphoreTakeRecursive(recursive,0)==1&&recursive->depth==2);
    task=2;assert(xSemaphoreTakeRecursive(recursive,0)==0);task=1;
    xSemaphoreGiveRecursive(recursive);
    assert(recursive->depth==1&&recursive->owner==1);
    task=2;assert(xSemaphoreTakeRecursive(recursive,0)==0);task=1;
    xSemaphoreGiveRecursive(recursive);
    assert(!recursive->depth&&!recursive->owner);
    task=2;assert(xSemaphoreTakeRecursive(recursive,0)==1);
    xSemaphoreGiveRecursive(recursive);task=1;vSemaphoreDelete(recursive);
    assert(!live_locks&&takes==gives);
}
static bridge_transport_t*new_fixture(void){
    reset();bridge_transport_t*t=bridge_transport_new(&cfg,T,61000000);assert(t);
    /* A repair must not bypass the bounded callbacks or replace actual SDK I/O
     * with a fixture algorithm to turn the original reentry RED into GREEN. */
    assert(t->read==ssl_read&&t->write==ssl_write);
    assert(t->ssl->_read==bounded_read&&t->ssl->_write==bounded_write);
    assert(t->ssl->_poll_read==bounded_poll_read&&t->ssl->_poll_write==bounded_poll_write);
    assert(t->poll_read==ssl_poll_read&&t->poll_write==ssl_poll_write);
    strcpy(response,"R");return t;
}
static int fixture_io(bridge_transport_t*t,bool read,int ms){
    char b[8]={0};
    int result=read?esp_transport_read(t->ssl,b,sizeof(b),ms):
                    esp_transport_write(t->ssl,"CANARY",6,ms);
    if(read&&result>0)assert(result==1&&b[0]=='R');
    return result;
}
static void assert_unlocked(bridge_transport_t*t){
    assert(!t->io->depth&&!t->io->owner&&takes==gives);
    assert(!t->operation_deadline&&!t->operation_probe);
}
static unsigned sdk_io_regressions(void){
    unsigned cases=0;
    fixture_mutex_selfcheck();
    bridge_transport_t*t=new_fixture();probe_exclusion=true;
    /* This is the intentional old-production RED: real ssl_write calls the
     * installed bounded_poll_write while bounded_write owns the mutex. */
    assert(fixture_io(t,false,1000)==6);
    assert(fixture_io(t,true,1000)==1);
    assert(nested_write_polls==1&&nested_read_polls==1&&polls==2);
    assert(raw_writes==1&&raw_reads==1&&max_depth==2&&!nonrecursive_rejections);
    /* Probe at nested depth 2 AND after its give at TLS depth 1: the inner give
     * must not expose the outer read/write to a competing close or I/O owner. */
    assert(exclusion_probes==4&&contended_takes==20);
    assert_unlocked(t);assert(esp_transport_close(t->ssl)==0&&closes==1);
    assert(!connects&&!auth_writes);fixture_power_cycle(t);cases++;

    const int requested[]={0,1,7,50,1000,-1};
    for(unsigned read=0;read<2;read++)for(size_t i=0;i<sizeof(requested)/sizeof(*requested);i++){
        t=new_fixture();int ms=requested[i];
        assert(fixture_io(t,read,ms)==(read?1:6));
        assert(last_poll_ms==(ms>=0&&ms<50?ms:50));
        assert(polls==1&&nested_read_polls+nested_write_polls==1);
        assert(raw_reads+raw_writes==1&&max_depth==2);
        assert_unlocked(t);fixture_power_cycle(t);cases++;
    }
    for(unsigned read=0;read<2;read++)for(int ready=0;ready>=-1;ready--){
        t=new_fixture();base_poll_result=ready;
        int expected=read?(ready==0?ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT:
                                   ERR_TCP_TRANSPORT_CONNECTION_FAILED):ready;
        assert(fixture_io(t,read,50)==expected);
        assert(polls==1&&nested_read_polls+nested_write_polls==1);
        assert(!raw_reads&&!raw_writes&&!error_handle_reads);
        assert_unlocked(t);fixture_power_cycle(t);cases++;
    }
    const int read_results[]={ESP_TLS_ERR_SSL_WANT_READ,ESP_TLS_ERR_SSL_TIMEOUT,-123,0};
    const int read_expected[]={ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT,
        ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT,-123,ERR_TCP_TRANSPORT_CONNECTION_CLOSED_BY_FIN};
    for(size_t i=0;i<sizeof(read_results)/sizeof(*read_results);i++){
        t=new_fixture();tls_read_result=read_results[i];
        assert(fixture_io(t,true,50)==read_expected[i]);
        assert(raw_reads==1&&nested_read_polls==1&&error_handle_reads==(i<3?1U:0U));
        assert_unlocked(t);fixture_power_cycle(t);cases++;
    }
    t=new_fixture();tls_write_result=-1;
    assert(fixture_io(t,false,50)==-1);
    assert(raw_writes==1&&nested_write_polls==1&&error_handle_reads==1);
    assert_unlocked(t);fixture_power_cycle(t);cases++;

    /* Events occur after acquisition, before the production budget() recheck,
     * at both outer I/O and inner SDK-poll levels. No synthetic poll error or
     * synthetic TLS failure is used to manufacture cancellation/deadline PASS.
     * This does NOT assert cancellation can undo already-admitted TLS I/O. */
    for(unsigned read=0;read<2;read++)for(unsigned depth=1;depth<=2;depth++)
        for(enum lock_action action=LOCK_CANCEL;action<=LOCK_EXPIRE;action++){
            t=new_fixture();lock_action=action;action_depth=depth;
            assert(fixture_io(t,read,1000)<0);
            assert(actions_fired==1&&lock_action==LOCK_NONE&&max_depth==depth);
            assert(atomic_load(&t->cancelled)==(action==LOCK_CANCEL));
            assert(!polls&&!raw_reads&&!raw_writes&&!bridge_transport_upgraded(t));
            assert(fixture_io(t,true,1000)<0&&fixture_io(t,false,1000)<0);
            assert(esp_transport_poll_read(t->ssl,1000)<0);
            assert(esp_transport_poll_write(t->ssl,1000)<0);
            assert(!polls&&!raw_reads&&!raw_writes);
            assert_unlocked(t);assert(esp_transport_close(t->ssl)==0);
            fixture_power_cycle(t);cases++;
        }
    for(unsigned read=0;read<2;read++){
        t=new_fixture();lock_action=LOCK_SHRINK;action_depth=2;
        assert(fixture_io(t,read,1000)==(read?1:6));
        assert(actions_fired==1&&lock_action==LOCK_NONE&&last_poll_ms==7);
        assert(polls==1&&raw_reads+raw_writes==1&&max_depth==2);
        assert_unlocked(t);fixture_power_cycle(t);cases++;
    }
    assert(cases==32);return cases;
}
enum wire_limit{WIRE_CALLER,WIRE_DEADLINE,WIRE_CANCEL};
struct wire_case{const char*name;enum wire_stall stall;int budget;enum wire_limit limit;bool success;};
static const struct wire_case wire_cases[]={
    {"tx-ready60",WIRE_TX_READY,1000,WIRE_CALLER,true},
    {"tx-payload-stall60",WIRE_TX_PAYLOAD,1000,WIRE_CALLER,true},
    /* Additional partial-write continuation oracle, not evidence that the
     * immediate short-write failure itself is caused by the 50ms clamp. */
    {"tx-partial-stall60",WIRE_TX_PARTIAL,1000,WIRE_CALLER,true},
    {"rx-header-gap60",WIRE_RX_HEADER,1000,WIRE_CALLER,true},
    {"tx-caller10",WIRE_TX_READY,10,WIRE_CALLER,false},
    {"rx-caller10",WIRE_RX_HEADER,10,WIRE_CALLER,false},
    {"tx-caller0",WIRE_TX_READY,0,WIRE_CALLER,false},
    {"rx-caller0",WIRE_RX_HEADER,0,WIRE_CALLER,false},
    {"tx-deadline20",WIRE_TX_READY,1000,WIRE_DEADLINE,false},
    {"rx-deadline20",WIRE_RX_HEADER,1000,WIRE_DEADLINE,false},
    {"tx-cancel20",WIRE_TX_READY,1000,WIRE_CANCEL,false},
    {"rx-cancel20",WIRE_RX_HEADER,1000,WIRE_CANCEL,false},
};
static void begin_wire(bridge_transport_t*t,const struct wire_case*c){
    /* Exercise the real SDK HTTP upgrade first. No post-upgrade data is
     * appended to that response: these cases do NOT test leftover HTTP bytes. */
    assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)==0);
    assert(bridge_transport_upgraded(t));assert_unlocked(t);
    raw_reads=raw_writes=polls=nested_read_polls=nested_write_polls=0;
    wire_enabled=true;wire_stall=c->stall;wire_started=now;
    wire_write_ready=now+(c->stall==WIRE_TX_READY?60000:0);
    wire_read_ready=now;
    wire_rx[wire_rx_len++]=0x82;wire_rx[wire_rx_len++]=sizeof(first_payload);
    memcpy(wire_rx+wire_rx_len,first_payload,sizeof(first_payload));wire_rx_len+=sizeof(first_payload);
    wire_rx[wire_rx_len++]=0x82;wire_rx[wire_rx_len++]=sizeof(second_payload);
    memcpy(wire_rx+wire_rx_len,second_payload,sizeof(second_payload));wire_rx_len+=sizeof(second_payload);
    if(c->limit==WIRE_DEADLINE)t->deadline=now+20000;
    if(c->limit==WIRE_CANCEL)wire_cancel_at=now+20000;
}
static int send_wire(bridge_transport_t*t,const unsigned char*bytes,size_t n,int ms){
    struct{uint64_t head;char bytes[32];uint64_t tail;} guarded={
        .head=UINT64_C(0x123456789abcdef0),.tail=UINT64_C(0xfedcba9876543210)};
    assert(n<=sizeof(guarded.bytes));
    memset(guarded.bytes,0xa5,sizeof(guarded.bytes));memcpy(guarded.bytes,bytes,n);
    /* Actual WS send_raw masks IN PLACE, so never pass read-only string data.
     * No retry of this WS call, even after an error or short payload result. */
    int result=esp_transport_ws_send_raw(t->ws,
        WS_TRANSPORT_OPCODES_BINARY|WS_TRANSPORT_OPCODES_FIN,guarded.bytes,(int)n,ms);
    assert(guarded.head==UINT64_C(0x123456789abcdef0)&&guarded.tail==UINT64_C(0xfedcba9876543210));
    for(size_t i=n;i<sizeof(guarded.bytes);i++)assert((unsigned char)guarded.bytes[i]==0xa5);
    return result;
}
static size_t check_masked_frame(size_t at,const unsigned char*payload,size_t n){
    assert(n<=125&&at+6+n<=wire_tx_len);
    assert(wire_tx[at]==0x82&&wire_tx[at+1]==(unsigned char)(0x80U|n));
    for(size_t i=0;i<n;i++)assert((wire_tx[at+6+i]^wire_tx[at+2+i%4])==payload[i]);
    return at+6+n;
}
static int run_wire_case(const struct wire_case*c){
    bridge_transport_t*t=new_fixture();begin_wire(t,c);
    const bool read=c->stall==WIRE_RX_HEADER;
    char received[32]={0};
    int result=read?esp_transport_read(t->ws,received,sizeof(received),c->budget):
        send_wire(t,first_payload,sizeof(first_payload),c->budget);
    int64_t elapsed=now-wire_started;
    /* Emit the observation BEFORE the intentional positive-contract assertion,
     * preserving the actual timeout/partial-byte result on an expected RED. */
    fprintf(stderr,"CASE %s result=%d elapsed_us=%lld polls=%u TLS_reads=%u TLS_writes=%u rx_bytes=%zu tx_bytes=%zu cancel=%d\n",
        c->name,result,(long long)elapsed,wire_polls,raw_reads,raw_writes,
        wire_rx_at,wire_tx_len,wire_cancel_fired);
    assert(wire_polls&&wire_polls<=128);assert_unlocked(t);
    if(c->success){
        /* Preserve the original RED's exact positive byte/time oracle. */
        assert(result==(int)sizeof(first_payload));
        assert(elapsed==60000&&elapsed<=(int64_t)c->budget*1000);
        if(read){
            assert(!memcmp(received,first_payload,sizeof(first_payload)));
            assert(esp_transport_ws_get_read_opcode(t->ws)==WS_TRANSPORT_OPCODES_BINARY);
            assert(esp_transport_ws_get_read_payload_len(t->ws)==(int)sizeof(first_payload));
            /* Transport-only fixture explicitly represents the SDK DATA
             * callback before issuing its next read. */
            bridge_transport_data_delivered(t);
            memset(received,0,sizeof(received));
            assert(esp_transport_read(t->ws,received,sizeof(received),c->budget)==(int)sizeof(second_payload));
            bridge_transport_data_delivered(t);
            assert(!memcmp(received,second_payload,sizeof(second_payload)));
            assert(wire_rx_at==wire_rx_len&&!wire_tx_len);
        }else{
            /* Decode the actual random mask and compare binary byte order.
             * Duplicate headers, repeated prefixes and whole-frame retries
             * all fail this exact two-frame length/order oracle. */
            assert(send_wire(t,second_payload,sizeof(second_payload),c->budget)==(int)sizeof(second_payload));
            size_t end=check_masked_frame(0,first_payload,sizeof(first_payload));
            end=check_masked_frame(end,second_payload,sizeof(second_payload));
            assert(end==wire_tx_len&&!wire_rx_at);
        }
    }else{
        /* These are expected operation failures (test PASS), not the positive
         * contract REDs. RX must consume only header byte 0; TX emits nothing. */
        assert(result<=0&&!wire_tx_len&&wire_rx_at==(read?1U:0U));
        if(c->limit==WIRE_CALLER)assert(elapsed==(int64_t)c->budget*1000);
        if(c->limit==WIRE_DEADLINE)assert(elapsed==20000);
        if(c->limit==WIRE_CANCEL){
            assert(wire_cancel_fired&&atomic_load(&t->cancelled));
            assert(elapsed>=20000&&elapsed<=50000);
        }
        if(c->limit!=WIRE_CALLER){
            /* Later readiness cannot revive a cancelled/expired chain. Do not
             * retry WS here: its failed partial header is not recoverable state. */
            unsigned before_reads=raw_reads,before_writes=raw_writes,before_polls=polls;
            now+=100000;
            assert(esp_transport_read(t->ssl,received,sizeof(received),1000)<0);
            assert(esp_transport_write(t->ssl,"LATE",4,1000)<0);
            assert(raw_reads==before_reads&&raw_writes==before_writes&&polls==before_polls);
        }
    }
    assert_unlocked(t);fixture_power_cycle(t);
    printf("PASS CASE %s: exact SDK WS/SSL bodies over timed synthetic readiness/bytes; full client task/abort policy and real TLS NOT tested.\n",c->name);
    return 0;
}
/* Each named case is a separate executable invocation. The lock/scheduler seams
 * model actual contention and time consumption, not always-success mutexes.
 * These are deterministic interleavings, NOT real scheduler qualification. */
static const char *const extra_cases[]={
    "idle-rx-start-tx","nested-tls-exclusion","nested-deadline-restore",
    "lock-wait60","lock-caller20","lock-caller0","lock-cancel20",
    "cancel-after-readiness","closed-fd","closed-after-readiness",
    "poll-negative-no-retry","timeout-total-budget","negative-budget-hard",
    "zero-probe","partial-error-no-retry","partial-fixed-deadline",
    "immediate-zero-progress","subtick-lock-budget",
    "zero-probe-clock-progress",
    "cancel-final-release","cancel-nested-release","cancel-release-invalidfd",
    "nested-short-request","ws-observation-isolation",
};
static unsigned observation_mode;
static int fixture_observed_ws_read(esp_transport_handle_t ws,char *b,int n,int ms){
    bridge_transport_t *t=atomic_load(&chain);
    assert(t&&ws==t->ws&&n>0&&ms>0&&task==1);
    if(observation_mode>=3){
        if(observation_mode==3){control_probe_target=t;probe_control_during_tls=true;}
        assert(esp_transport_read(t->ssl,b,n,1000)==1);
        task=2;assert(bridge_transport_admit_control(t,now+1000));task=1;
        return 1;
    }
    /* Another task's write during an active parser is not ingress. A foreign
     * plaintext read of the same socket is different: it steals input and
     * must fail closed, not be attributed to the parser or ignored. */
    task=2;assert(esp_transport_write(t->ssl,"START",5,1000)==5);
    if(observation_mode==1)assert(esp_transport_read(t->ssl,b,n,1000)==1);
    task=1;
    if(observation_mode==2)assert(esp_transport_read(t->ssl,b,n,1000)==1);
    return -1;
}
static int guarded_operation(bridge_transport_t*t,unsigned op,int ms){
    struct{uint64_t head;char bytes[8];uint64_t tail;} b={
        .head=UINT64_C(0x1020304050607080),.tail=UINT64_C(0x8070605040302010)};
    memset(b.bytes,0xa5,sizeof(b.bytes));
    int result;
    if(op==0)result=esp_transport_read(t->ssl,b.bytes,sizeof(b.bytes),ms);
    else if(op==1)result=esp_transport_write(t->ssl,"CANARY",6,ms);
    else if(op==2)result=esp_transport_poll_read(t->ssl,ms);
    else{assert(op==3);result=esp_transport_poll_write(t->ssl,ms);}
    assert(b.head==UINT64_C(0x1020304050607080)&&b.tail==UINT64_C(0x8070605040302010));
    size_t changed=op==0&&result>0?1:0;
    if(changed)assert(result==1&&b.bytes[0]=='R');
    for(size_t i=changed;i<sizeof(b.bytes);i++)assert((unsigned char)b.bytes[i]==0xa5);
    return result;
}
static int run_extra_case(size_t which){
    bridge_transport_t*t;
    if(which==0){
        t=new_fixture();base_poll_result=0;idle_read_only=true;start_tx_on_delay=true;
        int64_t entry=now;
        assert(esp_transport_poll_read(t->ssl,120)==0);
        assert(now-entry==120000&&schedule_tx==1&&!start_tx_on_delay);
        printf("IDLE RX/TX: first sender admitted after %lld us; idle poll held shared TLS mutex for up to %u ms\n",
               (long long)(scheduled_tx_at-entry),max_idle_poll_ms);
        fflush(stdout);
        /* The idle receiver must not sleep inside the shared TLS mutex.
         * WS preflight, header and payload each need that same mutex. */
        assert(scheduled_tx_at-entry<=1000000/configTICK_RATE_HZ&&max_idle_poll_ms==0);
        assert(raw_writes==1&&!raw_reads&&delays>=1&&max_depth==2);
        assert(!memcmp(last_plain_write,"START",5));
        assert_unlocked(t);fixture_power_cycle(t);
    }else if(which==1){
        t=new_fixture();probe_exclusion=true;
        assert(guarded_operation(t,1,1000)==6&&guarded_operation(t,0,1000)==1);
        assert(exclusion_probes==4&&contended_takes==20&&max_depth==2);
        assert(raw_reads==1&&raw_writes==1&&nested_read_polls==1&&nested_write_polls==1);
        assert_unlocked(t);fixture_power_cycle(t);
    }else if(which==2||which==11){
        for(unsigned op=0;op<(which==2?1U:4U);op++){
            t=new_fixture();base_poll_result=0;
            int64_t entry=now;expected_operation_deadline=entry+120000;
            int result=guarded_operation(t,op,120);
            assert(result==(op==0?ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT:0));
            assert(now-entry==120000&&!raw_reads&&!raw_writes);
            /* Idle RX now probes without waiting under io and sleeps one tick
             * outside it. Other/nested polls retain their 50ms slices. */
            assert(polls==(op==2?120U*configTICK_RATE_HZ/1000:3U));
            if(op<2)assert(nested_read_polls+nested_write_polls==3&&delays==0);
            assert_unlocked(t);
            /* A fresh call must not inherit an expired previous scope. */
            expected_operation_deadline=now+7000;base_poll_result=1;
            assert(guarded_operation(t,0,7)==1&&last_poll_ms==7);
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }else if(which>=3&&which<=6){
        for(unsigned op=0;op<4;op++){
            t=new_fixture();
            task=2;assert(xSemaphoreTakeRecursive(t->io,0)==pdTRUE);task=1;
            int64_t entry=now;int ms=which==4?20:which==5?0:1000;
            if(which==3)release_lock_at=entry+60000;
            if(which==6)cancel_lock_at=entry+20000;
            expected_operation_deadline=entry+(int64_t)ms*1000;
            int result=guarded_operation(t,op,ms);
            if(which==3){
                assert(result==(op==0?1:op==1?6:1));
                assert(now-entry==60000&&contended_takes==2);
            }else{
                assert(result==(which==6?-1:op==0?ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT:0));
                assert(now-entry==(which==4?20000:which==5?0:50000));
                assert(!raw_reads&&!raw_writes&&!polls&&!shutdowns);
                /* Busy owner, not waiting caller, services deferred shutdown. */
                assert(t->io->owner==2&&t->io->depth==1);
                task=2;give_io(t);task=1;
                assert(shutdowns==(which==6?1U:0U));
            }
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }else if(which==7||which==8||which==9){
        for(unsigned op=0;op<4;op++){
            t=new_fixture();
            if(which==7)cancel_after_ready=true;
            if(which==8){assert(esp_transport_close(t->ssl)==0);assert(fixture_fd==-1);}
            if(which==9)close_after_ready=true;
            assert(guarded_operation(t,op,1000)<0);
            assert(!raw_reads&&!raw_writes&&polls==(which==8?0U:1U));
            if(which==7)assert(shutdowns==1&&t->shutdown_done&&atomic_load(&t->cancelled));
            else{
                /* The old numeric identifier may now belong to somebody else.
                 * get_socket is invalid after vendor close: no poll/shutdown
                 * may use a cached numeric FD. Nothing touches a real host FD. */
                bridge_transport_cancel(t);assert(!shutdowns&&fixture_fd==-1);
            }
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }else if(which==10){
        for(unsigned op=0;op<4;op++)for(int error=-1;error>=-17;error-=16){
            t=new_fixture();base_poll_result=error;
            int result=guarded_operation(t,op,1000);
            assert(result==(op==0?ERR_TCP_TRANSPORT_CONNECTION_FAILED:-1));
            assert(polls==1&&!raw_reads&&!raw_writes&&!delays);
            assert_unlocked(t);fixture_power_cycle(t);
        }
        t=new_fixture();base_poll_result=7;
        assert(esp_transport_poll_read(t->ssl,1000)==7);
        assert(polls==1&&!raw_reads&&!raw_writes);
        assert_unlocked(t);fixture_power_cycle(t);
    }else if(which==12){
        for(unsigned op=0;op<4;op++){
            t=new_fixture();base_poll_result=0;t->deadline=now+20000;
            int64_t entry=now;expected_operation_deadline=t->deadline;
            assert(guarded_operation(t,op,-1)<0);
            assert(now-entry==20000&&polls==(op==2?20U*configTICK_RATE_HZ/1000:1U)&&!raw_reads&&!raw_writes);
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }else if(which==13){
        for(unsigned op=0;op<4;op++)for(int ready=0;ready<2;ready++){
            t=new_fixture();base_poll_result=ready;int64_t entry=now;
            int result=guarded_operation(t,op,0);
            assert(result==(ready?(op==1?6:1):(op==0?ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT:0)));
            assert(now==entry&&polls==1&&!delays&&last_poll_ms==0);
            assert(raw_reads==(op==0&&ready?1U:0U)&&raw_writes==(op==1&&ready?1U:0U));
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }else if(which==16){
        for(unsigned op=0;op<4;op++){
            t=new_fixture();base_poll_result=0;immediate_zero=true;int64_t entry=now;
            expected_operation_deadline=entry+10000;
            assert(guarded_operation(t,op,10)==(op==0?ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT:0));
            assert(now-entry==10000&&!raw_reads&&!raw_writes);
            assert(polls==(configTICK_RATE_HZ==1000?10U:1U)&&delays==polls);
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }else if(which==17){
        for(unsigned op=0;op<4;op++){
            t=new_fixture();
            task=2;assert(xSemaphoreTakeRecursive(t->io,0)==pdTRUE);task=1;
            int64_t entry=now;
            assert(guarded_operation(t,op,7)==(op==0?ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT:0));
            assert(now-entry==(configTICK_RATE_HZ==1000?7000:10000));
            assert(contended_takes==1&&!polls&&!raw_reads&&!raw_writes&&!delays);
            task=2;give_io(t);task=1;
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }else if(which==18){
        for(unsigned op=0;op<4;op++)for(int ready=0;ready<2;ready++){
            t=new_fixture();base_poll_result=ready;clock_step_us=1;int64_t entry=now;
            int result=guarded_operation(t,op,0);
            assert(result==(ready?(op==1?6:1):(op==0?ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT:0)));
            /* Real esp_timer queries consume CPU time even for a zero budget.
             * That must not erase the sole nonblocking readiness probe. */
            assert(polls==1&&last_poll_ms==0&&!delays&&!contended_takes);
            assert(now>entry&&now-entry<1000);
            clock_step_us=0;assert_unlocked(t);fixture_power_cycle(t);
        }
    }else if(which>=19&&which<=21){
        t=new_fixture();
        unsigned depth=which==20?2U:1U;
        for(unsigned i=0;i<depth;i++)assert(xSemaphoreTakeRecursive(t->io,0)==pdTRUE);
        if(which==21)fixture_fd=-1;
        cancel_give_depth=depth;
        give_io(t);
        assert(cancel_give_fired==1&&atomic_load(&t->cancelled));
        assert(shutdowns==(which==21?0U:1U));
        assert(t->io->depth==depth-1&&t->io->owner==(depth==2?task:0));
        if(depth==2)give_io(t);
        unsigned before=shutdowns;
        bridge_transport_cancel(t);bridge_transport_cancel(t);
        assert(shutdowns==before&&!raw_reads&&!raw_writes&&!polls);
        assert_unlocked(t);fixture_power_cycle(t);
    }else if(which==22){
        for(unsigned op=2;op<4;op++)for(unsigned zero=0;zero<2;zero++){
            t=new_fixture();base_poll_result=0;
            assert(xSemaphoreTakeRecursive(t->io,0)==pdTRUE);
            int64_t entry=now,outer=entry+100000;
            t->operation_deadline=outer;t->operation_probe=false;
            assert(guarded_operation(t,op,zero?0:7)==0);
            assert(now-entry==(zero?0:7000)&&polls==1);
            assert(last_poll_ms==(zero?0:7));
            assert(t->operation_deadline==outer&&!t->operation_probe&&t->io->depth==1);
            t->operation_deadline=0;give_io(t);
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }else if(which==23){
        for(observation_mode=0;observation_mode<3;observation_mode++){
            t=new_fixture();t->ws_read=fixture_observed_ws_read;
            char data[8];
            assert(complete_control_read(t->ws,data,sizeof(data),1000)<0);
            assert(!t->ws_read_active&&!t->ws_read_ingress&&!t->ws_read_stalled);
            assert(atomic_load(&t->data_lost)==(observation_mode!=0));
            assert(atomic_load(&t->cancelled)==(observation_mode!=0));
            assert(raw_writes==1&&raw_reads==(observation_mode?1U:0U));
            bridge_transport_terminal_t outcome=BR_TRANSPORT_PROTOCOL;
            assert_unlocked(t);assert(bridge_transport_dispose_result(t,now,&outcome));
            assert(outcome==(observation_mode?BR_TRANSPORT_EVENT_LOSS:BR_TRANSPORT_NONE));
            assert(!atomic_load(&chain)&&!live_locks&&takes==gives);
        }
        /* Parser ingress and control admission are ordered even if the
         * latter races after the TLS read returned but before observation. */
        for(observation_mode=3;observation_mode<=4;observation_mode++){
            t=new_fixture();t->ws_read=fixture_observed_ws_read;
            if(observation_mode==4)assert(bridge_transport_admit_control(t,now+1000));
            char data[8];ws_read_observation_t observed={0};
            assert(observed_ws_read(t,t->ws,data,sizeof(data),1000,&observed)==1);
            assert(observed.ingress&&observed.after_control==(observation_mode==4));
            assert(t->local_control_admitted&&!t->ws_read_active);
            if(observation_mode==3)assert(control_probe_denied&&!probe_control_during_tls);
            control_probe_target=NULL;control_probe_denied=false;
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }else{
        assert(which==14||which==15);
        const int scripts[][2]={{3,3},{3,0},{3,-123},{0,0},{-123,0},{7,0}};
        const int results[]={6,0,-123,0,-123,-1};
        size_t count=which==15?1:sizeof(scripts)/sizeof(*scripts);
        for(size_t i=0;i<count;i++){
            t=new_fixture();
            struct{uint64_t head;char bytes[8];uint64_t tail;} b={
                .head=UINT64_C(0xabcdef0123456789),.bytes={'C','A','N','A','R','Y'},
                .tail=UINT64_C(0x9876543210fedcba)};
            b.bytes[6]=b.bytes[7]=(char)0xa5;
            expected_write_base=b.bytes;expected_write_size=6;
            memcpy(write_script,scripts[i],sizeof(scripts[i]));write_script_len=i<3?2:1;
            int64_t entry=now;int ms=which==15?10:1000;
            expected_operation_deadline=entry+(int64_t)ms*1000;
            stall_after_partial=which==15;
            int result=esp_transport_write(t->ssl,b.bytes,6,ms);
            if(which==15){
                assert(result==0&&now-entry==10000&&last_poll_ms==3);
                assert(write_script_at==1&&raw_writes==1&&polls==2&&known_written==3);
            }else{
                assert(result==results[i]&&write_script_at==write_script_len);
                assert(raw_writes==write_script_len&&polls==write_script_len&&now==entry);
                assert(known_written==(i==0?6U:i<3?3U:0U));
            }
            assert(!memcmp(observed_write,"CANARY",known_written));
            for(size_t j=known_written;j<sizeof(observed_write);j++)assert(observed_write[j]==0xa5);
            assert(b.head==UINT64_C(0xabcdef0123456789)&&b.tail==UINT64_C(0x9876543210fedcba));
            assert(!memcmp(b.bytes,"CANARY",6));
            assert((unsigned char)b.bytes[6]==0xa5&&(unsigned char)b.bytes[7]==0xa5);
            assert_unlocked(t);fixture_power_cycle(t);
        }
    }
    printf("PASS CASE %s: exact SDK SSL bodies, synthetic clock/locks/TLS; no live or full-task qualification.\n",extra_cases[which]);
    return 0;
}
static int cert_connect(void){
    /* Exact hash-pinned SDK ssl_connect + esp_transport_set_errors copy
     * synthetic TLS errors to the shared foundation before destroying TLS.
     * The real WS connect and app adapter run; handshake itself is synthetic. */
    static const struct {int flags,esp,stack,expected;} cases[]={
        {MBEDTLS_X509_BADCERT_CN_MISMATCH,ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED,1,BR_TRANSPORT_CERT_VERIFY},
        {MBEDTLS_X509_BADCERT_NOT_TRUSTED|MBEDTLS_X509_BADCERT_EXPIRED,ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED,1,BR_TRANSPORT_CERT_VERIFY},
        {-1,ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED,1,BR_TRANSPORT_TLS_UNKNOWN},
        {0,ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED,1,BR_TRANSPORT_TLS_UNKNOWN},
        {0,0,0,BR_TRANSPORT_NONE},
        {0,ESP_ERR_ESP_TLS_FAILED_CONNECT_TO_HOST,0,BR_TRANSPORT_NONE},
        {0,ESP_ERR_ESP_TLS_CANNOT_RESOLVE_HOSTNAME,0,BR_TRANSPORT_NONE},
        {0,ESP_ERR_ESP_TLS_CONNECTION_TIMEOUT,0,BR_TRANSPORT_NONE},
        {0,ESP_ERR_ESP_TLS_TCP_CLOSED_FIN,0,BR_TRANSPORT_NONE},
        {0,0,1,BR_TRANSPORT_TLS_UNKNOWN},
        {0,ESP_ERR_NO_MEM,0,BR_TRANSPORT_TLS_UNKNOWN},
        {MBEDTLS_X509_BADCERT_SKIP_VERIFY,ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED,1,BR_TRANSPORT_TLS_UNKNOWN},
        {(int)UINT32_C(0x80000000),ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED,1,BR_TRANSPORT_TLS_UNKNOWN},
        {MBEDTLS_X509_BADCERT_SKIP_VERIFY|MBEDTLS_X509_BADCERT_EXPIRED,ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED,1,BR_TRANSPORT_TLS_UNKNOWN},
    };
    for(size_t i=0;i<sizeof(cases)/sizeof(*cases);i++){
        bridge_transport_t *t=new_fixture();fail_connect=true;
        t->ssl_connect=pinned_ssl_connect;
        connect_error=(esp_tls_last_error_t){.last_error=cases[i].esp,.esp_tls_error_code=cases[i].stack,.esp_tls_flags=cases[i].flags};
        assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)<0);
        assert(connects==1&&auth_writes==0&&!bridge_transport_upgraded(t)&&bridge_transport_http_status(t)==0);
        /* The adapter must NOT consume the SDK's error evidence. */
        esp_tls_error_handle_t h=esp_transport_get_error_handle(t->ws);
        assert(h&&h->last_error==cases[i].esp&&h->esp_tls_error_code==cases[i].stack&&h->esp_tls_flags==cases[i].flags);
        bridge_transport_terminal_t outcome=BR_TRANSPORT_PROTOCOL;
        assert(bridge_transport_dispose_result(t,now,&outcome));
        fprintf(stderr,"cert-connect case %zu observed=%d expected=%d\n",i,(int)outcome,cases[i].expected);
        assert((int)outcome==cases[i].expected);
    }
    bridge_transport_t *t=new_fixture();
    esp_transport_error_storage *saved=t->ssl->foundation->error_handle;
    assert(saved);t->ssl->foundation->error_handle=NULL;
    note_connect_error(t,t->ssl);
    t->ssl->foundation->error_handle=saved;
    bridge_transport_terminal_t outcome=BR_TRANSPORT_NONE;
    assert(bridge_transport_dispose_result(t,now,&outcome));
    assert(outcome==BR_TRANSPORT_TLS_UNKNOWN);
    puts("PASS cert-connect: SDK error copy + real WS + adapter, synthetic TLS");
    return 0;
}
static int cert_positive(const char *value){
    char *end=NULL;unsigned long flags=strtoul(value,&end,0);
    assert(end&&!*end&&flags>0&&flags<UINT32_MAX);
    bridge_transport_t *t=new_fixture();fail_connect=true;t->ssl_connect=pinned_ssl_connect;
    connect_error=(esp_tls_last_error_t){.last_error=ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED,
        .esp_tls_error_code=1,.esp_tls_flags=(int)flags};
    assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)<0);
    assert(connects==1&&!auth_writes&&!bridge_transport_upgraded(t)&&!bridge_transport_http_status(t));
    bridge_transport_terminal_t outcome=BR_TRANSPORT_NONE;
    assert(bridge_transport_dispose_result(t,now,&outcome));
    printf("CERT_TERMINAL=%d\n",(int)outcome);
    assert(outcome==BR_TRANSPORT_CERT_VERIFY);
    return 0;
}
#if defined(PET_PRIVATE_MIC_DIAG)
static int bench_counters(void){
    bridge_transport_t *t=new_fixture();pet_bench_transport_t result;
    task=2;assert(xSemaphoreTakeRecursive(t->io,0)==pdTRUE);task=1;
    release_lock_at=now+60000;
    assert(fixture_io(t,false,1000)==6);
    assert(bench_io[0].write_wait_count==1&&bench_io[0].write_wait_us_total==60000);
    assert(bench_io[0].write_wait_us_max==60000&&!bench_io[0].write_us_total);
    fixture_power_cycle(t);bridge_transport_bench(&result);
    assert(result.io.write_wait_us_total==60000);
    t=new_fixture(); /* constructor reset */
    assert(!bench_io[0].write_wait_count);
    base_poll_result=0;
    assert(fixture_io(t,false,12)==0);
    assert(bench_io[0].write_us_total==12000&&bench_io[0].write_us_max==12000);
    assert(!bench_io[0].idle_read_polls); /* nested write polling not idle */
    assert(fixture_io(t,true,7)==ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT);
    assert(bench_io[0].read_hold_count==1&&bench_io[0].read_hold_us_total==7000);
    assert(bench_io[0].read_hold_us_max==7000&&!bench_io[0].idle_read_polls);
    assert(esp_transport_poll_read(t->ssl,3)==0);
    assert(bench_io[0].idle_read_polls==3);
    bench_sdk=(TaskHandle_t)(uintptr_t)2;task=2;
    assert(fixture_io(t,true,4)==ERR_TCP_TRANSPORT_CONNECTION_TIMEOUT);
    task=1;
    assert(bench_io[1].read_hold_us_total==4000&&bench_io[1].read_hold_count==1);
    fixture_power_cycle(t);bridge_transport_bench(&result);
    assert(result.io.read_hold_us_total==11000&&result.io.read_hold_us_max==7000);
    assert(result.io.read_hold_count==2&&result.io.idle_read_polls==3);
    puts("PASS bench-counters (synthetic clock/RTOS/TLS, actual transport wrappers)");
    return 0;
}
#endif
int main(int argc,char**argv){
#if defined(PET_PRIVATE_MIC_DIAG)
    if(argc==2&&!strcmp(argv[1],"bench-counters"))return bench_counters();
#endif
    /* Default/legacy remains the original 32+110 suite. Run every positive RED
     * as a separate process/CASE so one assertion cannot mask the others. */
    if(argc==3&&!strcmp(argv[1],"cert-positive"))return cert_positive(argv[2]);
    if(argc==2&&strcmp(argv[1],"legacy")){
        if(!strcmp(argv[1],"cert-connect"))return cert_connect();
        for(size_t i=0;i<sizeof(wire_cases)/sizeof(*wire_cases);i++)
            if(!strcmp(argv[1],wire_cases[i].name))return run_wire_case(&wire_cases[i]);
        for(size_t i=0;i<sizeof(extra_cases)/sizeof(*extra_cases);i++)
            if(!strcmp(argv[1],extra_cases[i]))return run_extra_case(i);
        fprintf(stderr,"Unknown CASE: %s\n",argv[1]);return 2;
    }
    if(argc>2){fprintf(stderr,"Usage: %s [legacy|CASE]\n",argv[0]);return 2;}
    unsigned io_cases=sdk_io_regressions();
    unsigned cases=0;
    const int statuses[]={101,200,300,301,302,303,304,307,308,399,401,403,409,429,503};
    const char *dest[]={"/relative","https://pet.example.net/x","https://foreign.example/x","http://192.168.1.50/x","https://user:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA@foreign.example/x"};
    for(size_t i=0;i<sizeof(statuses)/sizeof(*statuses);i++)for(unsigned no=0;no<2;no++)for(size_t d=0;d<sizeof(dest)/sizeof(*dest);d++){
        reset();status=statuses[i];location=!no;strcpy(redirect,dest[d]);bridge_transport_t*t=bridge_transport_new(&cfg,T,61000000);assert(t);
        assert(t->ssl->_get_socket==socket_fn&&t->ssl->foundation==t->ws->foundation);
        int r=esp_transport_connect(t->ws,"192.168.1.50",9443,5000);
        assert((r==0)==(status==101));assert(connects==1&&auth_writes==1);
        assert(bridge_transport_upgraded(t)==(status==101));
        /* The host's answer is reported as a number, whatever it was; a
         * second, refused connect neither repeats the request nor changes it. */
        assert(bridge_transport_http_status(t)==status);
        assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)<0&&connects==1);
        assert(bridge_transport_http_status(t)==status);
        assert(esp_transport_connect_async(t->ssl,"192.168.1.50",9443,100)<0&&connects==1);
        fixture_power_cycle(t);cases++;
    }
    /* Byte-shaped refusals: the device learns the number (busy 409 is retried
     * by the caller, 401 is not), and the one connection attempt is spent.
     * 423/uncertain_unresolved is compatibility input only: the gateway fence
     * semantics are not frozen, so this does not ratify a product meaning. */
    static const struct {int status;const char *code;} refusals[]={{409,"busy"},{401,"unauthorized"},
        {429,"rate_limited"},{404,"not_found"},{400,"bad_request"},{421,"misdirected"},{405,"method_not_allowed"},
        {423,"uncertain_unresolved"}};
    unsigned refused=0;
    for(size_t i=0;i<sizeof(refusals)/sizeof(*refusals);i++){
        reset();status=refusals[i].status;gateway_refusal=refusals[i].code;
        bridge_transport_t*r=bridge_transport_new(&cfg,T,61000000);assert(r);
        assert(bridge_transport_http_status(r)==0);
        assert(esp_transport_connect(r->ws,"192.168.1.50",9443,5000)<0&&connects==1&&auth_writes==1);
        assert(!bridge_transport_upgraded(r)&&bridge_transport_http_status(r)==refusals[i].status);
        if(status==423){
            /* Real HTTP bytes reach the pinned SDK HTTP/WS parser, not an
             * injected status getter. No upgrade or redirect headers exist. */
            assert(!strcmp(response,"HTTP/1.1 423 uncertain_unresolved\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                "Connection: close\r\nContent-Length: 32\r\n\r\n"
                "{\"error\":\"uncertain_unresolved\"}"));
            assert(!strstr(response,"Sec-WebSocket-Accept:")&&!strstr(response,"Location:"));
        }
        assert(esp_transport_connect(r->ws,"192.168.1.50",9443,5000)<0&&connects==1&&auth_writes==1);
        assert(esp_transport_connect_async(r->ssl,"192.168.1.50",9443,100)<0&&connects==1&&auth_writes==1);
        assert(!bridge_transport_upgraded(r)&&bridge_transport_http_status(r)==refusals[i].status&&leaked_logs==0);
        fixture_power_cycle(r);refused++;
    }
    assert(refused==8);
    reset();bridge_transport_t*t=bridge_transport_new(&cfg,T,61000000);assert(t);
    assert(esp_transport_connect(t->ws,"foreign.example",9443,5000)<0&&connects==0);
    assert(esp_transport_connect(t->ws,"192.168.1.50",9444,5000)<0&&connects==0);
    assert(bridge_transport_http_status(t)==0&&bridge_transport_http_status(NULL)==0);
    fail_connect=true;assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)<0&&connects==1&&auth_writes==0);
    assert(bridge_transport_http_status(t)==0); /* no answer was read: unreachable, not refused */
    fail_connect=false;assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)<0&&connects==1);fixture_power_cycle(t);
    reset();t=bridge_transport_new(&cfg,T,61000000);assert(t);fail_write=true;
    assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)<0);assert(leaked_logs==0);fixture_power_cycle(t);
    reset();t=bridge_transport_new(&cfg,T,61000000);assert(t);cancel_during_connect=true;
    assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)<0&&connects==1&&auth_writes==0);fixture_power_cycle(t);
    reset();t=bridge_transport_new(&cfg,T,61000000);assert(t);assert(bridge_transport_start(t)==ESP_OK);
    unsigned destroyed=client_destroys;bridge_transport_dispose_unstarted(t);assert(client_destroys==destroyed);
    assert(t->cutoff->us==60000000&&bridge_transport_start(t)!=ESP_OK);
    assert(esp_transport_connect(t->ws,"192.168.1.50",9443,5000)==0);
    unsigned writes_before=raw_writes;now=61000000;t->cutoff->args.callback(t->cutoff->args.arg);
    assert(t->cancelled&&!bridge_transport_upgraded(t));
    assert(esp_transport_write(t->ssl,"CANARY",6,1000)<0&&raw_writes==writes_before);
    assert(esp_transport_poll_write(t->ssl,1000)<0);
    destroyed=client_destroys;bridge_transport_dispose_unstarted(t);
    assert(client_destroys==destroyed&&atomic_load(&chain)==t&&live_locks==1);
    assert(!bridge_transport_new(&cfg,T,121000000));fixture_power_cycle(t);
    assert(cases==150&&leaked_logs==0);
    printf("PASS exact SDK transport+WS/SSL-I/O/adapter: %u SDK-I/O cases + %u HTTP/Location cases + %u byte-shaped refusals + literal fence/failure burn/async denial/cancel-upgrade/deadline-I/O/retention/log canaries. Complete client-task, scheduler timing and real TLS NOT tested.\n",io_cases,cases,refused);
}
