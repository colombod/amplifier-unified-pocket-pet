/* Real tailtiny_init(_checked), do_register, ml_h2 and X25519. The generated
 * include contains exact source bodies and source locations, with hashes in
 * every log. No duplicate constructor, enrollment or persistence implementation. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "tailtiny.h"
#include "ml_policy.h"
#include "cJSON.h"
#include "x25519.h"

typedef int nvs_handle_t;
#define ESP_ERR_NVS_NOT_FOUND 4
#define NVS_READWRITE 1
#define NVS_READONLY 0
static unsigned char disk[512], staged[512];
static size_t disk_len, staged_len;
static unsigned opens, writes, generated, sends, auth_sends, copies, legacy_reads;
static unsigned wiped_frees,alloc_count,alloc_fail_at;
static bool alloc_count_on;
static int fail_open, fail_read, fail_write, fail_commit, fail_reopen, bad_readback;
static bool legacy, copy_fail, no_wire, ambiguous = true;
static int nonsecret_handle;
static esp_err_t nvs_open(const char *ns, int mode, nvs_handle_t *h) {
    assert(!strcmp(ns, "microlink")); (void)mode; opens++;
    if(fail_open || (fail_reopen && writes))return ESP_FAIL;
    *h=1; return ESP_OK;
}
static void nvs_close(nvs_handle_t h) {(void)h;}
static esp_err_t nvs_get_blob(nvs_handle_t h,const char *key,void *p,size_t *n) {
    (void)h;
    if(fail_read)return ESP_FAIL;
    if(strcmp(key,"identity_v1"))return legacy?ESP_ERR_INVALID_STATE:ESP_ERR_NVS_NOT_FOUND;
    if(!disk_len)return ESP_ERR_NVS_NOT_FOUND;
    if(*n<disk_len)return ESP_ERR_INVALID_SIZE;
    if(p){memcpy(p,disk,disk_len);if(bad_readback && writes)((uint8_t *)p)[0]^=1;}
    *n=disk_len;return ESP_OK;
}
static esp_err_t nvs_set_blob(nvs_handle_t h,const char *key,const void *p,size_t n) {
    (void)h;assert(!strcmp(key,"identity_v1")); writes++;
    if(fail_write)return ESP_FAIL;
    assert(n<=sizeof(staged));memcpy(staged,p,n);staged_len=n;return ESP_OK;
}
static esp_err_t nvs_commit(nvs_handle_t h) {
    (void)h;if(fail_commit)return ESP_FAIL;
    memcpy(disk,staged,staged_len);disk_len=staged_len;return ESP_OK;
}
static void esp_fill_random(void *p,size_t n) {
    generated++;for(size_t i=0;i<n;i++)((uint8_t *)p)[i]=(uint8_t)(i+generated);
}
#include "ml_identity.h"
#include "ml_registration.h"

/* No fake auth decisions here. All state below is storage for production code,
 * matching runtime fields; opaque peripherals are inert non-owning handles. */
typedef unsigned EventBits_t;
typedef void *QueueHandle_t;
typedef struct {uint8_t *data;} ml_derp_tx_item_t;
typedef struct {uint8_t *data;} ml_rx_packet_t;
typedef int ml_coord_cmd_t;
typedef struct {int unused;} ml_peer_update_t;
typedef struct {int unused;} ml_noise_state_t;
struct tailtiny_s {
    tailtiny_config_t config;
    char bootstrap_key[256],fingerprint[65];
    bool identity_loaded,identity_persisted,bootstrap,enrollment_blocked;
    uint32_t enrollment_phase;
    ml_policy_t policy;
    unsigned stack_min[4];
    tailtiny_peer_diag_t peer_diag;
    tailtiny_state_t state;
    int derp_wake_fd,coord_sock,disco_sock4,disco_sock6,stun_sock,stun_sock6;
    struct {int sockfd;} derp;
    uint32_t t_disco_heartbeat_ms,t_stun_interval_ms,t_ctrl_watchdog_ms;
    void *config_httpd,*events,*heavy_mem_lock;
    QueueHandle_t derp_tx_queue,disco_rx_queue,wg_rx_queue,stun_rx_queue,coord_cmd_queue,peer_update_queue;
    void *net_io_task,*derp_tx_task,*coord_task,*wg_mgr_task;
    EventBits_t started_tasks;
    char nvs_auth_key[96],nvs_device_name[48],ctrl_host[64];
    uint8_t debug_flags;
    uint8_t machine_private_key[32],machine_public_key[32],wg_private_key[32],wg_public_key[32];
    uint8_t disco_private_key[32],disco_public_key[32];
    uint32_t vpn_ip;
    uint16_t derp_preferred_region,derp_home_region;
};
#define CONFIG_ML_PRIORITY_PEER_IP ""
#define CONFIG_VFS_SUPPORT_SELECT 0
#define ML_MAX_PEERS 2
#define ML_DISCO_HEARTBEAT_MS 3000
#define ML_STUN_RESTUN_INTERVAL_MS 23000
#define ML_CTRL_WATCHDOG_MS 120000
#define ML_DERP_TX_QUEUE_DEPTH 16
#define ML_DISCO_RX_QUEUE_DEPTH 16
#define ML_WG_RX_QUEUE_DEPTH 32
#define ML_STUN_RX_QUEUE_DEPTH 4
#define ML_COORD_CMD_QUEUE_DEPTH 4
#define ML_PEER_UPDATE_QUEUE_DEPTH 400
#define ML_EVT_SHUTDOWN_REQUEST 1
#define ML_CMD_DISCONNECT 1
#define ML_CTRL_PROTOCOL_VER 131
#define ML_REG_H2_BUF_SIZE 6144
#define ML_REG_JSON_BUF_SIZE 4096
#define ML_PREFERRED_DERP(ml) 9
#define ML_DERP_REGION 9
#define CTRL_HOST(ml) "control.example"
#define pdFALSE 0
#define pdTRUE 1
#define pdMS_TO_TICKS(n) (n)
/* Keep arguments evaluated, including timing/log-only variables, without
 * emitting any key, payload or server diagnostics. */
