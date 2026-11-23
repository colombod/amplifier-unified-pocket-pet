/* Verbatim parser/admission/install/snapshot bodies. Fixtures never logged.
 * Real cJSON and policy; queue, allocations and WG peripheral are seams. */
#include <assert.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include "tailtiny.h"
#include "ml_policy.h"
#include "cJSON.h"
#define ML_MAX_PEERS 1
#define ML_MAX_ENDPOINTS 8
#define pdTRUE 1
#define pdMS_TO_TICKS(n) (n)
#define WIREGUARDIF_INVALID_INDEX 255
#define WIREGUARD_MAX_PEERS 1
#define ERR_OK 0
typedef uint8_t u8_t;
typedef int err_t;
#include "peer-types.inc"
struct netif {void *state;};
struct wireguard_peer {uint8_t public_key[32];};
struct wireguard_device {struct wireguard_peer peers[1];};
struct wireguardif_peer {
    const char *public_key,*preshared_key;
    struct {union {uint32_t ip4;} u_addr;} allowed_ip,allowed_mask,endpoint_ip;
    uint16_t endport_port,keep_alive;
};
#define IP4_ADDR(dst,a,b,c,d) (*(dst)=((uint32_t)(a)<<24)|((uint32_t)(b)<<16)|((uint32_t)(c)<<8)|(d))
struct tailtiny_s {
    tailtiny_config_t config;
    ml_policy_t policy;
    tailtiny_peer_diag_t peer_diag;
    ml_peer_t peers[1];
    int peer_count;
    void *wg_netif,*config_httpd,*peer_update_queue;
    bool identity_loaded,identity_persisted,bootstrap;
    char fingerprint[65];
    unsigned stack_min[4];
    tailtiny_peer_cb_t peer_cb;void *peer_cb_data;
};
typedef struct {tailtiny_t *ml;const ml_peer_update_t *u;bool allowed;} policy_peer_args_t;
static void quiet_log(const char *tag,const char *format,...) {(void)tag;(void)format;}
#define ESP_LOGI quiet_log
#define ESP_LOGW quiet_log
static const char *TAG="fixture-log-disabled";
/* Capture only explicit targeted diagnostic writes; never print fixture data. */
#define ESP_LOG_INFO 3
static unsigned observation_count;
static char observation[768];
static void esp_log_write(int level,const char *tag,const char *format,...) {
    assert(level==ESP_LOG_INFO&&!strcmp(tag,"tailnet_spike"));
    va_list args;va_start(args,format);
    int n=vsnprintf(observation,sizeof(observation),format,args);va_end(args);
    assert(n>0&&(size_t)n<sizeof(observation));observation_count++;
    assert(!strstr(observation,"nodekey:")&&!strstr(observation,"discokey:"));
    assert(!strstr(observation,"1111111111111111"));
}
static bool alloc_fail,queue_fail,allowed=true,install_fail,invalid_index;
static ml_peer_update_t *queue[16];
static unsigned queue_size,installs,checks;
static void ml_peer_diag_inc(uint32_t *counter);
static void *ml_psram_calloc(size_t n,size_t size) {return alloc_fail?NULL:calloc(n,size);}
static int ml_wg_queue_send(tailtiny_t *ml,void *q,const void *value,unsigned wait) {
    (void)ml;(void)q;(void)wait;
    if(queue_fail)return 0;
    assert(queue_size<16);queue[queue_size++]=*(ml_peer_update_t *const *)value;return pdTRUE;
}
static int xQueueReceive(void *q,void *out,int wait) {
    (void)q;(void)wait;if(!queue_size)return 0;
    *(ml_peer_update_t **)out=queue[0];queue_size--;
    memmove(queue,queue+1,queue_size*sizeof(queue[0]));return pdTRUE;
}
static void tailtiny_ip_to_str_seam(uint32_t ip,char *text) {(void)ip;strcpy(text,"fixture-hidden");}
#define tailtiny_ip_to_str tailtiny_ip_to_str_seam
static void ml_tcpip_run(void (*fn)(void *),void *arg) {fn(arg);}
static bool ml_config_peer_is_allowed(void *config,uint32_t ip) {(void)config;(void)ip;return allowed;}
static void wireguardif_remove_peer(struct netif *n,int index) {(void)n;(void)index;}
static void key_to_base64(const uint8_t *key,char *b64,size_t size) {(void)key;assert(size>=2);strcpy(b64,"x");}
static void wireguardif_peer_init(struct wireguardif_peer *p) {memset(p,0,sizeof(*p));}
static void ip_addr_set_any(bool ipv6,void *ip) {(void)ipv6;memset(ip,0,sizeof(uint32_t));}
static err_t wireguardif_add_peer(struct netif *n,const struct wireguardif_peer *p,u8_t *index) {
    (void)n;(void)p;installs++;*index=invalid_index?WIREGUARDIF_INVALID_INDEX:0;return install_fail?-1:ERR_OK;
}
static void ml_peer_nvs_save(ml_peer_t *p) {(void)p;}
static bool ml_at_socket_is_ready(void) {return true;}
static void disco_send_call_me_maybe(tailtiny_t *ml,int index) {(void)ml;(void)index;}
static void disco_send_ping_to_peer(tailtiny_t *ml,int index,bool force) {(void)ml;(void)index;(void)force;}
static uint64_t ml_get_time_ms(void) {return 1000;}
#include "peer-production.inc"

