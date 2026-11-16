#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
/* Local application fence, NOT a tailnet ACL evaluator. No pre-map cache trust.
 * All accesses are made on tcpip_thread via ml_tcpip_run. */
typedef struct {
    char host[64];
    uint16_t port,local_port;
    uint32_t ip;
    uint8_t key[32];
    bool conflict;
} ml_policy_t;
static inline bool ml_policy_name(const char *target,const char *name) {
    if(!name)return false;
    size_t n=strnlen(name,66);
    if(!n||n>64)return false;
    if(name[n-1]=='.')n--;
    return n<=63&&n==strlen(target)&&!memcmp(target,name,n);
}
static inline bool ml_policy_peer(ml_policy_t *p,const char *host,uint32_t ip,const uint8_t key[32]) {
    if(p->conflict||!p->host[0]||strcmp(p->host,host)||!ip||(ip&0xffc00000u)!=0x64400000u)return false;
    uint8_t any=0;for(unsigned i=0;i<32;i++)any|=key[i];if(!any)return false;
    if(p->ip && (p->ip!=ip || memcmp(p->key,key,32))) {__atomic_store_n(&p->conflict,true,__ATOMIC_RELEASE);return false;}
    memcpy(p->key,key,32);__atomic_store_n(&p->ip,ip,__ATOMIC_RELEASE);return true;
}
static inline uint16_t ml_be16(const uint8_t *p) {return (uint16_t)((p[0]<<8)|p[1]);}
static inline uint32_t ml_be32(const uint8_t *p) {
    return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3];
}
static inline bool ml_policy_packet(ml_policy_t *p,const uint8_t *b,size_t n,bool inbound,uint32_t self) {
    if(!self||!p->ip||!p->port||p->conflict||n<40||b[0]!=0x45||b[9]!=6||
       (ml_be16(b+6)&0x3fff)||ml_be16(b+2)!=n)return false;
    unsigned th=(b[32]>>4)*4;
    if(th<20||20+th>n)return false;
    uint32_t src=ml_be32(b+12),dst=ml_be32(b+16);
    uint16_t sp=ml_be16(b+20),dp=ml_be16(b+22);
    if(inbound) {
        /* Responses only, to our one observed outbound connection. */
        return src==p->ip&&dst==self&&sp==p->port&&p->local_port&&dp==p->local_port&&
            !(b[33]&2 && !(b[33]&16));
    }
    if(src!=self||dst!=p->ip||dp!=p->port||!sp)return false;
    if(!p->local_port) {
        if((b[33]&0x12)!=2)return false; /* first packet must be SYN, not ACK */
        p->local_port=sp;
    }
    return sp==p->local_port;
}