static void host_log(const char *tag,const char *format,...) {(void)tag;(void)format;}
#define ESP_LOGI host_log
#define ESP_LOGW host_log
#define ESP_LOGD host_log
#define ESP_LOGE host_log
static const char *TAG="enrollment-test";
static uint8_t s_node_key_challenge[32];
static bool s_has_node_key_challenge;
typedef union {size_t size;max_align_t alignment;} host_allocation_t;
void *ml_psram_malloc(size_t n) {
    if(alloc_count_on && ++alloc_count==alloc_fail_at)return NULL; /* armed fault */
    host_allocation_t *p=malloc(sizeof(*p)+n);if(!p)return NULL;
    p->size=n;return p+1;
}
static void *ml_psram_calloc(size_t n,size_t size) {
    void *p=ml_psram_malloc(n*size);if(p)memset(p,0,n*size);return p;
}
static void checked_free(void *p) {
    if(!p)return;
    host_allocation_t *a=(host_allocation_t *)p-1;
    const char *secret="DUMMY-BOOTSTRAP";size_t n=strlen(secret);
    for(size_t i=0;i+n<=a->size;i++)assert(memcmp((uint8_t *)p+i,secret,n));
    wiped_frees++;free(a);
}
/* Fingerprint hardware hash is irrelevant to the send decision. All keypair
 * consistency checks use the real X25519 implementation. */
