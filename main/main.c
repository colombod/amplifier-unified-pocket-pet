#include <stdio.h>
#include <stdatomic.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>
#include "esp_log.h"
#include "esp_app_desc.h"
#include "nvs_flash.h"
#include "bsp/esp-bsp.h"
#include "bsp/display.h"
#include "lvgl.h"
#include "network.h"
#include "audio_test.h"
#include "live_voice.h"
#include "touch_calibration.h"

#define FRAME_BYTES (192*208*2)
extern const uint8_t pet_pixels[] asm("_binary_tachikoma_rgb565_start");
static lv_image_dsc_t frames[73];
static lv_obj_t *pet, *title, *subtitle, *setup_panel, *qr, *wifi_details, *setup_message, *footer;
static lv_obj_t *audio_panel,*audio_message,*tone_button,*record_button,*audio_back,*volume_label;
static lv_obj_t *talk_button,*talk_label,*audio_open_button,*settings_panel,*settings_gear,*microphone_icon,*stop_icon;
static lv_obj_t *wifi_badge,*settings_connection,*settings_key;
static bool return_to_settings;
static unsigned tick_count=0, wave_ticks=0;
static _Atomic unsigned diag_ticks, diag_flush_start, diag_flush_return;
static void display_event(lv_event_t *event) {
    if(lv_event_get_code(event)==LV_EVENT_FLUSH_START) atomic_fetch_add(&diag_flush_start,1);
    if(lv_event_get_code(event)==LV_EVENT_FLUSH_FINISH) atomic_fetch_add(&diag_flush_return,1);
}
static void display_diagnostics(void *arg) {
    for(int i=0;i<4;i++) {
        vTaskDelay(pdMS_TO_TICKS(3000));
        ESP_LOGI("display_diag","ticks=%u flush_start=%u flush_return=%u",atomic_load(&diag_ticks),atomic_load(&diag_flush_start),atomic_load(&diag_flush_return));
    }
    vTaskDelete(NULL);
}
static bool was_setup=false, panel_requested=true;