static tailtiny_t ml;
static struct netif netif;
static void reset(void) {
    while(queue_size)free(queue[--queue_size]);
    memset(&ml,0,sizeof(ml));strcpy(ml.policy.host,"node.example");ml.policy.port=8941;
    ml.wg_netif=&netif;alloc_fail=queue_fail=install_fail=invalid_index=false;allowed=true;installs=0;
    observation_count=0;observation[0]=0;
}
static cJSON *node(const char *name,const char *dns,bool ipv6_first,bool bad_key) {
    cJSON *p=cJSON_CreateObject();assert(p);
    cJSON_AddStringToObject(p,"Name",name);cJSON_AddStringToObject(p,"DNSName",dns);
    cJSON_AddStringToObject(p,"Key",bad_key?"nodekey:bad":"nodekey:1111111111111111111111111111111111111111111111111111111111111111");
    cJSON *a=cJSON_AddArrayToObject(p,"Addresses");
    if(ipv6_first)cJSON_AddItemToArray(a,cJSON_CreateString("fd00::1/128"));
    cJSON_AddItemToArray(a,cJSON_CreateString("100.100.1.2/32"));
    return p;
}
static void map(bool delta,const char *name,const char *dns,bool ipv6,bool bad) {
    cJSON *r=cJSON_CreateObject(),*a=cJSON_AddArrayToObject(r,delta?"PeersChanged":"Peers");
    cJSON_AddItemToArray(a,node(name,dns,ipv6,bad));
    char *text=cJSON_PrintUnformatted(r);cJSON_Delete(r);
    r=parse_peer_map(&ml,text,delta);free(text);assert(r);
    parse_peers_from_map_response(&ml,r);cJSON_Delete(r);
}
static void pass(const char *label) {checks++;printf("PASS peer-diag %s\n",label);}
static void *saturate(void *arg) {for(unsigned i=0;i<40000;i++)ml_peer_diag_inc(arg);return NULL;}
static void observe_node(cJSON *p,bool known,bool bare,bool duplicate) {
    cJSON *addresses=cJSON_GetObjectItem(p,"Addresses");
    if(known)cJSON_AddItemToArray(addresses,cJSON_CreateString(bare?"100.64.0.1":"100.64.0.1/32"));
    cJSON *root=cJSON_CreateObject(),*peers=cJSON_AddArrayToObject(root,"Peers");
    cJSON_AddItemToArray(peers,p);
    if(duplicate)cJSON_AddItemToArray(peers,cJSON_Duplicate(p,true));
    parse_peers_from_map_response(&ml,root);cJSON_Delete(root);
}
int main(void) {
    (void)esp_log_write;
    reset();observe_node(node("unified-host.tail-example.ts.net.","",true,false),true,false,false);
    assert(observation_count==1&&strstr(observation,"knownServerSeen=1")); /* Expected functional RED. */
    assert(strstr(observation,"expected=node.example"));
    assert(strstr(observation,"name=unified-host.tail-example.ts.net."));
    assert(!queue_size&&!ml.peer_diag.name_match);pass("known-server-canonical-name-observed-not-admitted");
    reset();observe_node(node("UNIFIED-HOST.tail-example.ts.net.","",false,false),true,true,false);
    assert(observation_count==1&&strstr(observation,"name=UNIFIED-HOST.tail-example.ts.net."));
    assert(!queue_size);pass("known-server-bare-IP-case-retained-no-selection-change");
    reset();observe_node(node("other-server.example","node.example",false,false),true,false,true);
    assert(observation_count==1&&strstr(observation,"name=other-server.example")&&strstr(observation,"dns=node.example"));
    assert(!queue_size&&ml.peer_diag.entries==2);pass("known-server-different-name-once-per-map");
    reset();observe_node(node("private-third-party.example","private-dns.example",false,false),false,false,false);
    assert(observation_count==1&&strstr(observation,"knownServerSeen=0"));
    assert(!strstr(observation,"private-")&&!strstr(observation,"100.100.1.2"));
    pass("nonserver-name-address-and-key-not-observed");
    reset();cJSON *missing=node("","",false,false);
    cJSON_DeleteItemFromObjectCaseSensitive(missing,"Name");
    cJSON_ReplaceItemInObjectCaseSensitive(missing,"DNSName",cJSON_CreateNumber(12));
    observe_node(missing,true,false,false);
    assert(observation_count==1&&strstr(observation,"nameType=0 nameLen=0 nameBad=0 name=-"));
    assert(strstr(observation,"dnsType=8 dnsLen=0 dnsBad=0 dns=-")&&!queue_size);
    pass("missing-and-nonstring-fields-explicit");
    reset();char hostile[131];memset(hostile,'a',130);hostile[0]='%';hostile[1]='\n';hostile[2]='\r';hostile[129]='\t';hostile[130]=0;
    observe_node(node(hostile,"bad\ndns.example",false,false),true,false,false);
    assert(observation_count==1&&strlen(observation)<450);
    assert(strstr(observation,"nameLen=130 nameBad=1 name=___"));
    assert(strstr(observation,"dnsLen=15 dnsBad=1 dns=bad_dns.example"));
    assert(!strchr(observation,'\r')&&!strchr(observation,'%'));
    assert(strchr(observation,'\n')==observation+strlen(observation)-1);
    assert(!queue_size);pass("long-control-format-characters-sanitized-and-bounded");
    reset();tailtiny_runtime_status_t s;tailtiny_runtime_snapshot(&ml,&s);
    tailtiny_peer_diag_t zero={0};assert(!memcmp(&s.peer_diag,&zero,sizeof(zero)));
    map(false,"node.example.","node.example.",false,false);
    assert(ml.peer_diag.entries==1); /* Meaningful RED before counters instrumented. */
    assert(ml.peer_diag.full==1&&ml.peer_diag.name_match==1&&ml.peer_diag.dns_match==1);
    assert(ml.peer_diag.addr0_ipv4==1&&ml.peer_diag.any_tailnet_ipv4==1&&ml.peer_diag.key_valid==1);
    assert(ml.peer_diag.queued==1&&queue_size==1);
    process_peer_updates(&ml);
    assert(ml.peer_diag.add_dequeued==1&&ml.peer_diag.policy_accepted==1&&ml.peer_diag.wg_installed==1);
    pass("initial-map-through-install");
    map(true,"node.example","node.example",false,false);process_peer_updates(&ml);
    assert(ml.peer_diag.delta==1&&ml.peer_diag.entries==2);pass("delta-map");
    assert(!parse_peer_map(&ml,"{",false)&&ml.peer_diag.full==1);pass("invalid-json-not-counted");
    reset();map(false,"other.example","node.example.",false,false);
    assert(ml.peer_diag.entries==1&&!ml.peer_diag.name_match&&ml.peer_diag.dns_match==1&&!queue_size);
    pass("DNS-only-match-not-admitted");
    reset();map(false,"node.example","node.example",true,false);
    assert(!ml.peer_diag.addr0_ipv4&&ml.peer_diag.any_tailnet_ipv4==1&&queue_size==1&&queue[0]->vpn_ip==0);
    process_peer_updates(&ml);
    assert(ml.peer_diag.policy_rejected==1&&!ml.policy.ip&&!installs);pass("IPv6-first-selection-unchanged");
    reset();map(false,"node.example","node.example",false,true);
    assert(!ml.peer_diag.key_valid);process_peer_updates(&ml);
    assert(ml.peer_diag.policy_rejected==1);pass("invalid-key");
    reset();alloc_fail=true;map(false,"node.example","node.example",false,false);
    assert(ml.peer_diag.alloc_fail==1&&!ml.peer_diag.queued&&!queue_size);pass("allocation-failure");
    reset();queue_fail=true;map(false,"node.example","node.example",false,false);
    assert(ml.peer_diag.queue_fail==1&&!ml.peer_diag.queued&&!queue_size);pass("queue-failure");
    reset();cJSON *r=cJSON_CreateObject(),*a=cJSON_AddArrayToObject(r,"Peers");
    cJSON_AddItemToArray(a,node("node.example","",false,false));cJSON_AddItemToArray(a,node("node.example.","",false,false));
    parse_peers_from_map_response(&ml,r);cJSON_Delete(r);
    assert(ml.peer_diag.entries==2&&ml.peer_diag.name_match==2&&ml.peer_diag.duplicate==1&&ml.policy.conflict&&!queue_size);
    pass("duplicate-name-rejected");
    reset();allowed=false;map(false,"node.example","",false,false);process_peer_updates(&ml);
    assert(ml.peer_diag.policy_accepted==1&&ml.peer_diag.allowlist_rejected==1&&ml.policy.ip&&!installs);
    pass("allowlist-reject-after-policy-publication");
    reset();ml.peers[0].active=true;ml.peers[0].public_key[0]=9;ml.peer_count=1;
    map(false,"node.example","",false,false);process_peer_updates(&ml);
    assert(ml.peer_diag.slot_rejected==1&&!installs);pass("slot-reject");
    reset();ml.wg_netif=NULL;map(false,"node.example","",false,false);process_peer_updates(&ml);
    assert(ml.peer_diag.missing_netif==1&&ml.policy.ip&&!ml.peer_diag.wg_installed&&!installs);
    pass("missing-netif-policy-ip-is-not-install-proof");
    reset();install_fail=true;map(false,"node.example","",false,false);process_peer_updates(&ml);
    assert(ml.peer_diag.wg_failed==1&&!ml.peer_diag.wg_installed&&ml.policy.ip);
    pass("install-failure");
    reset();invalid_index=true;map(false,"node.example","",false,false);process_peer_updates(&ml);
    assert(ml.peer_diag.wg_failed==1);pass("invalid-install-index");
    reset();r=cJSON_Parse("{\"PeersRemoved\":[\"nodekey:11\"],\"PeersChangedPatch\":{\"nodekey:11\":{\"Online\":false}}}");
    assert(r);parse_peers_from_map_response(&ml,r);cJSON_Delete(r);
    assert(ml.peer_diag.removed==1&&ml.peer_diag.patches==1);process_peer_updates(&ml);pass("removed-and-patches");
    reset();pthread_t threads[2];
    for(unsigned i=0;i<2;i++)assert(!pthread_create(&threads[i],NULL,saturate,&ml.peer_diag.entries));
    for(unsigned i=0;i<2;i++)assert(!pthread_join(threads[i],NULL));
    tailtiny_runtime_snapshot(&ml,&s);assert(s.peer_diag.entries==65535);
    s.peer_diag.entries=0;assert(ml.peer_diag.entries==65535);
    ml.peer_diag=(tailtiny_peer_diag_t){1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22};
    tailtiny_runtime_snapshot(&ml,&s);
    #define CHECK(field) assert(s.peer_diag.field==ml.peer_diag.field)
    CHECK(full);CHECK(delta);CHECK(entries);CHECK(name_match);CHECK(dns_match);CHECK(duplicate);
    CHECK(addr0_ipv4);CHECK(any_tailnet_ipv4);CHECK(key_valid);CHECK(alloc_fail);CHECK(queued);CHECK(queue_fail);
    CHECK(removed);CHECK(patches);CHECK(add_dequeued);CHECK(policy_accepted);CHECK(policy_rejected);
    CHECK(allowlist_rejected);CHECK(slot_rejected);CHECK(missing_netif);CHECK(wg_installed);CHECK(wg_failed);
    #undef CHECK
    tailtiny_runtime_snapshot(NULL,&s);assert(!memcmp(&s.peer_diag,&zero,sizeof(zero)));
    pass("atomic-saturation-independent-snapshot-null");
    reset();printf("PASS peer-diag total=%u; no fixture names/addresses/keys/maps emitted\n",checks);
    return 0;
}