static int mbedtls_sha256(const uint8_t *p,size_t n,uint8_t out[32],int mode) {
    (void)mode;assert(n==32);memcpy(out,p,32);return 0;
}
static void ml_peer_nvs_init(void) {}
static void ml_peer_nvs_deinit(void) {}
static void *ml_config_httpd_init(void) {return &nonsecret_handle;}
const char *ml_config_get_auth_key(void *h) {
    (void)h;legacy_reads++;return "DUMMY-LEGACY-MUST-NEVER-BE-USED";
}
static const char *ml_config_get_device_name_full(void *h) {(void)h;return "test";}
static const char *ml_config_get_device_prefix(void *h) {(void)h;return NULL;}
static uint8_t ml_config_get_max_peers(void *h) {(void)h;return 0;}
static uint16_t ml_config_get_disco_heartbeat_ms(void *h) {(void)h;return 0;}
static uint32_t ml_config_get_priority_peer_ip(void *h) {(void)h;return 0;}
static const char *ml_config_get_ctrl_host(void *h) {(void)h;return NULL;}
static uint8_t ml_config_get_debug_flags(void *h) {(void)h;return 0;}
static void ml_config_httpd_stop(void *h) {(void)h;}
static void ml_config_httpd_deinit(void *h) {(void)h;}
#define ESP_MAC_WIFI_STA 0
static void esp_read_mac(uint8_t *p,int mode) {(void)mode;memset(p,0,6);}
static void *xEventGroupCreate(void) {return &nonsecret_handle;}
static void *xSemaphoreCreateBinary(void) {return &nonsecret_handle;}
static void xSemaphoreGive(void *h) {(void)h;}
static void *xQueueCreate(unsigned n,size_t sz) {(void)n;(void)sz;return &nonsecret_handle;}
static void xEventGroupSetBits(void *h,unsigned bits) {(void)h;(void)bits;}
static unsigned xEventGroupWaitBits(void *h,unsigned bits,int a,int b,unsigned t) {
    (void)h;(void)a;(void)b;(void)t;return bits;
}
static void ml_derp_wake(tailtiny_t *ml) {(void)ml;}
static void ml_wg_notify(tailtiny_t *ml) {(void)ml;}
static int xQueueSend(void *q,void *p,unsigned t) {(void)q;(void)p;(void)t;return 1;}
static int xQueueReceive(void *q,void *p,unsigned t) {(void)q;(void)p;(void)t;return 0;}
static void vQueueDelete(void *q) {(void)q;}
static void vEventGroupDelete(void *q) {(void)q;}
static void vSemaphoreDelete(void *q) {(void)q;}
static int ml_close_sock(int fd) {(void)fd;assert(false);return -1;}
static int64_t esp_timer_get_time(void) {return 1000;}
int ml_h2_build_headers_frame(uint8_t *,size_t,const char *,const char *,const char *,const char *,uint32_t,bool);
/* ml_h2 exports real framing; declarations here match its production API. */
int ml_h2_build_data_frame(uint8_t *,size_t,const uint8_t *,size_t,uint32_t,bool);
int ml_h2_build_window_update(uint8_t *,size_t,uint32_t,uint32_t);
static int noise_send(tailtiny_t *ml,ml_noise_state_t *noise,const uint8_t *p,size_t n) {
    (void)ml;(void)noise;
    if(n<20)return (int)n; /* connection WINDOW_UPDATE, not enrollment */
    uint32_t durable_phase=0;
    assert(ml_identity_phase(ml->machine_public_key,&durable_phase)==ESP_OK);
    assert(durable_phase==ML_ENROLL_ATTEMPTED||durable_phase==ML_ENROLL_REGISTERED);
    if(no_wire)return -1; /* Crash/local failure after durable mark, before wire. */
    sends++;
    /* Headers contain binary zeros; find JSON DATA payload by actual framing. */
    size_t pos=0;bool found=false;
    while(pos+9<=n) {
        size_t len=((size_t)p[pos]<<16)|((size_t)p[pos+1]<<8)|p[pos+2];
        assert(pos+9+len<=n);
        if(p[pos+3]==0) {
            cJSON *j=cJSON_ParseWithLength((const char *)p+pos+9,len);assert(j);
            cJSON *a=cJSON_GetObjectItem(j,"Auth"),*key=cJSON_GetObjectItem(a,"AuthKey");
            if(key){
                assert(cJSON_IsString(key));auth_sends++;assert(!strcmp(key->valuestring,"DUMMY-BOOTSTRAP"));
                ml_secure_zero(key->valuestring,strlen(key->valuestring));
            }
            cJSON_Delete(j);found=true;
        }
        pos+=9+len;
    }
    assert(found);
    return ambiguous?-1:(int)n;
}
static int noise_recv(tailtiny_t *ml,ml_noise_state_t *noise,uint8_t *p,size_t n) {
    (void)ml;(void)noise;
    const char *reply="{\"MachineAuthorized\":true}";
    return ml_h2_build_data_frame(p,n,(const uint8_t *)reply,strlen(reply),1,true);
}
/* real exported header builder, no framing duplicate */
int ml_h2_build_headers_frame(uint8_t *,size_t,const char *,const char *,const char *,const char *,uint32_t,bool);
/* Observe, never alter, the production helper call made by do_register: the probe
 * forwards arguments and result unchanged and only records the allocator window. */
