/* Production header tests; all persistence is synthetic, never device NVS. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "cJSON.h"
typedef int esp_err_t;
typedef int nvs_handle_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG 1
#define ESP_ERR_INVALID_SIZE 2
#define ESP_ERR_NVS_NOT_FOUND 3
#define ESP_ERR_NVS_INVALID_LENGTH 4
#define ESP_ERR_INVALID_STATE 5
static unsigned char blob[128];
static size_t blob_size;
static char token[256];
static bool blob_present, token_present,pending_present;
static uint8_t pending_value;
static int writes, fail_set, fail_commit, fail_get, fail_token_and_restore;
static esp_err_t nvs_get_blob(nvs_handle_t h,const char *k,void *out,size_t *n) {
    assert(h==1);
    if(fail_get){fail_get--;return ESP_FAIL;}
    if(!strcmp(k,"bridge_pending")){
        if(!pending_present)return ESP_ERR_NVS_NOT_FOUND;
        assert(*n>=1);*(uint8_t*)out=pending_value;*n=1;return ESP_OK;
    }
    assert(!strcmp(k,"bridge_cfg"));
    if(!blob_present)return ESP_ERR_NVS_NOT_FOUND;
    if(*n<blob_size){*n=blob_size;return ESP_ERR_NVS_INVALID_LENGTH;}
    memcpy(out,blob,blob_size);*n=blob_size;return ESP_OK;
}
static esp_err_t nvs_get_str(nvs_handle_t h,const char *k,char *out,size_t *n) {
    assert(h==1&&!strcmp(k,"bridge_tok"));
    if(fail_get){fail_get--;return ESP_FAIL;}
    if(!token_present)return ESP_ERR_NVS_NOT_FOUND;
    size_t need=strlen(token)+1;
    if(out&&*n<need){*n=need;return ESP_ERR_NVS_INVALID_LENGTH;}
    if(out)memcpy(out,token,need);
    *n=need;return ESP_OK;
}
static esp_err_t nvs_set_blob(nvs_handle_t h,const char *k,const void *in,size_t n) {
    assert(h==1);writes++;
    if(fail_set){fail_set--;return ESP_FAIL;}
    if(!strcmp(k,"bridge_pending")){assert(n==1);pending_value=*(const uint8_t*)in;pending_present=true;return ESP_OK;}
    assert(!strcmp(k,"bridge_cfg"));
    if(fail_token_and_restore==2)return ESP_FAIL;
    assert(n<=sizeof(blob));memcpy(blob,in,n);blob_size=n;blob_present=true;return ESP_OK;
}
static esp_err_t nvs_set_str(nvs_handle_t h,const char *k,const char *in) {
    assert(h==1&&!strcmp(k,"bridge_tok"));writes++;
    if(fail_set){fail_set--;return ESP_FAIL;}
    if(fail_token_and_restore){fail_token_and_restore=2;return ESP_FAIL;}
    assert(strlen(in)<sizeof(token));strcpy(token,in);token_present=true;return ESP_OK;
}
static esp_err_t nvs_erase_key(nvs_handle_t h,const char *k) {
    assert(h==1);writes++;
    if(!strcmp(k,"bridge_pending")){pending_present=false;pending_value=0;}
    else if(!strcmp(k,"bridge_cfg")){blob_present=false;memset(blob,0,sizeof(blob));}
    else{assert(!strcmp(k,"bridge_tok"));token_present=false;memset(token,0,sizeof(token));}
    return ESP_OK;
}
static esp_err_t nvs_commit(nvs_handle_t h) {
    assert(h==1);if(fail_commit){fail_commit--;return ESP_FAIL;}return ESP_OK;
}
#include "setup_bridge.h"
static const char *T="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
static esp_err_t save(const char *s,bool *saved) {
    cJSON *j=cJSON_Parse(s);assert(j);
    esp_err_t e=setup_bridge_save(1,j,saved);cJSON_Delete(j);return e;
}
static void valid(bool *saved) {
    char body[320];snprintf(body,sizeof(body),"{\"action\":\"bridge_gateway\",\"enabled\":true,\"ip\":\"192.168.1.50\",\"port\":9443,\"fqdn\":\"pet.example.net\",\"token\":\"%s\"}",T);
    assert(save(body,saved)==ESP_OK&&*saved);
}
int main(void) {
    setup_bridge_t cfg;bool saved=false;char copy[44];
    assert(setup_bridge_copy(1,&cfg)==ESP_OK&&!cfg.enabled);
    assert(setup_bridge_copy_token(1,copy,sizeof(copy))==ESP_ERR_NVS_NOT_FOUND&&!copy[0]);
    const char *endpoints[]={"10.0.0.0","10.255.255.255","172.16.0.0","172.31.255.255","192.168.0.0","192.168.255.255","127.0.0.1","100.64.1.2","8.8.8.8","192.168.01.2","192.168.1.256","192.168.1.2x"};
    uint8_t ip[4];
    for(size_t i=0;i<sizeof(endpoints)/sizeof(*endpoints);i++)assert(!setup_bridge_ip(endpoints[i],ip));
    const char *good[]={"10.0.0.1","10.255.255.254","172.16.0.1","172.31.255.254","192.168.0.1","192.168.255.254","192.168.4.2","192.168.1.0","192.168.1.255"};
    for(size_t i=0;i<sizeof(good)/sizeof(*good);i++)assert(setup_bridge_ip(good[i],ip));
    assert(setup_bridge_token_valid(T));
    assert(!setup_bridge_token_valid("AAA"));
    char badtoken[44];strcpy(badtoken,T);badtoken[42]='B';assert(!setup_bridge_token_valid(badtoken));
    valid(&saved);
    assert(setup_bridge_copy(1,&cfg)==ESP_OK&&cfg.enabled&&cfg.port==9443);
    assert(setup_bridge_copy_token(1,copy,sizeof(copy))==ESP_OK&&!strcmp(copy,T));
    assert(setup_bridge_copy_token(1,copy,8)==ESP_ERR_INVALID_ARG);
    int before=writes;
    const char *bad[]={
        "{\"action\":\"bridge_gateway\",\"enabled\":true,\"ip\":\"10.0.0.2\",\"port\":1,\"fqdn\":\"pet.example.net\",\"unknown\":0}",
        "{\"action\":\"bridge_gateway\",\"enabled\":true,\"enabled\":false,\"ip\":\"10.0.0.2\",\"port\":1,\"fqdn\":\"pet.example.net\"}",
        "{\"action\":\"bridge_gateway\",\"enabled\":1,\"ip\":\"10.0.0.2\",\"port\":1,\"fqdn\":\"pet.example.net\"}",
        "{\"action\":\"bridge_gateway\",\"enabled\":true,\"ip\":\"10.0.0.2\",\"port\":true,\"fqdn\":\"pet.example.net\"}",
        "{\"action\":\"bridge_gateway\",\"enabled\":true,\"ip\":\"10.0.0.2\",\"port\":1.5,\"fqdn\":\"pet.example.net\"}",
        "{\"action\":\"bridge_gateway\",\"enabled\":true,\"ip\":\"10.0.0.2\",\"port\":1,\"fqdn\":\"Pet.example.net\"}",
        "{\"action\":\"bridge_gateway\",\"enabled\":true,\"ip\":\"10.0.0.2\",\"port\":1,\"fqdn\":\"pet.example.net\",\"token\":null}",
        "{\"action\":\"bridge_gateway\",\"enabled\":true,\"ip\":\"10.0.0.2\",\"port\":1,\"fqdn\":\"pet.example.net\",\"clear_token\":true}"
    };
    for(size_t i=0;i<sizeof(bad)/sizeof(*bad);i++){assert(save(bad[i],&saved)!=ESP_OK);assert(writes==before);}
    unsigned char old[128];memcpy(old,blob,sizeof(blob));size_t oldsize=blob_size;
    const char *keep="{\"action\":\"bridge_gateway\",\"enabled\":true,\"ip\":\"10.0.0.2\",\"port\":1234,\"fqdn\":\"pet.example.net\",\"token\":\"\"}";
    fail_set=1;assert(save(keep,&saved)!=ESP_OK);assert(blob_size==oldsize&&!memcmp(old,blob,oldsize)&&token_present&&!strcmp(token,T));
    fail_commit=1;assert(save(keep,&saved)!=ESP_OK);assert(blob_size==oldsize&&!memcmp(old,blob,oldsize)&&token_present&&!strcmp(token,T));
    fail_get=1;before=writes;assert(save(keep,&saved)!=ESP_OK&&writes==before);
    assert(save(keep,&saved)==ESP_OK&&saved);
    assert(save("{\"action\":\"bridge_gateway\",\"enabled\":false,\"ip\":\"10.0.0.2\",\"port\":1234,\"fqdn\":\"pet.example.net\",\"clear_token\":true}",&saved)==ESP_OK&&!saved);
    assert(!token_present&&setup_bridge_copy(1,&cfg)==ESP_OK&&!cfg.enabled);
    /* Simulated restart during a torn pair never selects either backend. */
    pending_present=true;pending_value=1;before=writes;
    assert(setup_bridge_copy(1,&cfg)==ESP_ERR_INVALID_STATE&&!cfg.enabled);
    assert(setup_bridge_copy_token(1,copy,sizeof(copy))==ESP_ERR_INVALID_STATE&&!copy[0]);
    assert(save(keep,&saved)==ESP_ERR_INVALID_STATE&&writes==before);
    pending_present=false;
    valid(&saved);
    fail_token_and_restore=1;
    assert(save(keep,&saved)==ESP_ERR_INVALID_STATE);
    assert(pending_present&&setup_bridge_copy(1,&cfg)==ESP_ERR_INVALID_STATE);
    fail_token_and_restore=0; /* explicit synthetic restoration, not production auto-retry */
    assert(setup_bridge_restore(1,(setup_bridge_t*)old,true,T,true)==ESP_OK);
    valid(&saved);blob[0]=99;memcpy(old,blob,sizeof(blob));before=writes;
    assert(setup_bridge_copy(1,&cfg)==ESP_ERR_INVALID_STATE&&!cfg.enabled);
    assert(writes==before&&!memcmp(old,blob,sizeof(blob)));
    puts("PASS: bridge production storage/validation, own handle only, strict fields, RFC1918 endpoints, token, keep/clear, rollback and corrupt durable state unchanged");
}
