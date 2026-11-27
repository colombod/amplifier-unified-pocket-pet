/* Hardware/RTOS-only stubs: boot and join/save decisions are extracted production. */
#include <stdint.h>
#include <stdlib.h>
typedef unsigned EventBits_t;
static int state_lock,events,commands;
static bool credentials_present;
static network_status_t state;
static char token[33];
typedef struct {int kind;char ssid[33],password[65],key[256];} command_t;
typedef struct {int unused;} esp_netif_t;
typedef struct {int unused;} wifi_init_config_t;
typedef struct {
 struct {unsigned char ssid[33],password[65];struct {bool capable,required;} pmf_cfg;int scan_method,sort_method;
         unsigned char bssid[6];bool bssid_set;unsigned char channel;} sta;
 struct {unsigned char ssid[33],password[65];size_t ssid_len;int authmode,max_connection,channel;} ap;
} wifi_config_t;
typedef struct {unsigned free_entries,used_entries;} nvs_stats_t;
#define ESP_ERROR_CHECK(e) assert((e)==ESP_OK)
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define portMAX_DELAY 0
#define IP_READY 1u
#define LINK_LOST 2u
#define pdFALSE 0
#define pdTRUE 1
#define pdMS_TO_TICKS(n) (n)
#define WIFI_IF_STA 1
#define WIFI_IF_AP 2
#define ESP_MAC_WIFI_SOFTAP 0
#define ESP_NETIF_OP_SET 0
#define ESP_NETIF_CAPTIVEPORTAL_URI 0
#define WIFI_STORAGE_RAM 0
#define WIFI_EVENT 0
#define ESP_EVENT_ANY_ID 0
#define IP_EVENT 0
#define IP_EVENT_STA_GOT_IP 0
#define WIFI_MODE_APSTA 0
#define WIFI_MODE_STA 1
#define WIFI_AUTH_WPA2_PSK 0
#define WIFI_INIT_CONFIG_DEFAULT() ((wifi_init_config_t){0})
static int xSemaphoreCreateMutex(void){return 1;}
static int xEventGroupCreate(void){return 1;}
static int xQueueCreate(int n,size_t size){(void)n;(void)size;return 1;}
static int xSemaphoreTake(int h,int wait){(void)h;(void)wait;return pdTRUE;}
static void xSemaphoreGive(int h){(void)h;}
static void xEventGroupClearBits(int h,unsigned bits){(void)h;(void)bits;}
/* Scripted outcomes of the join waits, oldest first; IP_READY once exhausted. */
static unsigned link_script[8],link_script_n,link_script_i;
static EventBits_t xEventGroupWaitBits(int h,unsigned bits,int clear,int all,int wait) {
 (void)h;(void)clear;(void)all;(void)wait;
 if((bits&IP_READY)&&link_script_i<link_script_n)return link_script[link_script_i++];
 return IP_READY;
}
static void vTaskDelay(int ticks){(void)ticks;}
static void message(const char *s){strlcpy(state.message,s,sizeof(state.message));}
static const char *esp_err_to_name(int e){(void)e;return "synthetic error";}
static void open_ap(void){state.setup=true;}
static int esp_wifi_disconnect(void){return ESP_OK;}
static int esp_wifi_connect(void){on_wifi_connect();return ESP_OK;}
#define WIFI_FAST_SCAN 0
#define WIFI_ALL_CHANNEL_SCAN 1
#define WIFI_CONNECT_AP_BY_SIGNAL 0
#define WIFI_CONNECT_AP_BY_SECURITY 1
typedef struct {unsigned char *ssid;bool show_hidden;} wifi_scan_config_t;
typedef struct {unsigned char bssid[6],ssid[33],primary;signed char rssi;} wifi_ap_record_t;
/* Scripted scan: what the station sees of the network it is asked to join. */
static wifi_ap_record_t scan_found[8];static unsigned scan_found_n,scan_calls;static int scan_result=ESP_OK;
static int esp_wifi_scan_start(const wifi_scan_config_t *cfg,bool block){assert(cfg&&cfg->ssid&&block);scan_calls++;return scan_result;}
static int esp_wifi_scan_get_ap_records(uint16_t *count,wifi_ap_record_t *out){
 unsigned n=scan_found_n<*count?scan_found_n:*count;memcpy(out,scan_found,n*sizeof(*out));*count=(uint16_t)n;return ESP_OK;
}
static int station_scan_method=-1,station_sort_method=-1;
/* Every station configuration applied, in order: pinned or not, and to what. */
static struct {bool pinned;unsigned char bssid[6],channel;} station_history[8];static unsigned station_history_n;
static int esp_wifi_set_config(int iface,const wifi_config_t *cfg){
 if(iface==WIFI_IF_STA){
  station_scan_method=cfg->sta.scan_method;station_sort_method=cfg->sta.sort_method;
  assert(station_history_n<8);
  station_history[station_history_n].pinned=cfg->sta.bssid_set;station_history[station_history_n].channel=cfg->sta.channel;
  memcpy(station_history[station_history_n++].bssid,cfg->sta.bssid,6);
 }
 return ESP_OK;
}
static int esp_read_mac(uint8_t *mac,int kind){(void)kind;memset(mac,0,6);return ESP_OK;}
static unsigned long esp_random(void){return 0x1234;}
static int nvs_get_stats(const char *p,nvs_stats_t *s){(void)p;memset(s,0,sizeof(*s));return ESP_OK;}
static int esp_netif_init(void){return ESP_OK;}
static int esp_event_loop_create_default(void){return ESP_OK;}
static esp_netif_t netif;
static esp_netif_t *esp_netif_create_default_wifi_sta(void){return &netif;}
static esp_netif_t *esp_netif_create_default_wifi_ap(void){return &netif;}
static int esp_netif_dhcps_stop(esp_netif_t *p){(void)p;return ESP_OK;}
static int esp_netif_dhcps_start(esp_netif_t *p){(void)p;return ESP_OK;}
static int esp_netif_dhcps_option(esp_netif_t *p,int op,int kind,void *s,size_t n) {
 (void)p;(void)op;(void)kind;(void)s;(void)n;return ESP_OK;
}
static int esp_wifi_init(const wifi_init_config_t *cfg){(void)cfg;return ESP_OK;}
static int esp_wifi_set_storage(int kind){assert(kind==WIFI_STORAGE_RAM);return ESP_OK;}
static void wifi_event(void){}
static int esp_event_handler_register(int base,int id,void(*fn)(void),void *arg){
 (void)base;(void)id;(void)fn;(void)arg;return ESP_OK;
}
static int esp_wifi_set_mode(int mode){(void)mode;return ESP_OK;}
static int esp_wifi_start(void){return ESP_OK;}
static int start_server(void){return ESP_OK;}
static void dns_task(void *p){(void)p;}
static void worker(void *p){(void)p;}
/* The boot storage fixture does not run worker tasks or power admission. */
bool network_activity_begin(void){return true;}
#define pdPASS 1
static int xTaskCreate(void(*fn)(void*),const char *n,int stack,void *p,int priority,void *out) {
 (void)fn;(void)n;(void)stack;(void)p;(void)priority;(void)out;return 1;
}