static unsigned auth_calls,auth_first,auth_last;
static bool auth_result;
static bool probed_registration_auth(cJSON *root,const char *key,uint32_t phase,bool permitted) {
    unsigned before=alloc_count;
    bool ok=ml_registration_auth(root,key,phase,permitted);
    auth_calls++;auth_first=before+1;auth_last=alloc_count;auth_result=ok;return ok;
}
#define ml_registration_auth probed_registration_auth
#define free checked_free
#include "enrollment-production.inc"
#undef free
#undef ml_registration_auth
static esp_err_t copy_key(char *p,size_t n) {
    copies++;assert(n==256);if(copy_fail)return ESP_FAIL;
    strcpy(p,"DUMMY-BOOTSTRAP");return ESP_OK;
}
const char *tailtiny_default_device_name(void) {return "test";}
uint32_t tailtiny_parse_ip(const char *s) {(void)s;return 0;}
void tailtiny_ip_to_str(uint32_t ip,char *s) {(void)ip;strcpy(s,"100.64.0.1");}
static tailtiny_t *init(void) {
    tailtiny_config_t config={.peer_fqdn="peer.example.ts.net",.peer_port=8941,.copy_bootstrap_key=copy_key,
                              .auth_key="DUMMY-CALLER-MUST-NEVER-BE-USED"};
    tailtiny_t *ml=tailtiny_init(&config);
    if(ml) {
        tailtiny_runtime_status_t snapshot;tailtiny_runtime_snapshot(ml,&snapshot);
        tailtiny_peer_diag_t zero={0};
        assert(!memcmp(&snapshot.peer_diag,&zero,sizeof(zero)));
    }
    return ml;
}
static int reg(tailtiny_t *ml) {
    ml_noise_state_t noise={0};int result=do_register(ml,&noise);
    for(size_t i=0;i<sizeof(ml->bootstrap_key);i++)assert(!ml->bootstrap_key[i]);
    return result;
}
static void faults(const char *s) {
    fail_open=!strcmp(s,"open");
    fail_read=!strcmp(s,"read");
    fail_write=!strcmp(s,"write")||!strcmp(s,"mark_set");
    fail_commit=!strcmp(s,"commit")||!strcmp(s,"mark_commit");
    fail_reopen=!strcmp(s,"reopen")||!strcmp(s,"mark_reopen");
    bad_readback=!strcmp(s,"readback")||!strcmp(s,"mark_readback");
}
int main(int argc,char **argv) {
    assert(argc==2);const char *s=argv[1];tailtiny_t *ml=NULL;
    if(!strcmp(s,"copy_retry")) {
        copy_fail=true;assert(!init());assert(disk_len && generated==3 && !sends);
        copy_fail=false;ml=init();assert(ml && copies==2 && generated==3);reg(ml);
        assert(sends==1 && auth_sends==1);
    } else if(!strcmp(s,"power_loss")) {
        ml=init();assert(ml && !sends);uint8_t pub[32];memcpy(pub,ml->wg_public_key,32);tailtiny_destroy(ml);
        ml=init();assert(ml && !memcmp(pub,ml->wg_public_key,32) && generated==3);
        reg(ml);assert(sends==1 && auth_sends==1);
    } else if(!strncmp(s,"mark_",5)) {
        ml=init();assert(ml);writes=0;faults(s);assert(reg(ml)<0 && !sends && !auth_sends);
        assert(reg(ml)<0 && !sends); /* failed gate cannot fall through keyless */
    } else if(!strcmp(s,"alloc_fault_auth")) {
        /* Production ml_registration_auth with the production cJSON hooks: fail each
         * allocation in turn; checked_free asserts no dummy key survives any free. */
        ml=init();assert(ml);
        const cJSON *none;
        cJSON *r=cJSON_CreateObject();assert(r);
        assert(ml_registration_auth(r,"DUMMY-BOOTSTRAP",ML_ENROLL_ATTEMPTED,true));
        assert(ml_registration_auth(r,"DUMMY-BOOTSTRAP",ML_ENROLL_REGISTERED,true));
        assert(!ml_registration_auth(r,"DUMMY-BOOTSTRAP",ML_ENROLL_PREPARED,false));
        assert(!ml_registration_auth(r,"",ML_ENROLL_PREPARED,true));
        assert(!ml_registration_auth(r,"DUMMY-BOOTSTRAP",99,true));
        none=cJSON_GetObjectItemCaseSensitive(r,"Auth");assert(!none);cJSON_Delete(r);
        for(unsigned n=1;;n++) {
            cJSON *root=cJSON_CreateObject();assert(root);
            alloc_count=0;alloc_fail_at=n;alloc_count_on=true;
            bool ok=ml_registration_auth(root,"DUMMY-BOOTSTRAP",ML_ENROLL_PREPARED,true);
            alloc_count_on=false;alloc_fail_at=0;
            cJSON *auth=cJSON_GetObjectItemCaseSensitive(root,"Auth");
            assert(!!auth==ok);
            if(ok) {
                assert(n==6 && alloc_count==5); /* every allocation in the helper was faulted once */
                cJSON *key=cJSON_GetObjectItemCaseSensitive(auth,"AuthKey");
                assert(cJSON_IsString(key) && !strcmp(key->valuestring,"DUMMY-BOOTSTRAP"));
                ml_secure_zero(key->valuestring,strlen(key->valuestring));
                cJSON_Delete(root);break;
            }
            assert(alloc_count==n && n<6);
            cJSON_Delete(root);
        }
        assert(!sends && !auth_sends);
    } else if(!strcmp(s,"alloc_fault_reg")) {
        /* Full production do_register on a PREPARED runtime with the real cJSON hooks.
         * Baseline run measures the allocations made inside ml_registration_auth; then
         * each one is failed in turn. do_register must fail closed: negative result,
         * no transport send, no durable ATTEMPTED mark, and checked_free asserts that no
         * freed block (including cJSON nodes and the runtime) still holds the key. */
        ambiguous=false;ml=init();assert(ml && ml->enrollment_phase==ML_ENROLL_PREPARED);
        ml_noise_state_t noise={0};
        alloc_count=0;alloc_fail_at=0;alloc_count_on=true;
        assert(do_register(ml,&noise)==0);
        alloc_count_on=false;
        assert(auth_calls==1 && auth_result && sends==1 && auth_sends==1);
        const unsigned first=auth_first,last=auth_last;
        assert(first>1 && last-first+1==5); /* auth object, key node, key bytes, two names */
        tailtiny_destroy(ml);ml=NULL;
        for(unsigned n=first;n<=last;n++) {
            disk_len=staged_len=0;generated=copies=sends=auth_sends=auth_calls=legacy_reads=0;
            ml=init();assert(ml && copies==1 && ml->bootstrap && ml->enrollment_phase==ML_ENROLL_PREPARED);
            noise=(ml_noise_state_t){0};
            alloc_count=0;alloc_fail_at=n;alloc_count_on=true;
            int result=do_register(ml,&noise);
            alloc_count_on=false;alloc_fail_at=0;
            assert(alloc_count>=n);                  /* the armed fault was reached */
            assert(auth_calls==1 && !auth_result);   /* ...inside ml_registration_auth */
            assert(result<0);                        /* caller propagates the failure */
            assert(!sends && !auth_sends && !legacy_reads);
            uint32_t phase=0;
            assert(ml_identity_phase(ml->machine_public_key,&phase)==ESP_OK && phase==ML_ENROLL_PREPARED);
            assert(ml->enrollment_phase==ML_ENROLL_PREPARED);
            tailtiny_destroy(ml);ml=NULL;
        }
        ml=NULL;
    } else if(!strcmp(s,"marked_no_wire")) {
        ml=init();assert(ml);no_wire=true;assert(reg(ml)<0 && !sends);
        tailtiny_destroy(ml);ml=init();assert(ml && copies==1 && generated==3);
        no_wire=false;reg(ml);assert(sends==1 && !auth_sends && !legacy_reads);
    } else if(!strcmp(s,"ambiguous")||!strcmp(s,"registered")||!strcmp(s,"authorized")) {
        ml=init();assert(ml);ambiguous=strcmp(s,"ambiguous")!=0?false:true;
        int result=reg(ml);assert(ambiguous?result<0:result==0);assert(sends==1 && auth_sends==1);
        ml_identity_record_t id;bool loaded;
        assert(ml_identity_prepare(&id,&loaded)==ESP_OK);
        /* Phase is covered by versioned record, not inferred from key presence. */
        uint32_t phase;memcpy(&phase,(uint8_t *)&id+8,4);
        assert(id.version==2 && phase==(ambiguous?2u:3u));ml_secure_zero(&id,sizeof(id));
        tailtiny_destroy(ml);ml=init();assert(ml && copies==1 && generated==3);
        reg(ml);assert(sends==2 && auth_sends==1 && legacy_reads==0);
    } else {
        bool fresh=!strcmp(s,"write")||!strcmp(s,"commit")||!strcmp(s,"reopen")||!strcmp(s,"readback");
        if(!fresh && strcmp(s,"legacy")) {
            ml=init();assert(ml);tailtiny_destroy(ml);ml=NULL;writes=0;
            if(!strcmp(s,"corrupt"))disk[0]^=1;
            if(!strcmp(s,"partial"))disk_len--;
            if(!strcmp(s,"phase_unknown")) {
                ml_identity_record_t *id=(void *)disk;id->version=1;id->checksum=ml_identity_checksum(id);
            }
            if(!strcmp(s,"keypair")) {
                ml_identity_record_t *id=(void *)disk;id->public_key[2][0]^=1;id->checksum=ml_identity_checksum(id);
            }
        }
        legacy=!strcmp(s,"legacy");faults(s);
        unsigned before=generated;ml=init();assert(!ml && !sends);
        if(!fresh)assert(generated==before && !writes);
    }
    if(ml)tailtiny_destroy(ml);
    assert(wiped_frees || !copies);
    puts(s);puts("PASS actual constructor + registration transport");
}