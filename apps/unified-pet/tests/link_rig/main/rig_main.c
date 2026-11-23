/* One mic-on of the production conversation owner, started exactly as the
 * parent voice worker starts it (same task priority and stack), against the
 * scripted host on the other side of the impaired link. */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "nvs.h"
#include "cJSON.h"
#include "audio_test.h"
#include "bridge_voice.h"
#include "rig.h"

#define SESSION_US 660000000LL /* live_voice.c BRIDGE_SESSION_LIMIT_MS */

static void owner(void *arg){
    (void)arg;
    vTaskDelay(pdMS_TO_TICKS(300)); /* interface and ARP settle; Wi-Fi is already associated on the device */
    setup_bridge_t cfg={.version=1,.enabled=1,.ip={192,168,77,1},.port=8941,.fqdn="pet-rig.example.test"};
    audio_stream_prepare();
    bridge_voice_run(&cfg,esp_timer_get_time()+SESSION_US,SESSION_US,rig_parent_stopped);
    rig_report();rig_link_report();rig_probe_report();
    printf("RIG done t=%lld\n",(long long)(esp_timer_get_time()/1000));
    fflush(stdout);_exit(0);
}
/* The user's one control: mic off, at a scripted moment. */
static void user(void *arg){
    long at=(long)(intptr_t)arg;
    while(!rig_listening_seen()||rig_since_listening_ms()<at)vTaskDelay(5);
    printf("RIG mic_off since_listening_ms=%lld\n",(long long)rig_since_listening_ms());
    rig_parent_stop();bridge_voice_stop();
    vTaskDelete(NULL);
}
/* Optional display load; the voice owner now runs above it on the device. */
static void display(void *arg){
    long busy_us=(long)(intptr_t)arg*100; /* percent of every 10 ms */
    for(;;){
        int64_t until=esp_timer_get_time()+busy_us;
        while(esp_timer_get_time()<until){}
        vTaskDelay(pdMS_TO_TICKS(10-busy_us/1000>0?10-busy_us/1000:1));
    }
}
/* A run that never ends is a rig failure, not a result. */
static void watchdog(void *arg){
    long limit=(long)(intptr_t)arg;
    vTaskDelay(pdMS_TO_TICKS(limit));
    printf("RIG watchdog t=%lld\n",(long long)(esp_timer_get_time()/1000));
    rig_report();rig_link_report();rig_probe_report();fflush(stdout);_exit(3);
}
void app_main(void){
    setvbuf(stdout,NULL,_IOLBF,0);
    rig_seams_init();
    if(esp_netif_init()!=ESP_OK||esp_event_loop_create_default()!=ESP_OK){fprintf(stderr,"RIG init failed\n");exit(2);}
    rig_link_start();
    bridge_voice_init();
    long off=rig_env("RIG_MIC_OFF_MS",0),load=rig_env("RIG_UI_LOAD_PCT",0);
    if(off>0)xTaskCreate(user,"rig_user",4096,(void*)(intptr_t)off,6,NULL);
    if(load>0)xTaskCreate(display,"rig_display",4096,(void*)(intptr_t)load,4,NULL);
    xTaskCreate(watchdog,"rig_watchdog",4096,(void*)(intptr_t)rig_env("RIG_WATCHDOG_MS",120000),24,NULL);
    xTaskCreatePinnedToCore(owner,"pet_live",12288,NULL,6,NULL,1); /* live_voice.c */
}
