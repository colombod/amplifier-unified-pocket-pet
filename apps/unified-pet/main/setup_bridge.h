#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <math.h>
#include <stdio.h>
#include "setup_unified.h"
/* Include after nvs.h/cJSON.h. The supplied handle MUST be storage.own in
 * unified_pet. No legacy reads, networking, activation or credential logging. */
typedef struct {
    uint32_t version;
    uint8_t enabled;
    uint8_t ip[4];
    uint16_t port;
    uint16_t reserved;
    char fqdn[64];
} setup_bridge_t;
static inline void setup_bridge_wipe(void *p,size_t n) {
    volatile unsigned char *s=p;while(n--)*s++=0;
}
static inline bool setup_bridge_ip(const char *s,uint8_t out[4]) {
    if(!s||!out)return false;
    const char *p=s;unsigned v[4];
    for(unsigned i=0;i<4;i++) {
        if(*p<'0'||*p>'9')return false;
        bool zero=*p=='0';unsigned n=0,val=0;
        while(*p>='0'&&*p<='9') {
            if(++n>3)return false;
            val=val*10+(unsigned)(*p++-'0');
        }
        if((zero&&n>1)||val>255)return false;
        v[i]=val;
        if(i<3){if(*p++!='.')return false;}else if(*p)return false;
    }
    bool priv=v[0]==10||(v[0]==172&&v[1]>=16&&v[1]<=31)||(v[0]==192&&v[1]==168);
    bool endpoint=(v[0]==10&&((v[1]==0&&v[2]==0&&v[3]==0)||(v[1]==255&&v[2]==255&&v[3]==255)))||
        (v[0]==172&&((v[1]==16&&v[2]==0&&v[3]==0)||(v[1]==31&&v[2]==255&&v[3]==255)))||
        (v[0]==192&&v[1]==168&&((v[2]==0&&v[3]==0)||(v[2]==255&&v[3]==255)));
    if(!priv||endpoint)return false;
    for(unsigned i=0;i<4;i++)out[i]=(uint8_t)v[i];
    return true;
}
static inline bool setup_bridge_token_valid(const char *s) {
    if(!s||strnlen(s,44)!=43)return false;
    for(unsigned i=0;i<43;i++)if(!((s[i]>='A'&&s[i]<='Z')||(s[i]>='a'&&s[i]<='z')||
        (s[i]>='0'&&s[i]<='9')||s[i]=='-'||s[i]=='_'))return false;
    return strchr("AEIMQUYcgkosw048",s[42])!=NULL;
}
static inline bool setup_bridge_valid(const setup_bridge_t *v) {
    if(!v||v->version!=1||v->enabled>1||v->reserved||!v->port||
       !memchr(v->fqdn,0,sizeof(v->fqdn))||!setup_unified_host_valid(v->fqdn))return false;
    char ip[16];uint8_t parsed[4];
    snprintf(ip,sizeof(ip),"%u.%u.%u.%u",v->ip[0],v->ip[1],v->ip[2],v->ip[3]);
    return setup_bridge_ip(ip,parsed);
}
static inline esp_err_t setup_bridge_pair_clean(nvs_handle_t h) {
    uint8_t pending=0;size_t n=sizeof(pending);
    esp_err_t e=nvs_get_blob(h,"bridge_pending",&pending,&n);
    /* Any marker, including malformed data, is unresolved. Never auto-clear. */
    return e==ESP_ERR_NVS_NOT_FOUND?ESP_OK:e==ESP_OK||e==ESP_ERR_NVS_INVALID_LENGTH?ESP_ERR_INVALID_STATE:e;
}
static inline esp_err_t setup_bridge_raw_copy(nvs_handle_t h,setup_bridge_t *out) {
    if(!out)return ESP_ERR_INVALID_ARG;
    memset(out,0,sizeof(*out));out->version=1;
    setup_bridge_t got={0};size_t n=sizeof(got);
    esp_err_t e=nvs_get_blob(h,"bridge_cfg",&got,&n);
    if(e==ESP_ERR_NVS_NOT_FOUND)return ESP_OK; /* absent alone disables bridge */
    if(e!=ESP_OK)return e==ESP_ERR_NVS_INVALID_LENGTH?ESP_ERR_INVALID_STATE:e;
    if(n!=sizeof(got)||!setup_bridge_valid(&got))return ESP_ERR_INVALID_STATE;
    *out=got;return ESP_OK;
}
static inline esp_err_t setup_bridge_raw_token(nvs_handle_t h,char *out,size_t cap) {
    if(out&&cap)memset(out,0,cap);
    if(!out||cap<44)return ESP_ERR_INVALID_ARG;
    char got[44]={0};size_t n=sizeof(got);
    esp_err_t e=nvs_get_str(h,"bridge_tok",got,&n);
    if(e==ESP_OK&&(n!=44||got[43]!=0||!setup_bridge_token_valid(got)))e=ESP_ERR_INVALID_STATE;
    if(e==ESP_ERR_NVS_INVALID_LENGTH)e=ESP_ERR_INVALID_STATE;
    if(e==ESP_OK)memcpy(out,got,sizeof(got));
    setup_bridge_wipe(got,sizeof(got));return e;
}
static inline esp_err_t setup_bridge_copy(nvs_handle_t h,setup_bridge_t *out) {
    if(!out)return ESP_ERR_INVALID_ARG;
    memset(out,0,sizeof(*out));
    esp_err_t e=setup_bridge_pair_clean(h);
    return e==ESP_OK?setup_bridge_raw_copy(h,out):e;
}
static inline esp_err_t setup_bridge_copy_token(nvs_handle_t h,char *out,size_t cap) {
    if(out&&cap)memset(out,0,cap);
    if(!out||cap<44)return ESP_ERR_INVALID_ARG;
    esp_err_t e=setup_bridge_pair_clean(h);
    return e==ESP_OK?setup_bridge_raw_token(h,out,cap):e;
}
static inline esp_err_t setup_bridge_state(nvs_handle_t h,setup_bridge_t *out,bool *token_saved) {
    if(!out||!token_saved)return ESP_ERR_INVALID_ARG;
    *token_saved=false;
    esp_err_t e=setup_bridge_copy(h,out);
    char tok[44]={0};
    if(e==ESP_OK){
        esp_err_t te=setup_bridge_raw_token(h,tok,sizeof(tok));
        if(te==ESP_OK)*token_saved=true;
        else if(te!=ESP_ERR_NVS_NOT_FOUND||out->enabled)e=te==ESP_ERR_NVS_NOT_FOUND?ESP_ERR_INVALID_STATE:te;
    }
    setup_bridge_wipe(tok,sizeof(tok));
    if(e!=ESP_OK)memset(out,0,sizeof(*out));
    return e;
}
static inline esp_err_t setup_bridge_clear_pending(nvs_handle_t h) {
    esp_err_t e=nvs_erase_key(h,"bridge_pending");
    if(e==ESP_ERR_NVS_NOT_FOUND)e=ESP_OK;
    if(e==ESP_OK)e=nvs_commit(h);
    if(e==ESP_OK)e=setup_bridge_pair_clean(h);
    return e;
}
static inline esp_err_t setup_bridge_restore(nvs_handle_t h,const setup_bridge_t *old,bool had_cfg,
                                             const char *tok,bool had_tok) {
    uint8_t pending=1;
    esp_err_t fence=nvs_set_blob(h,"bridge_pending",&pending,sizeof(pending));
    if(fence==ESP_OK)fence=nvs_commit(h);
    if(fence!=ESP_OK)return ESP_ERR_INVALID_STATE;
    esp_err_t a=had_cfg?nvs_set_blob(h,"bridge_cfg",old,sizeof(*old)):nvs_erase_key(h,"bridge_cfg");
    esp_err_t b=had_tok?nvs_set_str(h,"bridge_tok",tok):nvs_erase_key(h,"bridge_tok");
    esp_err_t c=nvs_commit(h);
    if((a!=ESP_OK&&a!=ESP_ERR_NVS_NOT_FOUND)||(b!=ESP_OK&&b!=ESP_ERR_NVS_NOT_FOUND)||c!=ESP_OK)return ESP_ERR_INVALID_STATE;
    setup_bridge_t check={0};size_t n=sizeof(check);
    c=nvs_get_blob(h,"bridge_cfg",&check,&n);
    if(had_cfg?(c!=ESP_OK||n!=sizeof(check)||memcmp(old,&check,sizeof(check))):(c!=ESP_ERR_NVS_NOT_FOUND))return ESP_ERR_INVALID_STATE;
    char read[44]={0};c=setup_bridge_raw_token(h,read,sizeof(read));
    bool same=had_tok?(c==ESP_OK&&!strcmp(tok,read)):c==ESP_ERR_NVS_NOT_FOUND;
    setup_bridge_wipe(read,sizeof(read));
    return same?setup_bridge_clear_pending(h):ESP_ERR_INVALID_STATE;
}
static inline esp_err_t setup_bridge_save(nvs_handle_t h,cJSON *j,bool *token_saved) {
    if(!token_saved||!cJSON_IsObject(j))return ESP_ERR_INVALID_ARG;
    const char *names[]={"action","enabled","ip","port","fqdn","token","clear_token"};
    unsigned counts[7]={0};bool shape=true;
    for(const cJSON *i=j->child;i;i=i->next) {
        unsigned k=0;while(k<7&&(!i->string||strcmp(i->string,names[k])))k++;
        if(k==7||++counts[k]>1){shape=false;break;}
    }
    const cJSON *a=cJSON_GetObjectItemCaseSensitive(j,"action"),*en=cJSON_GetObjectItemCaseSensitive(j,"enabled");
    const cJSON *ip=cJSON_GetObjectItemCaseSensitive(j,"ip"),*port=cJSON_GetObjectItemCaseSensitive(j,"port");
    const cJSON *fq=cJSON_GetObjectItemCaseSensitive(j,"fqdn"),*t=cJSON_GetObjectItemCaseSensitive(j,"token");
    const cJSON *cl=cJSON_GetObjectItemCaseSensitive(j,"clear_token");
    setup_bridge_t next={.version=1},old={0},check={0};
    char oldtok[44]={0},newtok[44]={0},readtok[44]={0};
    bool had_cfg=false,had_tok=false,dirty=false;
    esp_err_t e=ESP_ERR_INVALID_ARG;
    if(!shape||!cJSON_IsString(a)||strcmp(a->valuestring,"bridge_gateway")||!cJSON_IsBool(en)||
       !cJSON_IsString(ip)||!cJSON_IsNumber(port)||!isfinite(port->valuedouble)||
       port->valuedouble<1||port->valuedouble>65535||port->valuedouble!=(int)port->valuedouble||
       !cJSON_IsString(fq)||!setup_unified_host_valid(fq->valuestring)||
       (t&&!cJSON_IsString(t))||(cl&&!cJSON_IsBool(cl))||!setup_bridge_ip(ip->valuestring,next.ip))goto done;
    bool clear=cJSON_IsTrue(cl),replace=t&&t->valuestring[0];
    if((replace&&!setup_bridge_token_valid(t->valuestring))||(clear&&replace))goto done;
    e=setup_bridge_pair_clean(h);if(e!=ESP_OK)goto done;
    next.enabled=cJSON_IsTrue(en);next.port=(uint16_t)port->valueint;
    memcpy(next.fqdn,fq->valuestring,strlen(fq->valuestring));
    size_t n=sizeof(old);
    e=nvs_get_blob(h,"bridge_cfg",&old,&n);
    if(e==ESP_OK){if(n!=sizeof(old)||!setup_bridge_valid(&old)){e=ESP_ERR_INVALID_STATE;goto done;}had_cfg=true;}
    else if(e!=ESP_ERR_NVS_NOT_FOUND)goto done;
    e=setup_bridge_copy_token(h,oldtok,sizeof(oldtok));
    if(e==ESP_OK)had_tok=true;else if(e!=ESP_ERR_NVS_NOT_FOUND)goto done;
    if(replace)memcpy(newtok,t->valuestring,44);else if(!clear&&had_tok)memcpy(newtok,oldtok,44);
    if(next.enabled&&!setup_bridge_token_valid(newtok)){e=ESP_ERR_INVALID_ARG;goto done;}
    /* A durable intent precedes either record write. A power loss at any point
     * leaves a marker that blocks BOTH bridge and provider fallback on restart.
     * This is detection, not an atomic multi-key NVS transaction. */
    uint8_t pending=1,verify_pending=0;size_t pn=sizeof(verify_pending);
    dirty=true;e=nvs_set_blob(h,"bridge_pending",&pending,sizeof(pending));
    if(e==ESP_OK)e=nvs_commit(h);
    if(e==ESP_OK)e=nvs_get_blob(h,"bridge_pending",&verify_pending,&pn);
    if(e==ESP_OK&&(pn!=1||verify_pending!=1))e=ESP_FAIL;
    if(e==ESP_OK)e=nvs_set_blob(h,"bridge_cfg",&next,sizeof(next));
    if(e==ESP_OK) {
        e=newtok[0]?nvs_set_str(h,"bridge_tok",newtok):nvs_erase_key(h,"bridge_tok");
        if(e==ESP_ERR_NVS_NOT_FOUND&&!newtok[0])e=ESP_OK;
    }
    if(e==ESP_OK)e=nvs_commit(h);
    if(e==ESP_OK)e=setup_bridge_raw_copy(h,&check);
    if(e==ESP_OK&&memcmp(&next,&check,sizeof(next)))e=ESP_FAIL;
    if(e==ESP_OK){
        esp_err_t te=setup_bridge_raw_token(h,readtok,sizeof(readtok));
        if(newtok[0]?(te!=ESP_OK||strcmp(newtok,readtok)):(te!=ESP_ERR_NVS_NOT_FOUND))e=ESP_FAIL;
    }
    if(e==ESP_OK)e=setup_bridge_clear_pending(h);
    if(e==ESP_OK)*token_saved=newtok[0]!=0;
    else if(dirty&&setup_bridge_restore(h,&old,had_cfg,oldtok,had_tok)!=ESP_OK)e=ESP_ERR_INVALID_STATE;
done:
    if(t&&cJSON_IsString(t))setup_bridge_wipe(t->valuestring,strlen(t->valuestring));
    setup_bridge_wipe(oldtok,sizeof(oldtok));setup_bridge_wipe(newtok,sizeof(newtok));
    setup_bridge_wipe(readtok,sizeof(readtok));return e;
}
