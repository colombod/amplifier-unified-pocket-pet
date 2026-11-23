#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include "nvs.h"
#include "cJSON.h"
static unsigned legacy_writes,own_writes;
static int fail_read,fail_commit;
enum {SSID,PASSWORD,API_KEY,TS_KEY,SETUP_PASS,KEY_COUNT};
static const char *keys[KEY_COUNT]={"ssid","password","api_key","ts_auth_key","setup_pass"};
static const char *legacy_values[KEY_COUNT]={"SYNTHETIC-SSID","SYNTHETIC-WIFI",
 "SYNTHETIC-API","SYNTHETIC-TAIL","SYNTHETIC-PAIR"};
static bool own_present[KEY_COUNT];
static char own_value[KEY_COUNT][256];
static unsigned key_writes[KEY_COUNT],legacy_reads[KEY_COUNT];
static int key_index(const char *key) {
 for(int i=0;i<KEY_COUNT;i++)if(!strcmp(key,keys[i]))return i;
 assert(!"unexpected string storage key");return -1;
}
static unsigned char target_bytes[80];
static size_t target_length;
static uint8_t own_volume=255;
static unsigned api_reads;
static unsigned char bridge_bytes[160];
static size_t bridge_length;
static bool bridge_token_present,bridge_pending,disable_during_connect;
static esp_err_t bridge_token_error;
static char bridge_token_value[44];
static void on_wifi_connect(void);
esp_err_t nvs_open(const char *ns,int mode,nvs_handle_t *out) {
 *out=!strcmp(ns,"unified_pet")?3:!strcmp(ns,"pet_setup")?1:2;
 if(mode==NVS_READWRITE && *out!=3)legacy_writes++;
 return ESP_OK;
}
void nvs_close(nvs_handle_t h){(void)h;}
esp_err_t nvs_get_str(nvs_handle_t h,const char *k,char *out,size_t *n) {
 if(!strcmp(k,"bridge_tok")){
  assert(h==3);if(bridge_token_error)return bridge_token_error;
  if(!bridge_token_present)return ESP_ERR_NVS_NOT_FOUND;
  size_t need=strlen(bridge_token_value)+1;if(out&&*n<need)return ESP_ERR_NVS_INVALID_LENGTH;
  if(out)memcpy(out,bridge_token_value,need);*n=need;return ESP_OK;
 }
 int i=key_index(k);if(i==API_KEY)api_reads++;assert(h==1||h==3);if(fail_read&&h==3)return ESP_FAIL;
 if(h==3&&!own_present[i])return ESP_ERR_NVS_NOT_FOUND;
 if(h==1)legacy_reads[i]++;
 const char *s=h==3?own_value[i]:legacy_values[i];
 size_t need=strlen(s)+1;if(out&&*n<need)return ESP_ERR_NVS_INVALID_LENGTH;
 if(out)memcpy(out,s,need);*n=need;return ESP_OK;
}
esp_err_t nvs_set_str(nvs_handle_t h,const char *k,const char *s) {
 int i=key_index(k);if(h!=3){legacy_writes++;return ESP_FAIL;}own_writes++;key_writes[i]++;
 assert(strlen(s)<sizeof(own_value[i]));strcpy(own_value[i],s);own_present[i]=true;return ESP_OK;
}
esp_err_t nvs_erase_key(nvs_handle_t h,const char *k) {
 int i=key_index(k);assert(h==3);own_present[i]=false;return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t h) {
 assert(h==3);if(fail_commit){fail_commit--;return ESP_FAIL;}return ESP_OK;
}
esp_err_t nvs_get_blob(nvs_handle_t h,const char *k,void *out,size_t *n) {
 if(!strcmp(k,"bridge_pending")){assert(h==3);if(!bridge_pending)return ESP_ERR_NVS_NOT_FOUND;if(out&&*n)memset(out,1,*n);*n=1;return ESP_OK;}
 if(!strcmp(k,"bridge_cfg")){
  assert(h==3);if(!bridge_length)return ESP_ERR_NVS_NOT_FOUND;
  if(out&&*n<bridge_length)return ESP_ERR_NVS_INVALID_LENGTH;
  if(out)memcpy(out,bridge_bytes,bridge_length);*n=bridge_length;return ESP_OK;
 }
 assert(!strcmp(k,"unified_target"));if(fail_read&&h==3)return ESP_FAIL;
 if(h==3&&!target_length)return ESP_ERR_NVS_NOT_FOUND;
 if(h!=3)return ESP_ERR_NVS_NOT_FOUND;
 if(*n<target_length)return ESP_ERR_NVS_INVALID_LENGTH;
 memcpy(out,target_bytes,target_length);*n=target_length;return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t h,const char *k,const void *v,size_t n) {
 assert(!strcmp(k,"unified_target"));assert(h==3);assert(n<=sizeof(target_bytes));memcpy(target_bytes,v,n);target_length=n;own_writes++;return ESP_OK;
}
esp_err_t nvs_get_u8(nvs_handle_t h,const char *k,uint8_t *v) {
 assert(!strcmp(k,"volume"));assert(h==2||h==3);if(fail_read&&h==3)return ESP_FAIL;
 if(h==3&&own_volume==255)return ESP_ERR_NVS_NOT_FOUND;
 *v=h==3?own_volume:42;return ESP_OK;
}
esp_err_t nvs_set_u8(nvs_handle_t h,const char *k,uint8_t v) {
 assert(!strcmp(k,"volume"));assert(h==3);own_volume=v;own_writes++;return ESP_OK;
}
#ifdef BASELINE_OPEN
int main(void) {
 nvs_handle_t storage;
#include "storage-open.inc"
 assert(legacy_writes==0); /* Original production legacy write-open must fail RED. */
}
#else
#include "setup_storage.h"
#include "network.h"
static pet_store_t storage;
#include "storage_network_host.h"
#include "storage-production.inc"
static void on_wifi_connect(void){
 if(disable_during_connect){
  disable_during_connect=false;setup_bridge_t cfg;
  assert(bridge_length==sizeof(cfg));memcpy(&cfg,bridge_bytes,sizeof(cfg));
  cfg.enabled=false;memcpy(bridge_bytes,&cfg,sizeof(cfg));bridge_token_present=false;
 }
}
static esp_err_t save(pet_store_t *s,const char *body,bool *saved) {
 cJSON *j=setup_parse_json(body,strlen(body));assert(j);
 esp_err_t e=pet_store_tailscale_save(s,j,saved);cJSON_Delete(j);return e;
}
int main(void) {
 /* Real network boot: distinct legacy keys recognized, without copying any. */
 network_init();
 assert(credentials_present&&state.key_saved&&state.tailscale_key_saved);
 assert(!state.setup&&!strcmp(state.ap_password,legacy_values[SETUP_PASS]));
 assert(own_writes==0&&legacy_writes==0);
 for(int i=0;i<KEY_COUNT;i++)assert(!own_present[i]);
 assert(legacy_reads[SSID]&&legacy_reads[API_KEY]&&legacy_reads[TS_KEY]&&legacy_reads[SETUP_PASS]);
 command_t blank={.kind=1,.ssid="SYNTHETIC-NEW-SSID",.password="SYNTHETIC-NEW-WIFI"};
 strcpy(state.ssid,"SYNTHETIC-OLD-SSID");
 assert(join_network(&blank,true)); /* Actual production join/save body. */
 /* Nothing listed by the scan: one unpinned configuration, strongest by signal. */
 assert(station_scan_method==WIFI_ALL_CHANNEL_SCAN&&station_sort_method==WIFI_CONNECT_AP_BY_SIGNAL);
 assert(scan_calls==1&&station_history_n==1&&!station_history[0].pinned);
 /* Three access points listed: the strongest is pinned, whatever its position.
  * It turns the pet away once (a stale session) and takes it on the next try:
  * the pet insists on it and no other configuration is applied. */
 scan_found_n=3;
 scan_found[0]=(wifi_ap_record_t){.bssid={1,1,1,1,1,1},.primary=11,.rssi=-80};
 scan_found[1]=(wifi_ap_record_t){.bssid={2,2,2,2,2,2},.primary=1,.rssi=-50};
 scan_found[2]=(wifi_ap_record_t){.bssid={3,3,3,3,3,3},.primary=6,.rssi=-91};
 station_history_n=0;link_script_n=1;link_script_i=0;link_script[0]=LINK_LOST;
 assert(join_network(&blank,false));
 assert(station_history_n==1&&station_history[0].pinned&&station_history[0].channel==1);
 assert(!memcmp(station_history[0].bssid,scan_found[1].bssid,6)&&link_script_i==1);
 /* The pinned one never takes it: after JOIN_PINNED_ATTEMPTS any access point
  * is accepted, with one further unpinned configuration. */
 station_history_n=0;link_script_n=JOIN_PINNED_ATTEMPTS;link_script_i=0;
 for(unsigned i=0;i<JOIN_PINNED_ATTEMPTS;i++)link_script[i]=LINK_LOST;
 assert(join_network(&blank,false));
 assert(station_history_n==2&&station_history[0].pinned&&!station_history[1].pinned&&link_script_i==JOIN_PINNED_ATTEMPTS);
 /* Nothing takes it at all: bounded, and reported as not joined. */
 station_history_n=0;link_script_n=JOIN_PINNED_ATTEMPTS+JOIN_OPEN_ATTEMPTS;link_script_i=0;
 for(unsigned i=0;i<link_script_n;i++)link_script[i]=LINK_LOST;
 assert(!join_network(&blank,false));
 assert(station_history_n==2&&link_script_i==JOIN_PINNED_ATTEMPTS+JOIN_OPEN_ATTEMPTS);
 /* A failed scan is not an obstacle: unpinned, as when nothing is listed. */
 station_history_n=0;link_script_n=0;link_script_i=0;scan_result=ESP_FAIL;
 assert(join_network(&blank,false)&&station_history_n==1&&!station_history[0].pinned);
 scan_result=ESP_OK;scan_found_n=0;station_history_n=0;
 /* Joining names no network: not the one left, not the one typed in. */
 assert(!state.ssid[0]);
 assert(state.settings_saved&&state.key_saved&&state.connected);
 assert(own_present[SSID]&&own_present[PASSWORD]);
 assert(!strcmp(own_value[SSID],blank.ssid)&&!strcmp(own_value[PASSWORD],blank.password));
 assert(!own_present[API_KEY]&&key_writes[API_KEY]==0);
 assert(!own_present[TS_KEY]&&!own_present[SETUP_PASS]);
 assert(own_writes==2&&legacy_writes==0);
 char value[256];
 assert(network_copy_api_key(value,sizeof(value))==ESP_OK&&!strcmp(value,legacy_values[API_KEY]));
 /* Reboot must observe own Wi-Fi and the still-legacy effective OpenAI secret. */
 unsigned before=own_writes;memset(&state,0,sizeof(state));credentials_present=false;
 network_init();assert(credentials_present&&state.key_saved&&own_writes==before);
 assert(network_copy_api_key(value,sizeof(value))==ESP_OK&&!strcmp(value,legacy_values[API_KEY]));
 assert(!own_present[API_KEY]&&key_writes[API_KEY]==0&&legacy_writes==0);
 puts("PASS production storage boot/join-save/blank OpenAI/reboot: key-aware effective legacy secret retained, zero credential migration");
 pet_store_t s,a;assert(pet_store_open(&s,"pet_setup")==ESP_OK);
 assert(pet_store_open(&a,"pet_audio")==ESP_OK);assert(!legacy_writes);
 storage=s;
 size_t n=sizeof(value);
 assert(network_copy_api_key(value,sizeof(value))==ESP_OK&&!strcmp(value,"SYNTHETIC-API"));
 assert(network_copy_tailscale_auth_key(value,sizeof(value))==ESP_OK&&!strcmp(value,"SYNTHETIC-TAIL"));
 assert(pet_store_get_str(&s,"api_key",value,&n)==ESP_OK&&!strcmp(value,"SYNTHETIC-API"));
 bool saved=false;assert(save(&s,"{}",&saved)==ESP_OK&&saved&&own_writes==before);
 assert(save(&s,"{\"tailscale_auth_key\":\"\"}",&saved)==ESP_OK&&saved&&own_writes==before);
 assert(save(&s,"{\"tailscale_auth_key\":\"SYNTHETIC-OWN\"}",&saved)==ESP_OK&&saved);
 n=sizeof(value);assert(pet_store_get_str(&s,"ts_auth_key",value,&n)==ESP_OK&&!strcmp(value,"SYNTHETIC-OWN"));
 fail_commit=1;
 assert(save(&s,"{\"tailscale_auth_key\":\"SYNTHETIC-FAIL\"}",&saved)!=ESP_OK);
 assert(!strcmp(own_value[TS_KEY],"SYNTHETIC-OWN"));
 own_present[TS_KEY]=false;fail_commit=1;
 assert(save(&s,"{\"tailscale_auth_key\":\"SYNTHETIC-FAIL\"}",&saved)!=ESP_OK&&!own_present[TS_KEY]);
 fail_read=1;n=sizeof(value);
 assert(pet_store_get_str(&s,"api_key",value,&n)==ESP_FAIL);
 memset(value,'x',sizeof(value));
 assert(network_copy_api_key(value,sizeof(value))==ESP_FAIL&&value[0]==0);
 memset(value,'x',sizeof(value));
 assert(network_copy_tailscale_auth_key(value,sizeof(value))==ESP_FAIL&&value[0]==0);
 assert(save(&s,"{}",&saved)==ESP_FAIL);
 fail_read=0;
 setup_unified_target_t target;
 assert(network_copy_unified_target(&target)==ESP_ERR_NVS_NOT_FOUND);
 setup_unified_target_t invalid={.version=9};
 memcpy(target_bytes,&invalid,sizeof(invalid));target_length=sizeof(invalid);
 assert(pet_store_unified_copy(&s,&target)==ESP_ERR_INVALID_STATE);
 uint8_t volume;assert(pet_store_get_volume(&a,&volume)==ESP_OK&&volume==42);
 own_volume=101;assert(pet_store_get_volume(&a,&volume)==ESP_ERR_INVALID_STATE);
 own_volume=17;assert(pet_store_get_volume(&a,&volume)==ESP_OK&&volume==17);
 assert(legacy_writes==0);
 /* The actual boot/accessor/join helpers must share the validated pair state. */
 setup_bridge_t bridge_cfg={.version=1,.enabled=true,.ip={192,168,1,2},.port=8443};
 strcpy(bridge_cfg.fqdn,"unified-host.tail-example.ts.net");
 bridge_length=sizeof(bridge_cfg);assert(bridge_length<=sizeof(bridge_bytes));
 memcpy(bridge_bytes,&bridge_cfg,bridge_length);
 memset(bridge_token_value,'A',43);bridge_token_value[43]=0;bridge_token_present=true;
 memset(&state,0,sizeof(state));api_reads=0;network_init();
 assert(state.bridge_valid&&state.bridge_enabled&&state.bridge_token_saved&&!state.key_saved&&api_reads==0);
 memset(value,'x',sizeof(value));assert(network_copy_api_key(value,sizeof(value))==ESP_ERR_INVALID_STATE&&value[0]==0&&api_reads==0);
 /* Wi-Fi save was admitted in bridge mode; disable/clear during its real wait. */
 disable_during_connect=true;assert(join_network(&blank,true));
 assert(state.connected&&!state.joining&&state.bridge_valid&&!state.bridge_enabled&&!state.bridge_token_saved&&!state.settings_saved&&api_reads==0);
 /* Disabled + corrupt token and read-failed token both deny provider access. */
 bridge_token_present=true;strcpy(bridge_token_value,"corrupt");
 memset(&state,0,sizeof(state));network_init();assert(!state.bridge_valid&&!state.settings_saved&&api_reads==0);
 memset(value,'x',sizeof(value));assert(network_copy_api_key(value,sizeof(value))!=ESP_OK&&value[0]==0&&api_reads==0);
 bridge_token_error=ESP_FAIL;memset(&state,0,sizeof(state));network_init();
 assert(!state.bridge_valid&&!state.settings_saved&&api_reads==0);
 bridge_token_error=ESP_OK;bridge_token_present=false;bridge_pending=true;
 memset(&state,0,sizeof(state));network_init();assert(!state.bridge_valid&&!state.settings_saved&&api_reads==0);
 bridge_pending=false;memset(&state,0,sizeof(state));network_init();
 assert(state.bridge_valid&&!state.bridge_enabled&&state.key_saved&&api_reads>0);
 assert(legacy_writes==0);
 puts("PASS bridge boot/API-key fail-closed for enabled/corrupt/read-failed/pending pair; actual Wi-Fi completion cannot revive readiness after concurrent disable");
 memset(value,0,sizeof(value));memset(own_value,0,sizeof(own_value));
 puts("PASS storage compatibility: boot/blank/replace/commit failures/errors/invalid records, own first and no legacy writes");
}
#endif