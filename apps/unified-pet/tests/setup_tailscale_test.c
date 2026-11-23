#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "cJSON.h"
/* Small NVS fault-injection stub; production uses these same handler/helpers. */
typedef int esp_err_t;
typedef int nvs_handle_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG 1
#define ESP_ERR_INVALID_SIZE 2
#define ESP_ERR_NVS_NOT_FOUND 3
#define ESP_ERR_NVS_INVALID_LENGTH 4
static char stored[256];
static bool present;
static int writes, commits, fail_set, fail_commit, fail_read;
static esp_err_t nvs_get_str(nvs_handle_t h,const char *name,char *out,size_t *n) {
    assert(h==1 && !strcmp(name,"ts_auth_key"));
    if(fail_read)return ESP_FAIL;
    if(!present)return ESP_ERR_NVS_NOT_FOUND;
    size_t need=strlen(stored)+1;
    if(out && *n<need){*n=need;return ESP_ERR_NVS_INVALID_LENGTH;}
    if(out)memcpy(out,stored,need);
    *n=need;return ESP_OK;
}
static esp_err_t nvs_set_str(nvs_handle_t h,const char *name,const char *value) {
    assert(h==1 && !strcmp(name,"ts_auth_key"));writes++;
    if(fail_set){fail_set--;return ESP_FAIL;}
    assert(strlen(value)<sizeof(stored));strcpy(stored,value);present=true;return ESP_OK;
}
static esp_err_t nvs_erase_key(nvs_handle_t h,const char *name) {
    assert(h==1 && !strcmp(name,"ts_auth_key"));present=false;stored[0]=0;return ESP_OK;
}
static esp_err_t nvs_commit(nvs_handle_t h) {
    assert(h==1);commits++;
    if(fail_commit){fail_commit--;return ESP_FAIL;}
    return ESP_OK;
}
#include "setup_tailscale.h"
static esp_err_t save(const char *body,bool *saved) {
    cJSON *json=setup_parse_json(body,strlen(body));
    if(!json)return ESP_ERR_INVALID_ARG;
    esp_err_t err=setup_tailscale_save(1,json,saved);
    cJSON_Delete(json);return err;
}
int main(void) {
    bool saved=false;
    assert(!setup_tailscale_saved(1));
    char copy[256];memset(copy,'x',sizeof(copy));
    assert(setup_tailscale_copy(1,copy,sizeof(copy))==ESP_ERR_NVS_NOT_FOUND && copy[0]==0);
    assert(save("{}", &saved)==ESP_OK && !saved && writes==0);
    assert(save("{\"tailscale_auth_key\":\"\"}",&saved)==ESP_OK && writes==0);
    assert(save("{\"tailscale_auth_key\":\"DUMMY-FIRST\"}",&saved)==ESP_OK && saved && commits==1);
    assert(setup_tailscale_copy(1,copy,sizeof(copy))==ESP_OK && !strcmp(copy,"DUMMY-FIRST"));
    /* Restart uses only NVS, not the previous HTTP/in-memory result. */
    saved=false;assert(setup_tailscale_saved(1));
    assert(save("{}",&saved)==ESP_OK && saved && !strcmp(stored,"DUMMY-FIRST"));
    assert(save("{\"tailscale_auth_key\":\"\"}",&saved)==ESP_OK && !strcmp(stored,"DUMMY-FIRST"));
    const char *bad[]={"{","null","[]","{} trailing","{\"tailscale_auth_key\":null}",
        "{\"tailscale_auth_key\":5}","{\"tailscale_auth_key\":true}",
        "{\"tailscale_auth_key\":{}}","{\"tailscale_auth_key\":[]}",
        "{\"tailscale_auth_key\":\"DUMMY\\u0000TRUNCATED\"}",
        "{\"tailscale_auth_key\":\"DUMMY\",\"tailscale_auth_key\":\"DUMMY-OTHER\"}"};
    int before=writes;
    for(size_t i=0;i<sizeof(bad)/sizeof(bad[0]);i++) {
        assert(save(bad[i],&saved)!=ESP_OK);assert(writes==before);
    }
    char body[512],value[257];memset(value,'D',256);value[256]=0;
    snprintf(body,sizeof(body),"{\"tailscale_auth_key\":\"%s\"}",value);
    assert(save(body,&saved)==ESP_ERR_INVALID_ARG && writes==before);
    value[255]=0;snprintf(body,sizeof(body),"{\"tailscale_auth_key\":\"%s\"}",value);
    assert(save(body,&saved)==ESP_OK && strlen(stored)==255);
    assert(setup_tailscale_copy(1,copy,8)==ESP_ERR_NVS_INVALID_LENGTH && copy[0]==0);
    assert(setup_tailscale_copy(1,NULL,256)==ESP_ERR_INVALID_ARG);
    assert(save("{\"tailscale_auth_key\":\"DUMMY-OLD\"}",&saved)==ESP_OK);
    fail_set=1;
    assert(save("{\"tailscale_auth_key\":\"DUMMY-FAILED\"}",&saved)!=ESP_OK);
    assert(!strcmp(stored,"DUMMY-OLD") && saved);
    fail_commit=1;
    assert(save("{\"tailscale_auth_key\":\"DUMMY-FAILED\"}",&saved)!=ESP_OK);
    assert(!strcmp(stored,"DUMMY-OLD") && setup_tailscale_saved(1));
    fail_read=1;before=writes;
    assert(save("{\"tailscale_auth_key\":\"DUMMY-FAILED\"}",&saved)!=ESP_OK && writes==before);
    assert(save("{}",&saved)!=ESP_OK && saved);
    fail_read=0;present=false;stored[0]=0;saved=false;fail_commit=1;
    assert(save("{\"tailscale_auth_key\":\"DUMMY-FAILED\"}",&saved)!=ESP_OK && !saved && !present);
    assert(!setup_tailscale_saved(1));
    puts("PASS: real Tailscale handler/storage helpers, keep/replace, strict JSON, bounds, restart/read and write/commit fault rollback");
}