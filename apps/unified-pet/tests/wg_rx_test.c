/* Full verbatim RX function + strict real policy, retained SDK lwIP pbuf/mem.
 * Crypto/keypair/time/endpoint/replay predicates are explicit seams: this
 * exercises authenticated-result control flow, NOT cryptographic correctness. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "lwip/pbuf.h"
#include "lwip/ip_addr.h"
#include "lwip/prot/ip4.h"
#include "ml_policy.h"
#define WIREGUARD_MAX_SRC_IPS 1
#define WIREGUARD_AUTHTAG_LEN 16
#define REJECT_AFTER_TIME 180000
#define REJECT_AFTER_MESSAGES UINT64_MAX
#define REKEY_TIMEOUT 5000
#define WG_DEBUG(...) do { } while(0)
#define IP_ADDR_NETCMP_COMPAT(a,b,m) ip4_addr_netcmp((a),(b),(m))
struct wireguard_device {int unused;};
struct wireguard_keypair {
    bool receiving_valid,initiator;
    uint32_t keypair_millis,last_rx;
    uint64_t sending_counter;
};
struct wireguard_peer {
    uint32_t last_rx,keepalive_interval;
    bool send_handshake;
    struct {bool valid;ip_addr_t ip,mask;} allowed_source_ips[1];
};
struct message_transport_data {uint32_t receiver;uint8_t counter[8],enc_packet[256];};
static struct wireguard_keypair keypair;
static bool auth_ok,replay_ok,has_key,expired,alloc_fail;
static unsigned crypto_calls,replay_calls,endpoint_updates,key_updates,key_destroys;
static unsigned outstanding,allocations,frees,cleanup_calls,cases;
static unsigned split_at,plain_len;
static uint8_t plaintext[256];
static struct pbuf *allocated;
void *rx_malloc(size_t size) {
    void *p=malloc(size);if(p){outstanding++;allocations++;}return p;
}
void *rx_calloc(size_t n,size_t size) {
    void *p=calloc(n,size);if(p){outstanding++;allocations++;}return p;
}
void rx_free(void *p) {if(p){assert(outstanding);outstanding--;frees++;free(p);}}
static struct wireguard_keypair *get_peer_keypair_for_idx(struct wireguard_peer *p,uint32_t idx) {
    (void)p;assert(idx==1);return has_key?&keypair:NULL;
}
static bool wireguard_expired(uint32_t start,uint32_t limit) {(void)start;(void)limit;return expired;}
static uint64_t nonce_le(const uint8_t *p) {
    uint64_t value=0;for(unsigned i=0;i<8;i++)value|=(uint64_t)p[i]<<(8*i);return value;
}
#define U8TO64_LITTLE(p) nonce_le(p)
static struct pbuf *rx_alloc(pbuf_layer layer,u16_t length,pbuf_type type) {
    assert(length==plain_len);assert(!allocated);
    allocated=alloc_fail?NULL:pbuf_alloc(layer,length,type);return allocated;
}
static u8_t rx_cleanup(struct pbuf *p) {cleanup_calls++;return pbuf_free(p);}
static bool wireguard_decrypt_packet(void *dst,const uint8_t *src,size_t len,uint64_t nonce,struct wireguard_keypair *kp) {
    (void)src;assert(kp==&keypair&&nonce==7&&len==plain_len+16);
    crypto_calls++;assert(!endpoint_updates&&!replay_calls);
    if(!auth_ok)return false;
    assert(dst==allocated->payload);memcpy(dst,plaintext,plain_len);
    if(split_at&&split_at<plain_len) {
        /* Real chain at post-decrypt seam; pbuf RAM was contiguous for decrypt.
         * No fake pbuf_realloc/chain/free implementation in the test. */
        struct pbuf *tail=pbuf_alloc(PBUF_RAW,plain_len-split_at,PBUF_RAM);assert(tail);
        assert(pbuf_take(tail,plaintext+split_at,plain_len-split_at)==ERR_OK);
        pbuf_realloc(allocated,split_at);pbuf_cat(allocated,tail);
    }
    return true;
}
static void update_peer_addr(struct wireguard_peer *p,const ip_addr_t *addr,u16_t port) {
    (void)p;(void)addr;assert(port==5555&&auth_ok&&crypto_calls==1);endpoint_updates++;
}
static uint32_t wireguard_sys_now(void) {return 1234;}
static void keypair_update(struct wireguard_peer *p,struct wireguard_keypair *kp) {
    (void)p;assert(kp==&keypair&&endpoint_updates==1);key_updates++;
}
static bool wireguard_check_replay(struct wireguard_keypair *kp,uint64_t nonce) {
    assert(kp==&keypair&&nonce==7&&key_updates==1);replay_calls++;return replay_ok;
}
static void keypair_destroy(struct wireguard_keypair *kp) {assert(kp==&keypair);key_destroys++;}
#define pbuf_alloc rx_alloc
#define pbuf_free rx_cleanup
#include "wg-rx-production.inc"
#undef pbuf_alloc
#undef pbuf_free

