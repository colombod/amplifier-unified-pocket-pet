#include "network.h"
#include <stdio.h>
#include <assert.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_log.h"
#include "esp_app_desc.h"
#include "esp_http_server.h"
#include "esp_mac.h"
#include "nvs.h"
#include "cJSON.h"
#include "lwip/sockets.h"
#include "lwip/inet.h"
#include "setup_address.h"

#define IP_READY BIT0
#define LINK_LOST BIT1
static network_status_t state;
static SemaphoreHandle_t state_lock;
static EventGroupHandle_t events;
static QueueHandle_t commands;
static httpd_handle_t server;
static nvs_handle_t storage;
static char token[33];
static bool credentials_present;
extern const unsigned char setup_html_start[] asm("_binary_setup_html_start");
extern const unsigned char setup_html_end[] asm("_binary_setup_html_end");
typedef struct { int kind; char ssid[33], password[64], key[256]; } command_t;
static void message(const char *text) {
    xSemaphoreTake(state_lock,portMAX_DELAY);
    strlcpy(state.message,text,sizeof(state.message));
    xSemaphoreGive(state_lock);
}
void network_snapshot(network_status_t *out) {
    xSemaphoreTake(state_lock,portMAX_DELAY); *out=state; xSemaphoreGive(state_lock);
}
esp_err_t network_copy_api_key(char *out,size_t capacity) {
    if(!out || capacity<2)return ESP_ERR_INVALID_ARG;
    memset(out,0,capacity);
    size_t length=capacity;
    return nvs_get_str(storage,"api_key",out,&length);
}
static void wifi_event(void *ctx, esp_event_base_t base, int32_t id, void *data) {
    if(base==IP_EVENT && id==IP_EVENT_STA_GOT_IP) {
        ESP_LOGI("pet_net","Station acquired IP");
        xEventGroupClearBits(events,LINK_LOST); xEventGroupSetBits(events,IP_READY);
    } else if(base==WIFI_EVENT && id==WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *event=data;
        ESP_LOGW("pet_net","Station disconnected: reason=%u",event->reason);
        xEventGroupClearBits(events,IP_READY); xEventGroupSetBits(events,LINK_LOST);
    }
}
/* Setup endpoints are reachable only through the device's temporary AP. */
static bool ap_request(httpd_req_t *req) {
    struct sockaddr_storage local={0}; socklen_t size=sizeof(local);
    if(getsockname(httpd_req_to_sockfd(req),(struct sockaddr*)&local,&size)!=0 ||
       !setup_address_is_ap((const struct sockaddr *)&local,size)) {
        httpd_resp_send_err(req,HTTPD_403_FORBIDDEN,"Open setup using the device Wi-Fi"); return false;
    }
    httpd_resp_set_hdr(req,"Cache-Control","no-store");
    httpd_resp_set_hdr(req,"X-Content-Type-Options","nosniff");
    httpd_resp_set_hdr(req,"Content-Security-Policy","default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'");
    return true;
}
static bool mutation_allowed(httpd_req_t *req) {
    if(!ap_request(req)) return false;
    char supplied[40]={0};
    if(httpd_req_get_hdr_value_str(req,"X-Setup-Token",supplied,sizeof(supplied))!=ESP_OK || strcmp(supplied,token)) {
        httpd_resp_send_err(req,HTTPD_403_FORBIDDEN,"Reload the setup page"); return false;
    }
    return true;
}
static esp_err_t json_reply(httpd_req_t *req,cJSON *body) {
    char *text=cJSON_PrintUnformatted(body); cJSON_Delete(body);
    if(!text) return httpd_resp_send_err(req,HTTPD_500_INTERNAL_SERVER_ERROR,"Out of memory");
    httpd_resp_set_type(req,"application/json");
    esp_err_t err=httpd_resp_sendstr(req,text); free(text); return err;
}
static esp_err_t page(httpd_req_t *req) {
    if(!ap_request(req)) return ESP_OK;
    char host[80]={0}; httpd_req_get_hdr_value_str(req,"Host",host,sizeof(host));
    if(strcmp(host,"192.168.4.1") && strcmp(host,"192.168.4.1:80")) {
        httpd_resp_set_status(req,"302 Found"); httpd_resp_set_hdr(req,"Location","http://192.168.4.1/");
        return httpd_resp_sendstr(req,"Open Tachikoma setup at http://192.168.4.1/");
    }
    httpd_resp_set_type(req,"text/html; charset=utf-8");
    /* Render the installed version even before the phone's status fetch works. */
    const char marker[]="__FIRMWARE_VERSION__";
    const char *start=(const char *)setup_html_start;
    size_t length=setup_html_end-setup_html_start;
    const char *where=memmem(start,length,marker,sizeof(marker)-1);
    if(where) {
        esp_err_t err=httpd_resp_send_chunk(req,start,where-start);
        if(err==ESP_OK) err=httpd_resp_sendstr_chunk(req,esp_app_get_description()->version);
        const char *after=where+sizeof(marker)-1;
        if(err==ESP_OK) err=httpd_resp_send_chunk(req,after,length-(after-start));
        if(err==ESP_OK) err=httpd_resp_send_chunk(req,NULL,0);
        return err;
    }
    return httpd_resp_send(req,(const char*)setup_html_start,setup_html_end-setup_html_start);
}
static esp_err_t status_get(httpd_req_t *req) {
    if(!ap_request(req)) return ESP_OK;
    network_status_t s; network_snapshot(&s);
    cJSON *o=cJSON_CreateObject();
    cJSON_AddStringToObject(o,"firmware_version",esp_app_get_description()->version);
    cJSON_AddBoolToObject(o,"connected",s.connected);
    cJSON_AddBoolToObject(o,"joining",s.joining);
    cJSON_AddBoolToObject(o,"key_saved",s.key_saved);
    cJSON_AddBoolToObject(o,"settings_saved",s.settings_saved);
    cJSON_AddNumberToObject(o,"configure_generation",s.configure_generation);
    cJSON_AddNumberToObject(o,"rssi",s.rssi);
    cJSON_AddStringToObject(o,"ssid",s.ssid);
    cJSON_AddStringToObject(o,"message",s.message);
    cJSON_AddStringToObject(o,"token",token);
    return json_reply(req,o);
}
static esp_err_t scan_get(httpd_req_t *req) {
    if(!ap_request(req)) return ESP_OK;
    network_status_t s; network_snapshot(&s);
    if(s.joining) return httpd_resp_send_err(req,HTTPD_400_BAD_REQUEST,"Connection in progress");
    wifi_scan_config_t scan={.show_hidden=false};
    if(esp_wifi_scan_start(&scan,true)!=ESP_OK)
        return httpd_resp_send_err(req,HTTPD_500_INTERNAL_SERVER_ERROR,"Try scanning again");
    uint16_t n=20; wifi_ap_record_t *records=calloc(n,sizeof(*records));
    if(!records) { esp_wifi_clear_ap_list(); return httpd_resp_send_err(req,HTTPD_500_INTERNAL_SERVER_ERROR,"Out of memory"); }
    esp_err_t err=esp_wifi_scan_get_ap_records(&n,records);
    cJSON *list=cJSON_CreateArray();
    for(int i=0; err==ESP_OK && i<n; ++i) {
        if(!records[i].ssid[0]) continue;
        bool duplicate=false;
        for(int j=0;j<i;j++) if(!strcmp((char*)records[j].ssid,(char*)records[i].ssid)) duplicate=true;
        if(duplicate) continue;
        cJSON *o=cJSON_CreateObject(); char name[33]; memcpy(name,records[i].ssid,32); name[32]=0;
        cJSON_AddStringToObject(o,"ssid",name);
        cJSON_AddNumberToObject(o,"rssi",records[i].rssi);
        cJSON_AddBoolToObject(o,"open",records[i].authmode==WIFI_AUTH_OPEN);
        cJSON_AddBoolToObject(o,"enterprise",records[i].authmode==WIFI_AUTH_WPA2_ENTERPRISE);
        cJSON_AddItemToArray(list,o);
    }
    free(records); return json_reply(req,list);
}
static esp_err_t configure(httpd_req_t *req) {
    if(!mutation_allowed(req)) return ESP_OK;
    if(req->content_len<=0 || req->content_len>1600)
        return httpd_resp_send_err(req,HTTPD_400_BAD_REQUEST,"Invalid form size");
    char *body=calloc(1,req->content_len+1); if(!body) return ESP_ERR_NO_MEM;
    int have=0;
    while(have<req->content_len) {
        int n=httpd_req_recv(req,body+have,req->content_len-have);
        if(n<=0) { free(body); return ESP_FAIL; } have+=n;
    }
    cJSON *json=cJSON_ParseWithLength(body,have); memset(body,0,have); free(body);
    cJSON *ssid=cJSON_GetObjectItemCaseSensitive(json,"ssid"), *pass=cJSON_GetObjectItemCaseSensitive(json,"password"), *key=cJSON_GetObjectItemCaseSensitive(json,"key");
    network_status_t saved; network_snapshot(&saved);
    bool valid=cJSON_IsString(ssid)&&cJSON_IsString(pass)&&cJSON_IsString(key);
    valid=valid && strlen(ssid->valuestring)>0 && strlen(ssid->valuestring)<=32 &&
        strlen(pass->valuestring)<=63 && (!pass->valuestring[0] || strlen(pass->valuestring)>=8) &&
        strlen(key->valuestring)<256 && ((!key->valuestring[0] && saved.key_saved) ||
        (strlen(key->valuestring)>=20 && !strncmp(key->valuestring,"sk-",3) && !strpbrk(key->valuestring," \t\r\n")));
    if(!valid) { cJSON_Delete(json); return httpd_resp_send_err(req,HTTPD_400_BAD_REQUEST,"Check network name, password (8–63 bytes), and required OpenAI API key"); }
    network_status_t s; network_snapshot(&s);
    if(s.joining) { cJSON_Delete(json); return httpd_resp_send_err(req,HTTPD_400_BAD_REQUEST,"Already connecting"); }
    command_t cmd={.kind=1};
    strlcpy(cmd.ssid,ssid->valuestring,sizeof(cmd.ssid)); strlcpy(cmd.password,pass->valuestring,sizeof(cmd.password)); strlcpy(cmd.key,key->valuestring,sizeof(cmd.key));
    /* Wipe the parsed secret strings before returning their allocations. */
    memset(pass->valuestring,0,strlen(pass->valuestring)); memset(key->valuestring,0,strlen(key->valuestring)); cJSON_Delete(json);
    xSemaphoreTake(state_lock,portMAX_DELAY);state.joining=true;state.settings_saved=false;state.configure_generation++;xSemaphoreGive(state_lock);
    message("Settings received. Connecting to Wi-Fi...");
    ESP_LOGI("pet_net","Configuration request accepted");
    BaseType_t queued=xQueueSend(commands,&cmd,0); memset(&cmd,0,sizeof(cmd));
    if(queued!=pdTRUE) {xSemaphoreTake(state_lock,portMAX_DELAY);state.joining=false;xSemaphoreGive(state_lock);}
    if(queued!=pdTRUE) return httpd_resp_send_err(req,HTTPD_500_INTERNAL_SERVER_ERROR,"Busy; try again");
    return httpd_resp_sendstr(req,"{}");
}
static esp_err_t finish(httpd_req_t *req) {
    if(!mutation_allowed(req)) return ESP_OK;
    network_status_t s; network_snapshot(&s);
    if(!s.connected || !s.settings_saved) return httpd_resp_send_err(req,HTTPD_400_BAD_REQUEST,"Wait until Wi-Fi is connected and settings are saved");
    command_t cmd={.kind=2};
    if(xQueueSend(commands,&cmd,0)!=pdTRUE) return httpd_resp_send_err(req,HTTPD_500_INTERNAL_SERVER_ERROR,"Try again");
    return httpd_resp_sendstr(req,"{}");
}
static void start_server(void) {
    httpd_config_t cfg=HTTPD_DEFAULT_CONFIG(); cfg.uri_match_fn=httpd_uri_match_wildcard;
    cfg.stack_size=8192; cfg.max_uri_handlers=6; cfg.lru_purge_enable=true;
    cfg.max_open_sockets=4;cfg.recv_wait_timeout=4;cfg.send_wait_timeout=4;
    ESP_ERROR_CHECK(httpd_start(&server,&cfg));
    httpd_uri_t routes[]={
        {.uri="/status",.method=HTTP_GET,.handler=status_get},
        {.uri="/networks",.method=HTTP_GET,.handler=scan_get},
        {.uri="/configure",.method=HTTP_POST,.handler=configure},
        {.uri="/finish",.method=HTTP_POST,.handler=finish},
        {.uri="/*",.method=HTTP_GET,.handler=page}};
    for(size_t i=0;i<sizeof(routes)/sizeof(routes[0]);i++) ESP_ERROR_CHECK(httpd_register_uri_handler(server,&routes[i]));
}
/* Small captive DNS responder; answer IPv4 questions, retain the original question. */
static void dns_task(void *arg) {
    int fd=socket(AF_INET,SOCK_DGRAM,IPPROTO_UDP);
    if(fd<0) { vTaskDelete(NULL); return; }
    struct sockaddr_in addr={.sin_family=AF_INET,.sin_port=htons(53),.sin_addr.s_addr=inet_addr("192.168.4.1")};
    if(bind(fd,(struct sockaddr*)&addr,sizeof(addr))!=0) { close(fd); vTaskDelete(NULL); return; }
    unsigned char b[512];
    for(;;) {
        struct sockaddr_in peer; socklen_t len=sizeof(peer); int n=recvfrom(fd,b,sizeof(b)-16,0,(struct sockaddr*)&peer,&len);
        if(n<12 || b[4]!=0 || b[5]!=1 || (b[2]&0x80)) continue;
        int end=12;
        while(end<n && b[end]) { int label=b[end]; if(label>63 || end+label+1>=n) {end=n;break;} end+=label+1; }
        if(end+5>n) continue;
        int q=end+1; bool ipv4=b[q]==0 && b[q+1]==1 && b[q+2]==0 && b[q+3]==1;
        n=q+4; b[2]=0x81; b[3]=0x80; b[6]=0;b[7]=ipv4?1:0; memset(b+8,0,4);
        if(ipv4) { const unsigned char answer[]={0xc0,0x0c,0,1,0,1,0,0,0,0,0,4,192,168,4,1};memcpy(b+n,answer,16);n+=16; }
        sendto(fd,b,n,0,(struct sockaddr*)&peer,len);
    }
}
static void open_ap(void) {
    wifi_mode_t mode;ESP_ERROR_CHECK(esp_wifi_get_mode(&mode));
    if(mode!=WIFI_MODE_APSTA) ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    xSemaphoreTake(state_lock,portMAX_DELAY); state.setup=true; xSemaphoreGive(state_lock);
}
static void join_network(const command_t *cmd,bool save) {
    xSemaphoreTake(state_lock,portMAX_DELAY); state.joining=true; state.connected=false; xSemaphoreGive(state_lock);
    message("Connecting to your Wi-Fi...");
    xEventGroupClearBits(events,IP_READY|LINK_LOST);
    if(esp_wifi_disconnect()==ESP_OK) xEventGroupWaitBits(events,LINK_LOST,pdFALSE,pdFALSE,pdMS_TO_TICKS(600));
    wifi_config_t cfg={0}; memcpy(cfg.sta.ssid,cmd->ssid,strlen(cmd->ssid)); memcpy(cfg.sta.password,cmd->password,strlen(cmd->password));
    cfg.sta.pmf_cfg.capable=true; cfg.sta.pmf_cfg.required=false;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA,&cfg)); memset(&cfg,0,sizeof(cfg));
    bool ok=false;
    for(int attempt=0;attempt<3&&!ok;attempt++) {
        xEventGroupClearBits(events,IP_READY|LINK_LOST);
        if(esp_wifi_connect()!=ESP_OK) break;
        EventBits_t bits=xEventGroupWaitBits(events,IP_READY|LINK_LOST,pdFALSE,pdFALSE,pdMS_TO_TICKS(12000));
        ok=(bits&IP_READY)!=0;
        if(!ok) {esp_wifi_disconnect();vTaskDelay(pdMS_TO_TICKS(500));}
    }
    if(ok && save) {
        esp_err_t err=nvs_set_str(storage,"ssid",cmd->ssid);
        if(err==ESP_OK) err=nvs_set_str(storage,"password",cmd->password);
        if(err==ESP_OK && cmd->key[0]) err=nvs_set_str(storage,"api_key",cmd->key);
        if(err==ESP_OK) err=nvs_commit(storage);
        if(err!=ESP_OK) {
            char info[96];snprintf(info,sizeof(info),"Wi-Fi connected; saving failed (%s). Keep setup open and retry.",esp_err_to_name(err));message(info);
            ESP_LOGE("pet_net","Credential save failed: %s",esp_err_to_name(err));open_ap();
        }
        else {
            credentials_present=true;
            ESP_LOGI("pet_net","Settings committed");
            xSemaphoreTake(state_lock,portMAX_DELAY); if(cmd->key[0]) state.key_saved=true;state.settings_saved=true; xSemaphoreGive(state_lock);
            message("Connected! Your settings are saved.");
        }
    } else if(ok) {
        xSemaphoreTake(state_lock,portMAX_DELAY);state.settings_saved=credentials_present&&state.key_saved;xSemaphoreGive(state_lock);
        message("Wi-Fi connected");
    }
    else { message("Could not connect. Check password or use a 2.4 GHz network."); open_ap(); }
    xSemaphoreTake(state_lock,portMAX_DELAY); state.joining=false; state.connected=ok; if(ok) strlcpy(state.ssid,cmd->ssid,sizeof(state.ssid)); xSemaphoreGive(state_lock);
}
static void worker(void *arg) {
    if(credentials_present) {
        command_t cmd={.kind=1};size_t n=sizeof(cmd.ssid);nvs_get_str(storage,"ssid",cmd.ssid,&n);n=sizeof(cmd.password);nvs_get_str(storage,"password",cmd.password,&n);
        join_network(&cmd,false);memset(&cmd,0,sizeof(cmd));
    }
    for(;;) {
        command_t cmd={0};
        if(xQueueReceive(commands,&cmd,pdMS_TO_TICKS(1000))==pdTRUE) {
            if(cmd.kind==0) open_ap();
            else if(cmd.kind==1) {
                /* Allow HTTP ACK/TCP delivery before AP+STA channel switching. */
                vTaskDelay(pdMS_TO_TICKS(1000));
                open_ap();join_network(&cmd,true);
            }
            else if(cmd.kind==2) {
                vTaskDelay(pdMS_TO_TICKS(1800));
                network_status_t s;network_snapshot(&s);
                if(s.connected && s.settings_saved) {esp_wifi_set_mode(WIFI_MODE_STA);xSemaphoreTake(state_lock,portMAX_DELAY);state.setup=false;xSemaphoreGive(state_lock);}
            }
            memset(&cmd,0,sizeof(cmd));
        }
        network_status_t s;network_snapshot(&s);
        wifi_ap_record_t ap_info;
        if(s.connected && esp_wifi_sta_get_ap_info(&ap_info)==ESP_OK) {
            xSemaphoreTake(state_lock,portMAX_DELAY);state.rssi=ap_info.rssi;xSemaphoreGive(state_lock);
        }
        if(s.connected && !(xEventGroupGetBits(events)&IP_READY)) {
            xSemaphoreTake(state_lock,portMAX_DELAY);state.connected=false;xSemaphoreGive(state_lock);
            message("Reconnecting to saved Wi-Fi...");
            command_t retry={.kind=1};size_t n=sizeof(retry.ssid);
            esp_err_t err=nvs_get_str(storage,"ssid",retry.ssid,&n);n=sizeof(retry.password);
            if(err==ESP_OK) err=nvs_get_str(storage,"password",retry.password,&n);
            if(err==ESP_OK) join_network(&retry,false); else open_ap();
            memset(&retry,0,sizeof(retry));
        }
    }
}
void network_close_setup(void) {command_t cmd={.kind=2};xQueueSend(commands,&cmd,0);}
void network_open_setup(void) {command_t cmd={.kind=0};xQueueSend(commands,&cmd,0);}
void network_init(void) {
    state.rssi=-127;
    state_lock=xSemaphoreCreateMutex();events=xEventGroupCreate();commands=xQueueCreate(2,sizeof(command_t));
    assert(state_lock&&events&&commands);
    ESP_ERROR_CHECK(nvs_open("pet_setup",NVS_READWRITE,&storage));
    size_t n=0; credentials_present=nvs_get_str(storage,"ssid",NULL,&n)==ESP_OK&&n>1;
    state.key_saved=nvs_get_str(storage,"api_key",NULL,&n)==ESP_OK;
    uint8_t mac[6];ESP_ERROR_CHECK(esp_read_mac(mac,ESP_MAC_WIFI_SOFTAP));
    snprintf(state.ap_ssid,sizeof(state.ap_ssid),"Tachikoma-%02X%02X-Setup",mac[4],mac[5]);
    /* Keep pairing credentials stable across power/USB resets. No values are logged. */
    n=sizeof(state.ap_password);
    if(nvs_get_str(storage,"setup_pass",state.ap_password,&n)!=ESP_OK || strlen(state.ap_password)<8) {
        snprintf(state.ap_password,sizeof(state.ap_password),"%08lx%04lx",(unsigned long)esp_random(),(unsigned long)(esp_random()&0xffff));
        esp_err_t saved=nvs_set_str(storage,"setup_pass",state.ap_password);
        if(saved==ESP_OK) saved=nvs_commit(storage);
        if(saved!=ESP_OK) {
            ESP_LOGW("pet_net","Pairing password persistence failed: %s",esp_err_to_name(saved));
            /* A unique name avoids stale iPhone password reuse if storage is full. */
            snprintf(state.ap_ssid,sizeof(state.ap_ssid),"Tachikoma-%02X%02X-%04lx",mac[4],mac[5],(unsigned long)(esp_random()&0xffff));
        }
    }
    nvs_stats_t stats;
    if(nvs_get_stats(NULL,&stats)==ESP_OK) ESP_LOGI("pet_net","NVS entries free=%u used=%u",(unsigned)stats.free_entries,(unsigned)stats.used_entries);
    snprintf(token,sizeof(token),"%08lx%08lx%08lx%08lx",(unsigned long)esp_random(),(unsigned long)esp_random(),(unsigned long)esp_random(),(unsigned long)esp_random());
    ESP_ERROR_CHECK(esp_netif_init());ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();esp_netif_t *ap_netif=esp_netif_create_default_wifi_ap();
    ESP_ERROR_CHECK(esp_netif_dhcps_stop(ap_netif));
    const char *portal="http://192.168.4.1/";
    ESP_ERROR_CHECK(esp_netif_dhcps_option(ap_netif,ESP_NETIF_OP_SET,ESP_NETIF_CAPTIVEPORTAL_URI,(void*)portal,strlen(portal)));
    ESP_ERROR_CHECK(esp_netif_dhcps_start(ap_netif));
    wifi_init_config_t init=WIFI_INIT_CONFIG_DEFAULT();ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,wifi_event,NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,wifi_event,NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    wifi_config_t ap={0};strlcpy((char*)ap.ap.ssid,state.ap_ssid,sizeof(ap.ap.ssid));ap.ap.ssid_len=strlen(state.ap_ssid);
    strlcpy((char*)ap.ap.password,state.ap_password,sizeof(ap.ap.password));ap.ap.authmode=WIFI_AUTH_WPA2_PSK;ap.ap.max_connection=2;ap.ap.channel=1;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP,&ap));
    if(credentials_present && state.key_saved) ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    state.setup=!(credentials_present && state.key_saved);message(credentials_present?"Connecting to saved Wi-Fi...":"Let's connect Tachikoma");
    start_server();xTaskCreate(dns_task,"setup_dns",3072,NULL,3,NULL);xTaskCreate(worker,"network",6144,NULL,4,NULL);
}
