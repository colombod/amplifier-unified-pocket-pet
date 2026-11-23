#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
/* Include after nvs.h/cJSON.h. One bounded nonsecret versioned NVS record.
 * Lowercase ASCII FQDN only: no OS DNS, URLs, numeric addresses or aliases. */
typedef struct {
    uint32_t version;
    char host[64];
    uint16_t port;
    uint16_t reserved;
} setup_unified_target_t;
static inline bool setup_unified_host_valid(const char *host) {
    size_t n=strnlen(host,64),label=0;bool dot=false,alpha=false;
    if(!n || n>63 || host[0]=='-' || host[n-1]=='-' || host[n-1]=='.')return false;
    for(size_t i=0;i<n;i++) {
        char c=host[i];
        if(c=='.') {
            if(!label || host[i-1]=='-' || host[i+1]=='-')return false;
            dot=true;label=0;
        } else {
            if(!((c>='a'&&c<='z')||(c>='0'&&c<='9')||c=='-'))return false;
            if(c>='a'&&c<='z')alpha=true;
            if(++label>63)return false;
        }
    }
    return dot && alpha && label>0;
}
static inline esp_err_t setup_unified_copy(nvs_handle_t storage,setup_unified_target_t *out) {
    if(!out)return ESP_ERR_INVALID_ARG;
    memset(out,0,sizeof(*out));out->version=1;out->port=8941;
    setup_unified_target_t got={0};size_t n=sizeof(got);
    esp_err_t err=nvs_get_blob(storage,"unified_target",&got,&n);
    if(err!=ESP_OK)return err;
    if(n!=sizeof(got)||got.version!=1||got.reserved||!got.port||
       !memchr(got.host,0,sizeof(got.host))||
       (got.host[0]&&!setup_unified_host_valid(got.host)))return ESP_ERR_INVALID_STATE;
    *out=got;return ESP_OK;
}
static inline esp_err_t setup_unified_save(nvs_handle_t storage,const cJSON *json) {
    unsigned a=0,h=0,p=0;
    for(const cJSON *i=json->child;i;i=i->next)if(i->string) {
        if(!strcmp(i->string,"action"))a++;
        if(!strcmp(i->string,"host"))h++;
        if(!strcmp(i->string,"port"))p++;
    }
    const cJSON *action=cJSON_GetObjectItemCaseSensitive(json,"action");
    const cJSON *host=cJSON_GetObjectItemCaseSensitive(json,"host");
    const cJSON *port=cJSON_GetObjectItemCaseSensitive(json,"port");
    if(a!=1||h!=1||p!=1||!cJSON_IsString(action)||strcmp(action->valuestring,"unified_target")||
       !cJSON_IsString(host)||!cJSON_IsNumber(port)||port->valuedouble<1||
       port->valuedouble>65535||port->valuedouble!=(int)port->valuedouble||
       (host->valuestring[0]&&!setup_unified_host_valid(host->valuestring)))return ESP_ERR_INVALID_ARG;
    setup_unified_target_t next={.version=1,.port=(uint16_t)port->valueint};
    memcpy(next.host,host->valuestring,strlen(host->valuestring));
    esp_err_t err=nvs_set_blob(storage,"unified_target",&next,sizeof(next));
    if(err==ESP_OK)err=nvs_commit(storage);
    setup_unified_target_t check;
    if(err==ESP_OK)err=setup_unified_copy(storage,&check);
    if(err==ESP_OK && memcmp(&check,&next,sizeof(next)))err=ESP_FAIL;
    return err;
}