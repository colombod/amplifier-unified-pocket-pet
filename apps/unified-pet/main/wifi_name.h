/* The name of the Wi-Fi network the pet is associated with right now, and how
 * Settings words it. Pure: no SDK, no logging, no storage.
 *
 * The name is only ever taken from the station's own record of the access
 * point it is associated with. It is never the name that was typed in or
 * saved, so a network the pet has left, failed to join or is still joining is
 * never shown as the current one. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define WIFI_NAME_BYTES 32 /* an SSID is at most 32 bytes and need not be terminated */

/* name: 33 bytes. ap_ssid: the associated access point's SSID field, or NULL
 * when the station could not report one. Anything but "associated, and the
 * station reported it" leaves the name empty. */
static inline void wifi_name_update(char name[WIFI_NAME_BYTES+1],bool associated,const uint8_t *ap_ssid){
    memset(name,0,WIFI_NAME_BYTES+1);
    if(!associated||!ap_ssid)return;
    for(size_t i=0;i<WIFI_NAME_BYTES&&ap_ssid[i];i++)name[i]=(char)ap_ssid[i];
}
/* What the screen can draw: printable ASCII as it is; every other character
 * (one whole UTF-8 sequence, or one stray byte) becomes a single '?'. Returns
 * false, with out empty, when nothing readable is left - the caller then says
 * "connected" without a name instead of showing question marks. */
static inline bool wifi_name_display(char *out,size_t capacity,const char *name){
    size_t n=0;bool readable=false;
    if(!out||!capacity)return false;
    for(size_t i=0;name&&i<WIFI_NAME_BYTES&&name[i]&&n+1<capacity;){
        unsigned char c=(unsigned char)name[i];
        if(c>=0x20&&c<0x7f){out[n++]=(char)c;i++;if(c!=' ')readable=true;continue;}
        size_t extra=c>=0xf0?3:c>=0xe0?2:c>=0xc0?1:0;i++;
        while(extra--&&i<WIFI_NAME_BYTES&&((unsigned char)name[i]&0xc0)==0x80)i++;
        out[n++]='?';
    }
    if(!readable)n=0;
    out[n]=0;return readable;
}
/* The connection line of Settings. Fits "Wi-Fi: " and 32 characters in 40 bytes. */
static inline void wifi_name_line(char *out,size_t capacity,bool connected,bool joining,const char *name){
    char shown[WIFI_NAME_BYTES+1];
    if(connected&&wifi_name_display(shown,sizeof(shown),name))snprintf(out,capacity,"Wi-Fi: %s",shown);
    else snprintf(out,capacity,"%s",connected?"Wi-Fi connected":joining?"Wi-Fi connecting...":"Wi-Fi disconnected");
}
