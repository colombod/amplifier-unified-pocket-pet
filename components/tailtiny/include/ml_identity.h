#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
/* Production NVS/crypto seam; included after nvs.h, esp_random.h and x25519.h.
 * Integrity is not encryption or tamper-proof storage. Never repair corruption. */
enum {
    ML_ENROLL_PREPARED=1,   /* Identity durable; credential never sent. */
    ML_ENROLL_ATTEMPTED=2,  /* Durable BEFORE send; outcome may be ambiguous. */
    ML_ENROLL_REGISTERED=3
};
typedef struct {
    uint32_t magic,version,phase;
    uint8_t private_key[3][32],public_key[3][32];
    uint32_t checksum;
} ml_identity_record_t;
static inline void ml_secure_zero(void *p,size_t n) {
    volatile uint8_t *v=p;while(n--)*v++=0;
}
static inline uint32_t ml_identity_checksum(const ml_identity_record_t *id) {
    const uint8_t *p=(const uint8_t *)id;uint32_t v=2166136261u;
    for(size_t i=0;i<offsetof(ml_identity_record_t,checksum);i++)v=(v^p[i])*16777619u;
    return v;
}
static inline bool ml_identity_valid(const ml_identity_record_t *id) {
    /* v1 has no phase: do not infer permission to replay from key presence. */
    if(id->magic!=0x50455449||id->version!=2||
       id->phase<ML_ENROLL_PREPARED||id->phase>ML_ENROLL_REGISTERED||
       id->checksum!=ml_identity_checksum(id))return false;
    for(unsigned i=0;i<3;i++) {
        uint8_t public_key[32];
        if((id->private_key[i][0]&7)||(id->private_key[i][31]&0xc0)!=0x40)return false;
        x25519_base(public_key,id->private_key[i],1);
        bool ok=!memcmp(public_key,id->public_key[i],32);ml_secure_zero(public_key,32);
        if(!ok)return false;
    }
    return true;
}
static inline esp_err_t ml_identity_read(nvs_handle_t h,ml_identity_record_t *id) {
    size_t n=sizeof(*id);esp_err_t e=nvs_get_blob(h,"identity_v1",id,&n);
    if(e==ESP_OK && (n!=sizeof(*id)||!ml_identity_valid(id)))return ESP_ERR_INVALID_STATE;
    return e;
}
static inline esp_err_t ml_identity_write_checked(nvs_handle_t h,ml_identity_record_t *id) {
    id->checksum=ml_identity_checksum(id);
    esp_err_t e=nvs_set_blob(h,"identity_v1",id,sizeof(*id));
    if(e==ESP_OK)e=nvs_commit(h);
    nvs_close(h);
    if(e==ESP_OK) {
        e=nvs_open("microlink",NVS_READONLY,&h);
        if(e==ESP_OK) {
            ml_identity_record_t check={0};e=ml_identity_read(h,&check);
            if(e==ESP_OK && memcmp(id,&check,sizeof(check)))e=ESP_FAIL;
            ml_secure_zero(&check,sizeof(check));nvs_close(h);
        }
    }
    return e;
}
/* Registration reads the validated durable gate afresh. Never creates a record. */
static inline esp_err_t ml_identity_phase(const uint8_t machine_public[32],uint32_t *phase) {
    nvs_handle_t h;ml_identity_record_t id={0};
    esp_err_t e=nvs_open("microlink",NVS_READONLY,&h);
    if(e==ESP_OK) {
        e=ml_identity_read(h,&id);nvs_close(h);
        if(e==ESP_OK && memcmp(id.public_key[0],machine_public,32))e=ESP_ERR_INVALID_STATE;
        if(e==ESP_OK)*phase=id.phase;
    }
    ml_secure_zero(&id,sizeof(id));return e;
}
static inline esp_err_t ml_identity_transition(const uint8_t machine_public[32],
                                              uint32_t from,uint32_t to) {
    if(!((from==ML_ENROLL_PREPARED && to==ML_ENROLL_ATTEMPTED)||
         (from==ML_ENROLL_ATTEMPTED && to==ML_ENROLL_REGISTERED)))return ESP_ERR_INVALID_STATE;
    nvs_handle_t h;ml_identity_record_t id={0};
    esp_err_t e=nvs_open("microlink",NVS_READWRITE,&h);
    if(e==ESP_OK) {
        e=ml_identity_read(h,&id);
        if(e==ESP_OK && (id.phase!=from||memcmp(id.public_key[0],machine_public,32)))e=ESP_ERR_INVALID_STATE;
        if(e==ESP_OK){id.phase=to;e=ml_identity_write_checked(h,&id);}
        else nvs_close(h);
    }
    ml_secure_zero(&id,sizeof(id));return e;
}
static inline esp_err_t ml_identity_prepare(ml_identity_record_t *out,bool *loaded) {
    memset(out,0,sizeof(*out));*loaded=false;
    nvs_handle_t h;esp_err_t e=nvs_open("microlink",NVS_READWRITE,&h);
    if(e!=ESP_OK)return e;
    e=ml_identity_read(h,out);
    if(e==ESP_OK){*loaded=true;nvs_close(h);return ESP_OK;}
    if(e!=ESP_ERR_NVS_NOT_FOUND){nvs_close(h);ml_secure_zero(out,sizeof(*out));return e;}
    const char *legacy[]={"machine_pri","machine_pub","wg_private","wg_public","disco_pri","disco_pub"};
    for(unsigned i=0;i<6;i++) {
        size_t n=0;e=nvs_get_blob(h,legacy[i],NULL,&n);
        if(e!=ESP_ERR_NVS_NOT_FOUND) {
            nvs_close(h);return e==ESP_OK?ESP_ERR_INVALID_STATE:e;
        }
    }
    out->magic=0x50455449;out->version=2;out->phase=ML_ENROLL_PREPARED;
    for(unsigned i=0;i<3;i++) {
        esp_fill_random(out->private_key[i],32);
        out->private_key[i][0]&=248;out->private_key[i][31]&=127;out->private_key[i][31]|=64;
        x25519_base(out->public_key[i],out->private_key[i],1);
    }
    e=ml_identity_write_checked(h,out);
    if(e!=ESP_OK)ml_secure_zero(out,sizeof(*out));
    return e;
}