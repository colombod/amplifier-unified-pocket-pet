#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "x25519.h"
typedef int esp_err_t;
typedef int nvs_handle_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG 1
#define ESP_ERR_INVALID_SIZE 2
#define ESP_ERR_INVALID_STATE 3
#define ESP_ERR_NVS_NOT_FOUND 4
#define NVS_READWRITE 1
#define NVS_READONLY 0
static unsigned char disk[512];
static size_t disk_len;
static int fail_open, fail_read, fail_set, fail_commit, fail_reopen, bad_readback;
static bool legacy;
static int writes, generated;
static esp_err_t nvs_open(const char *ns,int mode,nvs_handle_t *h) {
    (void)ns; (void)mode;
    if(fail_open || (writes && fail_reopen))return ESP_FAIL;
    *h=1; return ESP_OK;
}
static void nvs_close(nvs_handle_t h) {(void)h;}
static esp_err_t nvs_get_blob(nvs_handle_t h,const char *key,void *out,size_t *n) {
    (void)h;
    if(fail_read)return ESP_FAIL;
    if(strcmp(key,"identity_v1") && strcmp(key,"unified_target"))
        return legacy?ESP_ERR_INVALID_STATE:ESP_ERR_NVS_NOT_FOUND;
    if(!disk_len)return ESP_ERR_NVS_NOT_FOUND;
    if(out && *n<disk_len)return ESP_ERR_INVALID_SIZE;
    if(out){memcpy(out,disk,disk_len);if(writes && bad_readback)((uint8_t *)out)[0]^=1;}
    *n=disk_len;return ESP_OK;
}
static esp_err_t nvs_set_blob(nvs_handle_t h,const char *key,const void *p,size_t n) {
    (void)h;(void)key;writes++;
    if(fail_set)return ESP_FAIL;
    assert(n<=sizeof(disk));memcpy(disk,p,n);disk_len=n;return ESP_OK;
}
static esp_err_t nvs_commit(nvs_handle_t h) {(void)h;return fail_commit?ESP_FAIL:ESP_OK;}
static void esp_fill_random(void *p,size_t n) {
    generated++;for(size_t i=0;i<n;i++)((uint8_t *)p)[i]=(uint8_t)(i+generated);
}
#include "setup_unified.h"
#include "ml_identity.h"
#include "ml_registration.h"
static void reset(void) {
    memset(disk,0,sizeof(disk));disk_len=0;legacy=false;writes=generated=0;
    fail_open=fail_read=fail_set=fail_commit=fail_reopen=bad_readback=0;
}
static esp_err_t target(const char *body) {
    cJSON *j=cJSON_Parse(body);assert(j);
    esp_err_t e=setup_unified_save(1,j);cJSON_Delete(j);return e;
}
int main(void) {
    const char *reg[]={"{\"MachineAuthorized\":true}","{\"MachineAuthorized\":false}","{}",
        "{\"MachineAuthorized\":true,\"AuthURL\":\"https://invalid.example/approval\"}",
        "{\"MachineAuthorized\":true,\"Error\":\"denied\"}"};
    for(unsigned i=0;i<sizeof(reg)/sizeof(reg[0]);i++) {
        cJSON *j=cJSON_Parse(reg[i]);assert(ml_registration_result(j)==(i==0));cJSON_Delete(j);
    }
    for(unsigned phase=ML_ENROLL_PREPARED;phase<=ML_ENROLL_REGISTERED;phase++) {
        cJSON *j=cJSON_CreateObject();
        assert(ml_registration_auth(j,"DUMMY-STORED-BOOTSTRAP",phase,true));
        char *wire=cJSON_PrintUnformatted(j);assert(wire);
        assert((strstr(wire,"AuthKey")!=NULL)==(phase==ML_ENROLL_PREPARED));
        free(wire);cJSON_Delete(j);
    }
    setup_unified_target_t t;
    reset();assert(setup_unified_copy(1,&t)==ESP_ERR_NVS_NOT_FOUND);
    assert(t.host[0]==0 && t.port==8941);
    assert(target("{\"action\":\"unified_target\",\"host\":\"node.example.ts.net\",\"port\":8941}")==ESP_OK);
    assert(setup_unified_copy(1,&t)==ESP_OK && !strcmp(t.host,"node.example.ts.net") && t.port==8941);
    const char *bad[]={
        "{\"action\":\"unified_target\",\"host\":\"100.64.1.1\",\"port\":8941}",
        "{\"action\":\"unified_target\",\"host\":\"https://node.example\",\"port\":8941}",
        "{\"action\":\"unified_target\",\"host\":\"Node.example\",\"port\":8941}",
        "{\"action\":\"unified_target\",\"host\":\"node.example/\",\"port\":8941}",
        "{\"action\":\"unified_target\",\"host\":\"node.example\",\"port\":0}",
        "{\"action\":\"unified_target\",\"host\":\"node.example\",\"port\":1.5}",
        "{\"action\":\"unified_target\",\"host\":\"node.example\",\"port\":\"8941\"}",
        "{\"action\":\"unified_target\",\"action\":\"unified_target\",\"host\":\"node.example\",\"port\":8941}",
        "{\"action\":\"unified_target\",\"host\":\"node.example\",\"host\":\"other.example\",\"port\":8941}",
        "{\"action\":\"unified_target\",\"host\":\"node.example\",\"port\":8941,\"port\":1}"
    };
    int before=writes;
    for(size_t i=0;i<sizeof(bad)/sizeof(bad[0]);i++)assert(target(bad[i])!=ESP_OK);
    assert(writes==before);
    fail_set=1;assert(target("{\"action\":\"unified_target\",\"host\":\"node.example\",\"port\":1}")!=ESP_OK);
    fail_set=0;fail_commit=1;assert(target("{\"action\":\"unified_target\",\"host\":\"node.example\",\"port\":1}")!=ESP_OK);
    reset();
    ml_identity_record_t id;bool loaded=true;
    assert(ml_identity_prepare(&id,&loaded)==ESP_OK && !loaded && writes==1 && generated==3);
    unsigned char saved[512];memcpy(saved,disk,disk_len);size_t len=disk_len;
    assert(ml_identity_prepare(&id,&loaded)==ESP_OK && loaded && generated==3 && writes==1);
    for(int failure=0;failure<6;failure++) {
        reset();
        if(failure==0)fail_open=1;
        if(failure==1)fail_read=1;
        if(failure==2)fail_set=1;
        if(failure==3)fail_commit=1;
        if(failure==4)fail_reopen=1;
        if(failure==5)bad_readback=1;
        assert(ml_identity_prepare(&id,&loaded)!=ESP_OK);
    }
    reset();legacy=true;assert(ml_identity_prepare(&id,&loaded)!=ESP_OK && !generated && !writes);
    for(size_t i=0;i<len;i++) {
        reset();memcpy(disk,saved,len);disk_len=len;disk[i]^=1;
        assert(ml_identity_prepare(&id,&loaded)!=ESP_OK && !generated && !writes);
    }
    reset();memcpy(disk,saved,len);disk_len=len-1;
    assert(ml_identity_prepare(&id,&loaded)!=ESP_OK && !writes);
    puts("PASS production target and identity: absent/resume/corrupt/legacy/length/keypair/I/O/write/commit/reopen/readback");
}