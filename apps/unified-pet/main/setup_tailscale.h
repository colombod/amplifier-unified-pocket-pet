#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
/* Internal setup handler helpers. Include after cJSON.h and nvs.h.
 * Header-only, like setup_address/network_retry, so host tests use the real code. */
#define SETUP_TAILSCALE_KEY_CAPACITY 256
#define SETUP_TAILSCALE_NVS_KEY "ts_auth_key"

static inline cJSON *setup_parse_json(const char *body,size_t length) {
    /* cJSON strings cannot represent embedded NUL; never silently save a prefix. */
    if(memchr(body,0,length))return NULL;
    for(size_t i=0;i<length;i++)if(body[i]=='\\') {
        if(i+5<length && !memcmp(body+i+1,"u0000",5))return NULL;
        i++; /* Skip an escaped backslash rather than treating it as a new escape. */
    }
    cJSON *json=cJSON_ParseWithLengthOpts(body,length+1,NULL,true);
    if(!cJSON_IsObject(json)){cJSON_Delete(json);return NULL;}
    return json;
}
static inline esp_err_t setup_tailscale_copy(nvs_handle_t storage,char *out,size_t capacity) {
    if(!out || capacity<2)return ESP_ERR_INVALID_ARG;
    memset(out,0,capacity);
    size_t length=capacity;
    esp_err_t err=nvs_get_str(storage,SETUP_TAILSCALE_NVS_KEY,out,&length);
    if(err!=ESP_OK)memset(out,0,capacity);
    return err;
}
static inline bool setup_tailscale_saved(nvs_handle_t storage) {
    size_t n=0;
    return nvs_get_str(storage,SETUP_TAILSCALE_NVS_KEY,NULL,&n)==ESP_OK &&
        n>1 && n<=SETUP_TAILSCALE_KEY_CAPACITY;
}
static inline esp_err_t setup_tailscale_save(nvs_handle_t storage,cJSON *json,bool *saved) {
    cJSON *key=cJSON_GetObjectItemCaseSensitive(json,"tailscale_auth_key");
    unsigned count=0;
    for(cJSON *item=json->child;item;item=item->next)
        if(item->string && !strcmp(item->string,"tailscale_auth_key"))count++;
    if(count>1 || (key && (!cJSON_IsString(key) ||
       strlen(key->valuestring)>=SETUP_TAILSCALE_KEY_CAPACITY)))return ESP_ERR_INVALID_ARG;
    if(!key || !key->valuestring[0]) {
        size_t n=0;
        esp_err_t err=nvs_get_str(storage,SETUP_TAILSCALE_NVS_KEY,NULL,&n);
        if(err!=ESP_OK && err!=ESP_ERR_NVS_NOT_FOUND)return err;
        *saved=err==ESP_OK && n>1 && n<=SETUP_TAILSCALE_KEY_CAPACITY;
        return ESP_OK; /* Blank/omitted is keep, never clear. */
    }
    char previous[SETUP_TAILSCALE_KEY_CAPACITY]={0};
    esp_err_t prior=setup_tailscale_copy(storage,previous,sizeof(previous));
    esp_err_t err=prior;
    if(prior==ESP_OK || prior==ESP_ERR_NVS_NOT_FOUND) {
        err=nvs_set_str(storage,SETUP_TAILSCALE_NVS_KEY,key->valuestring);
        if(err==ESP_OK)err=nvs_commit(storage);
        if(err==ESP_OK)*saved=true;
        else {
            /* NVS is not a transaction. Best-effort restore if a write/commit fails;
             * persistent flash failure can also prevent this rollback. Never ACK it. */
            esp_err_t restored=prior==ESP_OK?
                nvs_set_str(storage,SETUP_TAILSCALE_NVS_KEY,previous):
                nvs_erase_key(storage,SETUP_TAILSCALE_NVS_KEY);
            if(restored==ESP_OK)(void)nvs_commit(storage);
        }
    }
    memset(previous,0,sizeof(previous));
    memset(key->valuestring,0,strlen(key->valuestring));
    return err;
}