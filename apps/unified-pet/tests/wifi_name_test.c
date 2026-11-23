/* The network name in Settings. Synthetic names only; no radio, no storage.
 * refresh_station() is the production body from main/network.c. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "wifi_name.h"

/* ---- the station, as the SDK presents it ---- */
#define ESP_OK 0
#define portMAX_DELAY 0
typedef struct {uint8_t bssid[6];uint8_t ssid[33];int8_t rssi;} wifi_ap_record_t;
typedef struct {bool connected,joining;int rssi;char ssid[33];} network_status_t;
static network_status_t state;
static int state_lock,locked,ap_calls;
static bool ap_available;
static wifi_ap_record_t ap_now;
static int esp_wifi_sta_get_ap_info(wifi_ap_record_t *out){
    ap_calls++;assert(!locked); /* the radio is never asked while the state lock is held */
    if(!ap_available)return 1;
    *out=ap_now;return ESP_OK;
}
static void xSemaphoreTake(int lock,int wait){(void)lock;(void)wait;assert(!locked);locked=1;}
static void xSemaphoreGive(int lock){(void)lock;assert(locked);locked=0;}
#include "station-refresh.inc"

static void associate(const char *name,size_t bytes,int rssi){
    memset(&ap_now,0xAA,sizeof(ap_now)); /* nothing around the name is zero */
    memset(ap_now.ssid,0,sizeof(ap_now.ssid));memcpy(ap_now.ssid,name,bytes);ap_now.rssi=(int8_t)rssi;ap_available=true;
}
static const char *line(void){
    static char out[40];
    wifi_name_line(out,sizeof(out),state.connected,state.joining,state.ssid);return out;
}
int main(void){
    char shown[33];
    /* Unavailable: never joined. The radio is not asked, nothing is named. */
    state=(network_status_t){.rssi=-127};
    refresh_station(false);
    assert(ap_calls==0&&!state.ssid[0]&&state.rssi==-127&&!strcmp(line(),"Wi-Fi disconnected"));
    state.joining=true;assert(!strcmp(line(),"Wi-Fi connecting..."));state.joining=false;

    /* Connected: the associated access point's own name and signal. */
    associate("Home Net",8,-60);state.connected=true;
    refresh_station(true);
    assert(!strcmp(state.ssid,"Home Net")&&state.rssi==-60&&!strcmp(line(),"Wi-Fi: Home Net"));

    /* Disconnect: the name goes at once; the last signal value is kept as before. */
    state.connected=false;ap_available=false;
    refresh_station(false);
    assert(!state.ssid[0]&&state.rssi==-60&&!strcmp(line(),"Wi-Fi disconnected"));
    /* Even a name left behind is never worded as current while not connected. */
    strcpy(state.ssid,"Home Net");
    assert(!strcmp(line(),"Wi-Fi disconnected"));
    state.joining=true;assert(!strcmp(line(),"Wi-Fi connecting..."));state.joining=false;

    /* Switch: the new access point's name, with nothing of the longer old one. */
    strcpy(state.ssid,"A much longer previous network!!");
    associate("Cafe",4,-71);state.connected=true;
    refresh_station(true);
    assert(!strcmp(state.ssid,"Cafe")&&state.ssid[5]==0&&state.ssid[31]==0&&state.rssi==-71&&!strcmp(line(),"Wi-Fi: Cafe"));

    /* Connected, and the station cannot report its access point: no name, not a stale one. */
    ap_available=false;
    refresh_station(true);
    assert(!state.ssid[0]&&state.rssi==-71&&!strcmp(line(),"Wi-Fi connected"));

    /* A full 32-byte name has no terminator in the record's 32 bytes. */
    const char full[]="0123456789abcdefghijklmnopqrstuv";
    assert(sizeof(full)==33);
    associate(full,32,-50);ap_now.ssid[32]='X'; /* whatever follows the name is not part of it */
    refresh_station(true);
    assert(!strcmp(state.ssid,full)&&strlen(state.ssid)==32);
    assert(strlen(line())==39&&!strcmp(line()+7,full));
    char small[12];wifi_name_line(small,sizeof(small),true,false,state.ssid);
    assert(!strcmp(small,"Wi-Fi: 0123")); /* a short buffer truncates, never overruns */

    /* Hidden or empty name: connected, without a name. */
    associate("",0,-50);refresh_station(true);
    assert(!state.ssid[0]&&!strcmp(line(),"Wi-Fi connected"));
    associate("   ",3,-50);refresh_station(true);
    assert(!strcmp(line(),"Wi-Fi connected"));

    /* Characters the screen cannot draw: one '?' per character, the rest kept. */
    associate("Caf\xc3\xa9 \xe2\x98\x95 5G",12,-50);refresh_station(true);
    assert(!strcmp(line(),"Wi-Fi: Caf? ? 5G"));
    associate("a\x01" "b\x7f" "c\x80" "d\xff",8,-50);refresh_station(true);
    assert(!strcmp(line(),"Wi-Fi: a?b?c?d?"));
    associate("Net\xf0\x9f\x8f\xa0",7,-50);refresh_station(true);
    assert(!strcmp(line(),"Wi-Fi: Net?"));
    associate("ab\xe2\x98",4,-50);refresh_station(true); /* a sequence cut off at the end */
    assert(!strcmp(line(),"Wi-Fi: ab?"));
    /* Nothing drawable at all: no row of question marks. */
    associate("\xe5\xae\xb6\xe5\xba\xad",6,-50);refresh_station(true);
    assert(state.ssid[0]&&!wifi_name_display(shown,sizeof(shown),state.ssid)&&!shown[0]);
    assert(!strcmp(line(),"Wi-Fi connected"));
    /* 32 bytes: fifteen two-byte characters, a lead byte with no continuation, and a
     * letter in the last byte. Read to the 32nd byte and not past it. */
    char wide[33];for(int i=0;i<30;i+=2){wide[i]=(char)0xc3;wide[i+1]=(char)0xa9;}wide[30]=(char)0xc3;wide[31]='z';wide[32]=0;
    associate(wide,32,-50);ap_now.ssid[32]=0xa9;refresh_station(true);
    assert(wifi_name_display(shown,sizeof(shown),state.ssid)&&!strcmp(shown,"????????????????z"));
    /* A lead byte in the very last position is one '?', with nothing read after it. */
    wide[31]=(char)0xe2;associate(wide,32,-50);ap_now.ssid[32]=0x98;refresh_station(true);
    assert(wifi_name_display(shown,sizeof(shown),state.ssid)==false&&!shown[0]);
    wide[0]='k';wide[1]='k';associate(wide,32,-50);refresh_station(true);
    /* k, k, fourteen two-byte characters and two lead bytes with nothing after them. */
    assert(wifi_name_display(shown,sizeof(shown),state.ssid)&&strlen(shown)==18&&!strncmp(shown,"kk",2));
    for(int i=2;i<18;i++)assert(shown[i]=='?');

    /* Formatting characters in a name are text, not a format. */
    associate("%s%n 100%",9,-50);refresh_station(true);
    assert(!strcmp(line(),"Wi-Fi: %s%n 100%"));
    assert(!locked);
    puts("wifi_name: ok");
    return 0;
}