static struct wireguard_peer peer;
static bool link_up;
static void packet(unsigned total) {
    memset(plaintext,0xa5,sizeof(plaintext)); /* Nonzero WG padding is allowed. */
    memset(plaintext,0,60);
    plaintext[0]=0x45;plaintext[2]=(uint8_t)(total>>8);plaintext[3]=(uint8_t)total;
    plaintext[9]=6;
    const uint8_t source[]={100,111,191,22},dest[]={100,120,115,125};
    memcpy(plaintext+12,source,4);memcpy(plaintext+16,dest,4);
    plaintext[20]=0x22;plaintext[21]=0xed; /* source port 8941 */
    plaintext[22]=0xc3;plaintext[23]=0x50; /* observed client port 50000 */
    plaintext[32]=0xa0;plaintext[33]=0x12; /* SYNACK + 40-byte TCP header */
}
static void reset(void) {
    assert(!outstanding);
    keypair=(struct wireguard_keypair){.receiving_valid=true};
    peer=(struct wireguard_peer){0};peer.allowed_source_ips[0].valid=true;
    IP4_ADDR(&peer.allowed_source_ips[0].ip,100,111,191,22);
    IP4_ADDR(&peer.allowed_source_ips[0].mask,255,255,255,255);
    auth_ok=replay_ok=has_key=true;expired=alloc_fail=false;
    crypto_calls=replay_calls=endpoint_updates=key_updates=key_destroys=0;
    allocations=frees=cleanup_calls=0;split_at=0;plain_len=64;allocated=NULL;link_up=false;
    packet(60);
}
static struct pbuf *receive(void) {
    struct wireguard_device device={0};
    struct message_transport_data data={.receiver=1,.counter={7}};
    ip_addr_t remote;IP4_ADDR(&remote,1,2,3,4);
    struct pbuf *delivered=NULL;
    wireguardif_process_data_message_locked(&device,&peer,&data,plain_len+16,&remote,5555,&link_up,&delivered);
    return delivered;
}
typedef struct {uint32_t events,vpn_ip;ml_policy_t policy;} policy_owner_t;
static policy_owner_t *s_policy_owner;
#define ML_EVT_SHUTDOWN_REQUEST 64
static uint32_t xEventGroupGetBits(uint32_t events) {return events;}
#include "wg-policy-production.inc"
static bool strict_policy(struct pbuf *p) {
    policy_owner_t owner={.vpn_ip=0x6478737d,.policy={.ip=0x646fbf16,.port=8941,.local_port=50000}};
    s_policy_owner=&owner;
    policy_packet_args_t args={.p=p,.inbound=true};
    check_packet_on_tcpip(&args);
    s_policy_owner=NULL;return args.allowed;
}
static void release(struct pbuf *p) {
    if(p)pbuf_free(p);
    assert(!outstanding&&allocations==frees);
}
static void pass(const char *name) {cases++;printf("PASS wg-rx %s\n",name);}
int main(void) {
    reset();struct pbuf *p=receive();
    assert(p&&strict_policy(p)); /* RED: old RX delivers 64 rather than IP length 60. */
    assert(p->tot_len==60&&cleanup_calls==0&&replay_calls==1&&link_up);
    release(p);pass("authenticated-result-60-in-64-real-policy-and-pbuf");
    reset();plain_len=60;p=receive();assert(p&&p->tot_len==60&&strict_policy(p));
    release(p);pass("unpadded-unchanged");
    reset();split_at=8;p=receive();
    assert(p&&p->len==8&&p->next&&p->next->len==52&&p->tot_len==60&&strict_policy(p));
    release(p);pass("split-fixed-header-real-pbuf-copy-and-chain-trim");
    reset();split_at=60;p=receive();
    assert(p&&p->tot_len==60&&!p->next&&frees==1&&outstanding==1&&strict_policy(p));
    release(p);pass("padding-only-tail-freed-by-real-pbuf-realloc");
    for(unsigned n=1;n<20;n++) {
        reset();plain_len=n;p=receive();assert(!p&&cleanup_calls==1&&replay_calls==1);
        release(p);
    }
    pass("all-short-fixed-headers-1-through-19");
    const uint8_t first_bytes[]={0x65,0x44,0x4f};
    for(unsigned i=0;i<3;i++){
        reset();if(i==2)packet(40); /* IHL 60 exceeds total length 40. */
        plaintext[0]=first_bytes[i];p=receive();assert(!p&&cleanup_calls==1);release(p);
        pass("invalid-version-or-IHL");
    }
    for(unsigned n=0;n<2;n++){
        reset();packet(n?65:19);p=receive();assert(!p&&cleanup_calls==1);release(p);
        pass("total-below-IHL-or-above-plaintext");
    }
    reset();plaintext[12]=101;p=receive();assert(!p&&cleanup_calls==1);release(p);
    pass("wrong-allowed-source-dropped-before-delivery");
    for(unsigned n=0;n<3;n++){
        reset();plaintext[n==0?16:n==1?20:22]^=1;p=receive();
        assert(p&&!strict_policy(p)&&cleanup_calls==0);release(p);pass("wrong-destination-or-ports-policy-still-rejects");
    }
    reset();plaintext[0]=0x46;p=receive();assert(p&&p->tot_len==60&&!strict_policy(p));
    release(p);pass("valid-IPv4-options-still-rejected-by-strict-policy");
    reset();plaintext[6]=0x20;p=receive();assert(p&&!strict_policy(p));
    release(p);pass("fragments-still-rejected");
    reset();auth_ok=false;p=receive();assert(!p&&!link_up&&crypto_calls==1&&!replay_calls&&!endpoint_updates&&cleanup_calls==1);
    release(p);pass("auth-failure-seam-single-cleanup-no-replay");
    reset();replay_ok=false;p=receive();assert(!p&&link_up&&replay_calls==1&&cleanup_calls==1);
    release(p);pass("replay-rejection-seam-single-cleanup");
    reset();plain_len=0;p=receive();assert(!p&&link_up&&crypto_calls==1&&!replay_calls&&cleanup_calls==1);
    release(p);pass("empty-keepalive-no-IP-read-or-replay-change");
    reset();alloc_fail=true;p=receive();assert(!p&&!crypto_calls&&!cleanup_calls&&!link_up);
    release(p);pass("allocation-failure");
    reset();has_key=false;p=receive();assert(!p&&!allocations&&!crypto_calls);release(p);pass("missing-key");
    reset();expired=true;p=receive();assert(!p&&!crypto_calls&&key_destroys==1);release(p);pass("expired-key");
    printf("PASS wg-rx total=%u; actual RX + policy callback + real SDK pbuf/mem; crypto/replay predicates stubbed\n",cases);
}