static lv_obj_t *label(lv_obj_t *parent,const char *text,int width,int x,int y,const lv_font_t *font) {
    lv_obj_t *o=lv_label_create(parent);lv_label_set_text(o,text);lv_obj_set_width(o,width);
    lv_obj_set_style_text_font(o,font,0);lv_obj_set_style_text_align(o,LV_TEXT_ALIGN_CENTER,0);
    /* Card objects have their own theme foreground; do not inherit dark text. */
    lv_obj_set_style_text_color(o,lv_color_hex(0xf1f5f9),0);
    lv_obj_align(o,LV_ALIGN_TOP_MID,x,y);return o;
}
static void setup_clicked(lv_event_t *event){return_to_settings=true;lv_obj_add_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);panel_requested=true;network_open_setup();}
static void pet_clicked(lv_event_t *event){wave_ticks=24;}
static void talk_clicked(lv_event_t *event){live_status_t s;live_voice_snapshot(&s);if(s.active)live_voice_stop();else live_voice_start();}
static void settings_clicked(lv_event_t *event){lv_obj_clear_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);}
static void settings_closed(lv_event_t *event){lv_obj_add_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);}
static void align_touch_clicked(lv_event_t *event){touch_calibration_begin();}
static void audio_clicked(lv_event_t *event){lv_obj_add_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);lv_obj_clear_flag(audio_panel,LV_OBJ_FLAG_HIDDEN);}
static void audio_closed(lv_event_t *event){lv_obj_add_flag(audio_panel,LV_OBJ_FLAG_HIDDEN);lv_obj_clear_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);}
static void volume_released(lv_event_t *event){audio_test_save_volume();}
static void volume_changed(lv_event_t *event){
 int value=lv_slider_get_value(lv_event_get_target(event));audio_test_set_volume(value);
 lv_label_set_text_fmt(volume_label,"Volume %d%%",value);
}
static void tone_clicked(lv_event_t *event){audio_test_request(false);}
static void record_clicked(lv_event_t *event){audio_test_request(true);}
static void close_clicked(lv_event_t *event){panel_requested=false;network_close_setup();if(return_to_settings){return_to_settings=false;lv_obj_clear_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);}}
static lv_obj_t *button(lv_obj_t *parent,const char *text,int x,int y,int width,lv_event_cb_t cb) {
    lv_obj_t *o=lv_btn_create(parent);lv_obj_set_size(o,width,44);lv_obj_align(o,LV_ALIGN_BOTTOM_MID,x,y);
    lv_obj_set_style_radius(o,12,0);lv_obj_set_style_bg_color(o,lv_color_hex(0x38bdf8),0);
    lv_obj_set_ext_click_area(o,8);
    lv_obj_add_event_cb(o,cb,LV_EVENT_CLICKED,NULL);lv_obj_t *l=lv_label_create(o);
    lv_label_set_text(l,text);lv_obj_set_style_text_color(l,lv_color_hex(0x0f172a),0);lv_obj_center(l);return o;
}
static void refresh(lv_timer_t *timer) {
    atomic_fetch_add(&diag_ticks,1);
    audio_test_status_t a;audio_test_snapshot(&a);
    live_status_t voice;live_voice_snapshot(&voice);
    lv_label_set_text(talk_label,voice.active?"Stop listening":"Start voice");
    if(voice.active){lv_obj_add_flag(microphone_icon,LV_OBJ_FLAG_HIDDEN);lv_obj_clear_flag(stop_icon,LV_OBJ_FLAG_HIDDEN);lv_obj_add_state(settings_gear,LV_STATE_DISABLED);}
    else{lv_obj_clear_flag(microphone_icon,LV_OBJ_FLAG_HIDDEN);lv_obj_add_flag(stop_icon,LV_OBJ_FLAG_HIDDEN);lv_obj_remove_state(settings_gear,LV_STATE_DISABLED);}
    if(a.busy && !voice.active)lv_obj_add_state(talk_button,LV_STATE_DISABLED);
    else lv_obj_remove_state(talk_button,LV_STATE_DISABLED);
    lv_label_set_text(audio_message,a.message);
    lv_label_set_text_fmt(volume_label,"Volume %d%%",a.volume);
    if(a.busy){lv_obj_add_state(tone_button,LV_STATE_DISABLED);lv_obj_add_state(record_button,LV_STATE_DISABLED);lv_obj_add_state(audio_back,LV_STATE_DISABLED);}
    else{lv_obj_remove_state(tone_button,LV_STATE_DISABLED);lv_obj_remove_state(record_button,LV_STATE_DISABLED);lv_obj_remove_state(audio_back,LV_STATE_DISABLED);}
    network_status_t s;network_snapshot(&s);
    const char *signal=s.rssi>=-55?"excellent":s.rssi>=-67?"good":s.rssi>=-75?"fair":"weak";
    if(s.connected){lv_label_set_text_fmt(wifi_badge,LV_SYMBOL_WIFI "  %d dBm",s.rssi);lv_label_set_text_fmt(settings_connection,"Wi-Fi connected\nSignal %s (%d dBm)",signal,s.rssi);}
    else{lv_label_set_text(wifi_badge,LV_SYMBOL_WIFI "  Disconnected");lv_label_set_text(settings_connection,s.joining?"Wi-Fi connecting...":"Wi-Fi disconnected");}
    lv_obj_set_style_text_color(wifi_badge,lv_color_hex(s.connected?0x86efac:0xfbbf24),0);
    lv_label_set_text_fmt(settings_key,"OpenAI key: %s\nVoice: %s",s.key_saved?"saved":"required",voice.active?"session active":"disconnected");
    bool visible=s.setup&&panel_requested;
    if(visible!=was_setup) {
        was_setup=visible;
        if(visible){lv_obj_clear_flag(setup_panel,LV_OBJ_FLAG_HIDDEN);}
        else lv_obj_add_flag(setup_panel,LV_OBJ_FLAG_HIDDEN);
    }
    lv_label_set_text(subtitle,(voice.active || voice.state==LIVE_ERROR || s.connected)?voice.message:s.joining?"Connecting Wi-Fi...":"Let's get connected");
    if(s.setup) {
        char details[120];snprintf(details,sizeof(details),"%s\nPassword: %s\nThen open 192.168.4.1",s.ap_ssid,s.ap_password);
        lv_label_set_text(wifi_details,details);lv_label_set_text(setup_message,s.joining?"Connecting to your Wi-Fi...":s.connected&&s.settings_saved?"Saved. Tap Done.":s.connected?"Not saved. Check your phone.":"Finish setup on your phone");
        char payload[160];snprintf(payload,sizeof(payload),"WIFI:T:WPA;S:%s;P:%s;;",s.ap_ssid,s.ap_password);
        static char last_payload[160];
        if(strcmp(last_payload,payload)) {lv_qrcode_update(qr,payload,strlen(payload));strlcpy(last_payload,payload,sizeof(last_payload));}
    }
    unsigned frame;
    if(wave_ticks){frame=22+(24-wave_ticks)/6;--wave_ticks;}
    else if(s.joining || voice.state==LIVE_CONNECTING || voice.state==LIVE_THINKING) frame=45+(tick_count/2)%6;
    else frame=(tick_count/3)%6;
    lv_image_set_src(pet,&frames[frame]);tick_count++;
}
void app_main(void) {
    /* Never erase user settings as an automatic response to an NVS error. */
    ESP_ERROR_CHECK(nvs_flash_init());
    network_init();audio_tests_init();live_voice_init();
    lv_display_t *display=bsp_display_start();assert(display);
    ESP_ERROR_CHECK(bsp_display_backlight_on());
    bsp_display_lock(0);
    lv_display_add_event_cb(display,display_event,LV_EVENT_FLUSH_START,NULL);
    lv_display_add_event_cb(display,display_event,LV_EVENT_FLUSH_FINISH,NULL);
    lv_obj_t *screen=lv_screen_active();lv_obj_set_style_bg_color(screen,lv_color_hex(0x0f172a),0);
    lv_obj_set_style_text_color(screen,lv_color_hex(0xf1f5f9),0);lv_obj_clear_flag(screen,LV_OBJ_FLAG_SCROLLABLE);
    title=label(screen,"Tachikoma",350,0,12,&lv_font_montserrat_22);
    subtitle=label(screen,"Let's get connected",350,0,42,&lv_font_montserrat_16);
    wifi_badge=label(screen,"Wi-Fi",330,0,80,&lv_font_montserrat_12);
    for(unsigned i=0;i<73;i++) {
        frames[i].header.magic=LV_IMAGE_HEADER_MAGIC;frames[i].header.cf=LV_COLOR_FORMAT_RGB565;frames[i].header.stride=192*2;frames[i].header.w=192;frames[i].header.h=208;
        frames[i].data_size=FRAME_BYTES;frames[i].data=pet_pixels+i*FRAME_BYTES;
    }
    pet=lv_image_create(screen);lv_image_set_src(pet,&frames[0]);lv_image_set_scale(pet,300);lv_obj_align(pet,LV_ALIGN_CENTER,0,-10);
    lv_obj_add_flag(pet,LV_OBJ_FLAG_CLICKABLE);lv_obj_add_event_cb(pet,pet_clicked,LV_EVENT_CLICKED,NULL);
    settings_gear=button(screen,LV_SYMBOL_SETTINGS,0,0,42,settings_clicked);
    lv_obj_set_size(settings_gear,42,42);lv_obj_align(settings_gear,LV_ALIGN_TOP_RIGHT,-12,8);
    lv_obj_set_ext_click_area(settings_gear,15);
    lv_obj_set_style_bg_opa(settings_gear,LV_OPA_TRANSP,0);lv_obj_set_style_shadow_width(settings_gear,0,0);
    lv_obj_set_style_text_color(lv_obj_get_child(settings_gear,0),lv_color_hex(0xcbd5e1),0);
    talk_button=lv_button_create(screen);lv_obj_set_size(talk_button,64,64);lv_obj_align(talk_button,LV_ALIGN_BOTTOM_MID,0,-28);
    lv_obj_set_style_radius(talk_button,LV_RADIUS_CIRCLE,0);lv_obj_set_style_bg_color(talk_button,lv_color_hex(0xf1f5f9),0);
    lv_obj_set_style_pad_all(talk_button,0,0);lv_obj_set_style_shadow_width(talk_button,0,0);
    lv_obj_add_event_cb(talk_button,talk_clicked,LV_EVENT_CLICKED,NULL);
    lv_obj_set_ext_click_area(talk_button,12);
    microphone_icon=lv_obj_create(talk_button);lv_obj_set_size(microphone_icon,40,40);lv_obj_center(microphone_icon);
    lv_obj_remove_style_all(microphone_icon);lv_obj_set_size(microphone_icon,40,40);lv_obj_center(microphone_icon);
    lv_obj_remove_flag(microphone_icon,LV_OBJ_FLAG_CLICKABLE|LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t *capsule=lv_obj_create(microphone_icon);lv_obj_remove_style_all(capsule);lv_obj_set_size(capsule,10,18);lv_obj_align(capsule,LV_ALIGN_CENTER,0,-6);
    lv_obj_set_style_radius(capsule,5,0);lv_obj_set_style_bg_opa(capsule,LV_OPA_COVER,0);lv_obj_set_style_bg_color(capsule,lv_color_hex(0x0f172a),0);
    lv_obj_t *cup=lv_arc_create(microphone_icon);lv_obj_set_size(cup,24,24);lv_obj_align(cup,LV_ALIGN_CENTER,0,-3);
    lv_obj_set_style_arc_color(cup,lv_color_hex(0x0f172a),LV_PART_MAIN);lv_obj_set_style_arc_width(cup,2,LV_PART_MAIN);
    lv_arc_set_bg_angles(cup,0,180);lv_obj_set_style_arc_opa(cup,LV_OPA_TRANSP,LV_PART_INDICATOR);lv_obj_remove_style(cup,NULL,LV_PART_KNOB);
    lv_obj_remove_flag(cup,LV_OBJ_FLAG_CLICKABLE);
    lv_obj_t *stem=lv_obj_create(microphone_icon);lv_obj_remove_style_all(stem);lv_obj_set_size(stem,2,7);lv_obj_align(stem,LV_ALIGN_CENTER,0,11);
    lv_obj_set_style_bg_opa(stem,LV_OPA_COVER,0);lv_obj_set_style_bg_color(stem,lv_color_hex(0x0f172a),0);
    lv_obj_t *base=lv_obj_create(microphone_icon);lv_obj_remove_style_all(base);lv_obj_set_size(base,14,2);lv_obj_align(base,LV_ALIGN_CENTER,0,15);
    lv_obj_set_style_bg_opa(base,LV_OPA_COVER,0);lv_obj_set_style_bg_color(base,lv_color_hex(0x0f172a),0);
    /* Icon primitives must leave clicks with the parent voice button. */
    lv_obj_remove_flag(capsule,LV_OBJ_FLAG_CLICKABLE);lv_obj_remove_flag(stem,LV_OBJ_FLAG_CLICKABLE);lv_obj_remove_flag(base,LV_OBJ_FLAG_CLICKABLE);
    stop_icon=lv_obj_create(talk_button);lv_obj_remove_style_all(stop_icon);lv_obj_set_size(stop_icon,20,20);lv_obj_center(stop_icon);
    lv_obj_set_style_radius(stop_icon,3,0);lv_obj_set_style_bg_opa(stop_icon,LV_OPA_COVER,0);lv_obj_set_style_bg_color(stop_icon,lv_color_hex(0x0f172a),0);lv_obj_remove_flag(stop_icon,LV_OBJ_FLAG_CLICKABLE);
    talk_label=label(screen,"Start voice",300,0,426,&lv_font_montserrat_12);
    setup_panel=lv_obj_create(screen);lv_obj_set_size(setup_panel,368,448);lv_obj_center(setup_panel);
    lv_obj_set_style_bg_color(setup_panel,lv_color_hex(0x0f172a),0);lv_obj_set_style_border_width(setup_panel,0,0);
    lv_obj_set_style_text_color(setup_panel,lv_color_hex(0xf1f5f9),0);
    lv_obj_set_style_pad_all(setup_panel,0,0);lv_obj_clear_flag(setup_panel,LV_OBJ_FLAG_SCROLLABLE);
    label(setup_panel,"Connect your phone",350,0,14,&lv_font_montserrat_22);
    label(setup_panel,"Scan to join Tachikoma Wi-Fi",350,0,46,&lv_font_montserrat_16);
    char version[64];snprintf(version,sizeof(version),"Firmware %s",esp_app_get_description()->version);
    lv_obj_t *version_label=label(setup_panel,version,350,0,68,&lv_font_montserrat_14);
    lv_obj_set_style_text_color(version_label,lv_color_hex(0x8fe1ff),0);
    /* 4-module quiet zone around the Wi-Fi QR code. */
    lv_obj_t *quiet=lv_obj_create(setup_panel);lv_obj_set_size(quiet,204,204);lv_obj_align(quiet,LV_ALIGN_TOP_MID,0,86);
    lv_obj_set_style_bg_color(quiet,lv_color_white(),0);lv_obj_set_style_radius(quiet,5,0);lv_obj_set_style_border_width(quiet,0,0);lv_obj_set_style_pad_all(quiet,0,0);lv_obj_clear_flag(quiet,LV_OBJ_FLAG_SCROLLABLE);
    qr=lv_qrcode_create(quiet);lv_qrcode_set_size(qr,176);lv_qrcode_set_dark_color(qr,lv_color_black());lv_qrcode_set_light_color(qr,lv_color_white());lv_obj_center(qr);
    wifi_details=label(setup_panel,"",350,0,298,&lv_font_montserrat_16);
    setup_message=label(setup_panel,"",350,0,372,&lv_font_montserrat_16);
    button(setup_panel,"Back / Done",0,-10,240,close_clicked);
    lv_obj_add_flag(setup_panel,LV_OBJ_FLAG_HIDDEN);
    audio_panel=lv_obj_create(screen);lv_obj_set_size(audio_panel,368,448);lv_obj_center(audio_panel);
    lv_obj_set_style_bg_color(audio_panel,lv_color_hex(0x0f172a),0);lv_obj_set_style_text_color(audio_panel,lv_color_hex(0xf1f5f9),0);
    lv_obj_set_style_border_width(audio_panel,0,0);lv_obj_set_style_pad_all(audio_panel,0,0);lv_obj_clear_flag(audio_panel,LV_OBJ_FLAG_SCROLLABLE);
    label(audio_panel,"Audio check",340,0,20,&lv_font_montserrat_22);
    label(audio_panel,"First check the speaker. Then record a short clip and hear it back.",320,0,62,&lv_font_montserrat_16);
    volume_label=label(audio_panel,"Volume 60%",340,0,114,&lv_font_montserrat_16);
    lv_obj_t *volume_slider=lv_slider_create(audio_panel);lv_obj_set_size(volume_slider,278,16);lv_obj_align(volume_slider,LV_ALIGN_TOP_MID,0,149);
    audio_test_status_t initial_audio;audio_test_snapshot(&initial_audio);
    lv_slider_set_range(volume_slider,0,100);lv_slider_set_value(volume_slider,initial_audio.volume,LV_ANIM_OFF);
    lv_obj_set_style_bg_color(volume_slider,lv_color_hex(0x38bdf8),LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(volume_slider,lv_color_hex(0x8fe1ff),LV_PART_KNOB);
    lv_obj_add_event_cb(volume_slider,volume_changed,LV_EVENT_VALUE_CHANGED,NULL);
    lv_obj_add_event_cb(volume_slider,volume_released,LV_EVENT_RELEASED,NULL);
    tone_button=button(audio_panel,"1. Speaker tone",0,0,260,tone_clicked);lv_obj_align(tone_button,LV_ALIGN_TOP_MID,0,181);
    record_button=button(audio_panel,"2. Record 3 seconds",0,0,260,record_clicked);lv_obj_align(record_button,LV_ALIGN_TOP_MID,0,241);
    audio_message=label(audio_panel,"",336,0,303,&lv_font_montserrat_18);
    label(audio_panel,"Only records when you tap.\nNothing is saved or uploaded.",340,0,356,&lv_font_montserrat_12);
    audio_back=button(audio_panel,"Back to settings",0,-10,260,audio_closed);
    lv_obj_add_flag(audio_panel,LV_OBJ_FLAG_HIDDEN);
    settings_panel=lv_obj_create(screen);lv_obj_set_size(settings_panel,368,448);lv_obj_center(settings_panel);
    lv_obj_set_style_bg_color(settings_panel,lv_color_hex(0x0f172a),0);lv_obj_set_style_border_width(settings_panel,0,0);
    lv_obj_set_style_pad_all(settings_panel,0,0);lv_obj_remove_flag(settings_panel,LV_OBJ_FLAG_SCROLLABLE);
    label(settings_panel,"Settings",340,0,24,&lv_font_montserrat_22);
    settings_connection=label(settings_panel,"",330,0,66,&lv_font_montserrat_16);
    settings_key=label(settings_panel,"",330,0,114,&lv_font_montserrat_14);
    footer=button(settings_panel,"Wi-Fi & OpenAI key",0,0,280,setup_clicked);lv_obj_align(footer,LV_ALIGN_TOP_MID,0,168);
    audio_open_button=button(settings_panel,"Audio / volume",0,0,280,audio_clicked);lv_obj_align(audio_open_button,LV_ALIGN_TOP_MID,0,230);
    lv_obj_t *alignment=button(settings_panel,"Touch alignment",0,0,280,align_touch_clicked);lv_obj_align(alignment,LV_ALIGN_TOP_MID,0,292);
    label(settings_panel,version,340,0,345,&lv_font_montserrat_14);
    button(settings_panel,"Back to Tachikoma",0,-20,280,settings_closed);
    lv_obj_add_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);
    touch_calibration_init(bsp_display_get_input_dev(),screen);
    lv_timer_create(refresh,120,NULL);refresh(NULL);
    bsp_display_unlock();
    xTaskCreate(display_diagnostics,"display_diag",3072,NULL,2,NULL);
    ESP_LOGI("pet","Tachikoma UI ready; voice starts only on Talk. No Unified connector in this isolated test.");
}
