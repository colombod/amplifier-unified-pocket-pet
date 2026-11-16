/* Include the real production flow; all input advancement uses its read callback. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "nvs.h"
#ifndef TOUCH_SOURCE
#define TOUCH_SOURCE "../components/pet_touch_calibration/touch_calibration.c"
#endif
#include TOUCH_SOURCE
static stored_map_t records[3];
static size_t lengths[3];
static bool exists[3];
static int writes[3],reads[3],fail_read_handle,fail_commit;
static lv_indev_data_t next;
esp_err_t nvs_open(const char *ns,int mode,nvs_handle_t *h) {
 *h=!strcmp(ns,"pet_touch")?1:2;
 assert(!strcmp(ns,"pet_touch")||!strcmp(ns,"unified_pet"));
 if(*h==1 && mode==NVS_READONLY && !exists[1])return ESP_ERR_NVS_NOT_FOUND;
 return ESP_OK;
}
void nvs_close(nvs_handle_t h){(void)h;}
esp_err_t nvs_get_blob(nvs_handle_t h,const char *key,void *out,size_t *n) {
 assert(!strcmp(key,"map"));reads[h]++;if(fail_read_handle==h)return ESP_FAIL;
 if(!exists[h])return ESP_ERR_NVS_NOT_FOUND;
 if(*n<lengths[h])return ESP_ERR_NVS_INVALID_LENGTH;
 memcpy(out,&records[h],lengths[h]);*n=lengths[h];return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t h,const char *key,const void *value,size_t n) {
 assert(!strcmp(key,"map"));assert(n==sizeof(stored_map_t));writes[h]++;
 memcpy(&records[h],value,n);lengths[h]=n;exists[h]=true;return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t h){(void)h;return fail_commit?ESP_FAIL:ESP_OK;}
static void raw(lv_indev_t *p,lv_indev_data_t *d){(void)p;*d=next;}
static lv_indev_t input;
static void init(void) {
 /* Model a fresh process with synthetic NVS retained. */
 collecting=result_visible=pressed=mapped=store_ready=false;
 input=(lv_indev_t){.read=raw,.mode=7};touch_calibration_init(&input,&stub_object);
}
static lv_indev_data_t read_at(int x,int y,int state) {
 next=(lv_indev_data_t){.point={x,y},.state=state};
 lv_indev_data_t got;input.read(&input,&got);return got;
}
static void tap(int x,int y) {
 read_at(x,y,LV_INDEV_STATE_PRESSED);stub_ticks+=10;
 read_at(x,y,LV_INDEV_STATE_RELEASED);
}
static void calibrate(void) {
 touch_calibration_begin();assert(input.mode==LV_INDEV_MODE_TIMER);
 tap(140,64);tap(140,304);tap(340,184);tap(224,184);
 assert(result_visible&&mapped);tap(0,0);assert(input.mode==7);
}
int main(void) {
 int own=1;
#ifdef TEST_APP_STORE
 own=2;
#endif
 init();assert(!mapped);lv_indev_data_t d=read_at(-99,999,1);
 assert(d.point.x==-99&&d.point.y==999); /* raw remains unchanged without map */
 records[1]=(stored_map_t){.version=1,.map={.b=1,.d=1}};
 exists[1]=true;lengths[1]=sizeof(stored_map_t);
 stored_map_t legacy=records[1];
 init();assert(mapped);d=read_at(300,120,1);assert(d.point.x==120&&d.point.y==300);
 d=read_at(999,-99,1);assert(d.point.x==0&&d.point.y==447);
 d=read_at(-99,999,1);assert(d.point.x==367&&d.point.y==0);
 calibrate();assert(writes[own]==1&&records[own].version==1);
 if(own==2){assert(writes[1]==0);assert(!memcmp(&legacy,&records[1],sizeof(legacy)));}
 init();assert(mapped);d=read_at(300,120,1);assert(d.point.x==120&&d.point.y==300);
 int legacy_reads=reads[1];
 records[own].version=9;init();assert(!mapped); /* invalid own never falls back */
 if(own==2)assert(reads[1]==legacy_reads);
 records[own].version=1;records[own].map.a=NAN;init();assert(!mapped);
 if(own==2)assert(reads[1]==legacy_reads);
 lengths[own]=4;init();assert(!mapped);
 if(own==2)assert(reads[1]==legacy_reads);
 /* App-only I/O fault with an independently readable valid legacy map. */
 records[1]=legacy;lengths[1]=sizeof(legacy);exists[1]=true;
 fail_read_handle=own;init();assert(!mapped);
 if(own==2) {
   assert(reads[1]==legacy_reads);
   stored_map_t readable;size_t n=sizeof(readable);
   assert(nvs_get_blob(1,"map",&readable,&n)==ESP_OK);
   assert(!memcmp(&readable,&legacy,sizeof(legacy)));
 }
 fail_read_handle=0;
 exists[1]=exists[2]=false;init();touch_calibration_begin();
 tap(100,100);tap(100,100);tap(100,100);assert(result_visible&&!mapped);
 touch_calibration_begin();read_at(100,100,1);stub_ticks+=1600;read_at(100,100,0);
 assert(result_visible&&!mapped); /* long press cancels */
 fail_commit=1;calibrate();assert(mapped);
 puts("PASS shared calibration: root/app store, legacy roundtrip, invalid/missing/error, degenerate/cancel, swapped axes and pressed clamps");
}