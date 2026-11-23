#pragma once
#include <stdbool.h>
#include <string.h>
#include "nvs.h"
/* One app-local write store and a read-only legacy view. Never migrate. */
typedef struct {
    nvs_handle_t own,legacy;
    bool legacy_ready;
} pet_store_t;
static inline esp_err_t pet_store_open(pet_store_t *s,const char *legacy_namespace) {
    memset(s,0,sizeof(*s));
    esp_err_t err=nvs_open("unified_pet",NVS_READWRITE,&s->own);
    if(err!=ESP_OK)return err;
    err=nvs_open(legacy_namespace,NVS_READONLY,&s->legacy);
    if(err==ESP_OK)s->legacy_ready=true;
    else if(err!=ESP_ERR_NVS_NOT_FOUND){nvs_close(s->own);return err;}
    return ESP_OK;
}
static inline esp_err_t pet_store_get_str(pet_store_t *s,const char *key,char *out,size_t *n) {
    size_t capacity=*n;
    esp_err_t err=nvs_get_str(s->own,key,out,n);
    if(err==ESP_ERR_NVS_NOT_FOUND && s->legacy_ready) {
        *n=capacity;err=nvs_get_str(s->legacy,key,out,n);
    }
    if(err!=ESP_OK && out)memset(out,0,capacity);
    return err;
}
static inline esp_err_t pet_store_get_volume(pet_store_t *s,uint8_t *out) {
    esp_err_t err=nvs_get_u8(s->own,"volume",out);
    if(err==ESP_ERR_NVS_NOT_FOUND&&s->legacy_ready)err=nvs_get_u8(s->legacy,"volume",out);
    if(err==ESP_OK&&*out>100)return ESP_ERR_INVALID_STATE;
    return err;
}