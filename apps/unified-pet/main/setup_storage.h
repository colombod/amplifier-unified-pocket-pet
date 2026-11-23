#pragma once
/* App-local compatibility reads, never migration. Callers own/wipe secrets.
 * Only ESP_ERR_NVS_NOT_FOUND permits fallback; malformed records/I/O errors
 * remain visible. All writes use .own, always the unified_pet namespace. */
#include "pet_store.h"
#include "cJSON.h"
#include "setup_tailscale.h"
#include "setup_unified.h"
static inline esp_err_t pet_store_tailscale_copy(pet_store_t *s,char *out,size_t n) {
    if(!out||n<2)return ESP_ERR_INVALID_ARG;
    memset(out,0,n);return pet_store_get_str(s,SETUP_TAILSCALE_NVS_KEY,out,&n);
}
static inline esp_err_t pet_store_tailscale_status(pet_store_t *s,bool *saved) {
    size_t n=0;
    esp_err_t err=pet_store_get_str(s,SETUP_TAILSCALE_NVS_KEY,NULL,&n);
    if(err!=ESP_OK&&err!=ESP_ERR_NVS_NOT_FOUND)return err;
    *saved=err==ESP_OK&&n>1&&n<=SETUP_TAILSCALE_KEY_CAPACITY;
    return ESP_OK;
}
static inline esp_err_t pet_store_tailscale_save(pet_store_t *s,cJSON *json,bool *saved) {
    /* Keep the canonical JSON validation, bounds and own-record rollback. */
    esp_err_t err=setup_tailscale_save(s->own,json,saved);
    if(err==ESP_OK)err=pet_store_tailscale_status(s,saved);
    return err;
}
static inline esp_err_t pet_store_unified_copy(pet_store_t *s,setup_unified_target_t *out) {
    esp_err_t err=setup_unified_copy(s->own,out);
    if(err==ESP_ERR_NVS_NOT_FOUND&&s->legacy_ready)err=setup_unified_copy(s->legacy,out);
    return err;
}