#include <stdio.h>
#include <stdatomic.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>
#include "esp_log.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "nvs_flash.h"
#include "bsp/esp-bsp.h"
#include "bsp/display.h"
#include "esp_lcd_panel_ops.h"
#include "lvgl.h"
#include "network.h"
#include "wifi_name.h"
#include "audio_test.h"
#include "live_voice.h"
#include "touch_calibration.h"
#include "tailnet_spike.h"
#include "device_power.h"
#include "power_input.h"

#define FRAME_BYTES (192*208*2)
extern const uint8_t pet_pixels[] asm("_binary_amplifier_assistant_rgb565_start");
static lv_image_dsc_t frames[73];
static lv_obj_t *pet, *title, *subtitle, *setup_panel, *qr, *wifi_details, *setup_message, *footer;
static lv_obj_t *audio_panel,*audio_back,*volume_label;
static lv_obj_t *talk_button,*talk_label,*audio_open_button,*settings_panel,*settings_gear,*microphone_icon,*mute_slash;
static lv_obj_t *wifi_badge,*settings_connection,*settings_signal,*settings_key;
static lv_obj_t *back_button;
static lv_obj_t *calibration_panel;
static lv_obj_t *power_panel,*power_title,*power_message,*power_cancel,*power_confirm;
static power_action_t power_action;
static bool power_running;
static power_touch_t power_touch;
static bool return_to_settings;
static unsigned tick_count=0, wave_ticks=0;
static bool was_setup=false, panel_requested=true;
/* INPUT_DIAG_BEGIN */
/* Diagnostic only: BSP output before calibration, mapped before LVGL rotation.
 * No sample history. Release coordinates are stale: retain last DOWN + its ID.
 * All tuples/counters are copied under this short mux; never call LVGL,
 * upstream callbacks or logging while holding it. */
typedef struct {
    unsigned active,chain,task,read_in;
    unsigned raw_n,raw_down,raw_up,raw_state,raw_id;
    int raw_x,raw_y;
    unsigned map_n,map_down,map_up,map_state,map_id;
    int map_x,map_y;
    unsigned event_n,event_code,target,event_id;
    int point_x,point_y;
    unsigned back_pressed,back_released,back_clicked,close_in,close_out;
    unsigned loop_in,loop_out,flush_start,flush_return,hidden,requested;
} input_diag_t;
static input_diag_t input_diag={.active=1,.task=1};
static portMUX_TYPE input_diag_mux=portMUX_INITIALIZER_UNLOCKED;
static lv_indev_read_cb_t saved_bsp,saved_cal;
static lv_indev_t *diag_input;
static bool diag_active(void) {
    portENTER_CRITICAL(&input_diag_mux);
    bool active=input_diag.active;
    portEXIT_CRITICAL(&input_diag_mux);
    return active;
}
static void diag_mark(unsigned *counter) {
    portENTER_CRITICAL(&input_diag_mux);
    if(input_diag.active)++*counter;
    portEXIT_CRITICAL(&input_diag_mux);
}
static void diag_raw(lv_indev_t *input,lv_indev_data_t *data) {
    saved_bsp(input,data);
    portENTER_CRITICAL(&input_diag_mux);
    if(input_diag.active) {
        input_diag.raw_n++;
        input_diag.raw_state=data->state==LV_INDEV_STATE_PRESSED;
        if(input_diag.raw_state) {
            input_diag.raw_down++;input_diag.raw_id=input_diag.raw_n;
            input_diag.raw_x=data->point.x;input_diag.raw_y=data->point.y;
        } else input_diag.raw_up++;
    }
    portEXIT_CRITICAL(&input_diag_mux);
}
static void diag_mapped(lv_indev_t *input,lv_indev_data_t *data) {
    diag_mark(&input_diag.read_in);
    saved_cal(input,data);
    portENTER_CRITICAL(&input_diag_mux);
    if(input_diag.active) {
        input_diag.map_n++;
        input_diag.map_state=data->state==LV_INDEV_STATE_PRESSED;
        if(input_diag.map_state) {
            input_diag.map_down++;input_diag.map_id=input_diag.map_n;
            input_diag.map_x=data->point.x;input_diag.map_y=data->point.y;
        } else input_diag.map_up++;
    }
    portEXIT_CRITICAL(&input_diag_mux);
}
static unsigned diag_target(lv_obj_t *object) {
    if(!object)return 0;
    if(object==back_button)return 1;
    if(object==settings_gear)return 2;
    if(object==footer)return 3;
    if(object==setup_panel)return 4;
    return 5;
}
static unsigned diag_event_code(lv_event_t *event) {
    lv_event_code_t code=lv_event_get_code(event);
    return code==LV_EVENT_PRESSED?1:code==LV_EVENT_RELEASED?2:code==LV_EVENT_CLICKED?3:0;
}
static void diag_indev_event(lv_event_t *event) {
    if(!diag_active())return;
    unsigned code=diag_event_code(event);if(!code)return;
    /* Pinned LVGL 9.3 sends the routed object as param; event target is indev.
     * This is LVGL's processed point, not another hardware read. */
    unsigned target=diag_target(lv_event_get_param(event));
    lv_point_t point;lv_indev_get_point(diag_input,&point);
    portENTER_CRITICAL(&input_diag_mux);
    if(input_diag.active) {
        input_diag.event_n++;input_diag.event_code=code;input_diag.target=target;
        input_diag.event_id=input_diag.map_n;
        input_diag.point_x=point.x;input_diag.point_y=point.y;
    }
    portEXIT_CRITICAL(&input_diag_mux);
}
static void diag_back_event(lv_event_t *event) {
    if(!diag_active())return;
    unsigned code=diag_event_code(event);
    if(code==1)diag_mark(&input_diag.back_pressed);
    if(code==2)diag_mark(&input_diag.back_released);
    if(code==3)diag_mark(&input_diag.back_clicked);
}
static void diag_visibility(bool hidden,bool requested) {
    portENTER_CRITICAL(&input_diag_mux);
    if(input_diag.active){input_diag.hidden=hidden;input_diag.requested=requested;}
    portEXIT_CRITICAL(&input_diag_mux);
}
static void display_event(lv_event_t *event) {
    if(lv_event_get_code(event)==LV_EVENT_FLUSH_START)diag_mark(&input_diag.flush_start);
    if(lv_event_get_code(event)==LV_EVENT_FLUSH_FINISH)diag_mark(&input_diag.flush_return);
}
static void diag_emit(unsigned seq) {
    int64_t ms=esp_timer_get_time()/1000;
    portENTER_CRITICAL(&input_diag_mux);
    input_diag_t d=input_diag;
    portEXIT_CRITICAL(&input_diag_mux);
    /* Fixed versioned numeric-only schema: never labels, network state bodies,
     * pointer values, SSIDs, secrets, audio, transcripts or provider content. */
    ESP_LOGI("pet_input_diag","{\"v\":1,\"seq\":%u,\"ms\":%lld,\"active\":%u,\"chain\":%u,\"task\":%u,"
        "\"read_in\":%u,\"raw_n\":%u,\"raw_down\":%u,\"raw_up\":%u,\"raw_state\":%u,\"raw_id\":%u,\"raw_x\":%d,\"raw_y\":%d,"
        "\"map_n\":%u,\"map_down\":%u,\"map_up\":%u,\"map_state\":%u,\"map_id\":%u,\"map_x\":%d,\"map_y\":%d,"
        "\"event_n\":%u,\"event_code\":%u,\"target\":%u,\"event_id\":%u,\"point_x\":%d,\"point_y\":%d,"
        "\"back_pressed\":%u,\"back_released\":%u,\"back_clicked\":%u,\"close_in\":%u,\"close_out\":%u,"
        "\"loop_in\":%u,\"loop_out\":%u,\"flush_start\":%u,\"flush_return\":%u,\"hidden\":%u,\"requested\":%u}",
        seq,(long long)ms,d.active,d.chain,d.task,d.read_in,d.raw_n,d.raw_down,d.raw_up,d.raw_state,d.raw_id,d.raw_x,d.raw_y,
        d.map_n,d.map_down,d.map_up,d.map_state,d.map_id,d.map_x,d.map_y,d.event_n,d.event_code,d.target,d.event_id,d.point_x,d.point_y,
        d.back_pressed,d.back_released,d.back_clicked,d.close_in,d.close_out,d.loop_in,d.loop_out,d.flush_start,d.flush_return,d.hidden,d.requested);
}
static void display_diagnostics(void *arg) {
    (void)arg;
    for(unsigned seq=1;seq<=1200;seq++) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        bool expired=esp_timer_get_time()>=1200000000LL || seq==1200;
        if(expired) {
            portENTER_CRITICAL(&input_diag_mux);input_diag.active=0;portEXIT_CRITICAL(&input_diag_mux);
        }
        diag_emit(seq);
        if(expired)break;
    }
    vTaskDelete(NULL);
}
static void diag_install(lv_obj_t *screen) {
    /* Called once while already holding the BSP display lock. Do not change
     * userdata, driverdata, input mode, IRQ/poll policy, or read data flags. */
    diag_input=bsp_display_get_input_dev();
    saved_bsp=diag_input?lv_indev_get_read_cb(diag_input):NULL;
    portENTER_CRITICAL(&input_diag_mux);input_diag.chain=0;portEXIT_CRITICAL(&input_diag_mux);
    if(!saved_bsp)return; /* log missing chain; do not invoke null calibration */
    lv_indev_set_read_cb(diag_input,diag_raw);
    touch_calibration_init(diag_input,screen);
    saved_cal=lv_indev_get_read_cb(diag_input);
    if(!saved_cal){lv_indev_set_read_cb(diag_input,diag_raw);return;}
    lv_indev_set_read_cb(diag_input,diag_mapped);
    lv_indev_add_event_cb(diag_input,diag_indev_event,LV_EVENT_ALL,NULL);
    portENTER_CRITICAL(&input_diag_mux);input_diag.chain=2;portEXIT_CRITICAL(&input_diag_mux);
}
static void diag_start(void) {
    if(xTaskCreate(display_diagnostics,"input_diag",3072,NULL,2,NULL)!=pdPASS) {
        portENTER_CRITICAL(&input_diag_mux);input_diag.task=0;input_diag.active=0;portEXIT_CRITICAL(&input_diag_mux);
        diag_emit(0); /* explicit allocation failure, no silent heartbeat claim */
    }
}
/* INPUT_DIAG_END */

static lv_obj_t *label(lv_obj_t *parent,const char *text,int width,int x,int y,const lv_font_t *font) {
    lv_obj_t *o=lv_label_create(parent);lv_label_set_text(o,text);lv_obj_set_width(o,width);
    lv_obj_set_style_text_font(o,font,0);lv_obj_set_style_text_align(o,LV_TEXT_ALIGN_CENTER,0);
    /* Card objects have their own theme foreground; do not inherit dark text. */
    lv_obj_set_style_text_color(o,lv_color_hex(0xf1f5f9),0);
    lv_obj_align(o,LV_ALIGN_TOP_MID,x,y);return o;
}
static void setup_clicked(lv_event_t *event){return_to_settings=true;lv_obj_add_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);panel_requested=true;network_open_setup();}
static void pet_clicked(lv_event_t *event){wave_ticks=24;}
/* The one voice control. Mic on: a running spoken conversation. Mic off: the
 * call ends. Only this tap ever turns the mic on; while it is on, a lost host
 * connection is re-established without another tap, and this tap ends that too. */
static void talk_clicked(lv_event_t *event){
    (void)event;live_status_t s;live_voice_snapshot(&s);
    if(s.active)live_voice_stop();
    else live_voice_start();
}
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
static void close_clicked(lv_event_t *event){
    diag_mark(&input_diag.close_in);
    panel_requested=false;network_close_setup();if(return_to_settings){return_to_settings=false;lv_obj_clear_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);}
    diag_mark(&input_diag.close_out);
}
static lv_obj_t *button(lv_obj_t *parent,const char *text,int x,int y,int width,lv_event_cb_t cb) {
    lv_obj_t *o=lv_btn_create(parent);lv_obj_set_size(o,width,44);lv_obj_align(o,LV_ALIGN_BOTTOM_MID,x,y);
    lv_obj_set_style_radius(o,12,0);lv_obj_set_style_bg_color(o,lv_color_hex(0x38bdf8),0);
    lv_obj_set_ext_click_area(o,8);
    lv_obj_add_event_cb(o,cb,LV_EVENT_CLICKED,NULL);lv_obj_t *l=lv_label_create(o);
    lv_label_set_text(l,text);lv_obj_set_style_text_color(l,lv_color_hex(0x0f172a),0);lv_obj_center(l);return o;
}
static void power_close(lv_event_t *event) {
    (void)event;
    if(power_running)return;
    power_action=POWER_NONE;
    power_touch=POWER_TOUCH_WAIT;
    lv_indev_reset(diag_input,NULL);
    lv_obj_add_flag(power_panel,LV_OBJ_FLAG_HIDDEN);
    device_power_block_keys(false);
}
static void power_notice(const char *heading,const char *message) {
    power_action=POWER_NONE;
    power_touch=POWER_TOUCH_WAIT;
    lv_label_set_text(power_title,heading);
    lv_label_set_text(power_message,message);
    lv_label_set_text(lv_obj_get_child(power_cancel,0),"OK");
    lv_obj_align(power_cancel,LV_ALIGN_BOTTOM_MID,0,-174);
    lv_obj_remove_flag(power_cancel,LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(power_confirm,LV_OBJ_FLAG_HIDDEN);
}
static bool power_touch_ready(void) {
    return calibration_panel && lv_obj_has_flag(calibration_panel,LV_OBJ_FLAG_HIDDEN) &&
        lv_obj_has_flag(audio_panel,LV_OBJ_FLAG_HIDDEN);
}
static void power_request(power_action_t action) {
    if(!power_touch_ready() || !lv_obj_has_flag(power_panel,LV_OBJ_FLAG_HIDDEN))return;
    device_power_block_keys(true);
    lv_obj_remove_flag(power_panel,LV_OBJ_FLAG_HIDDEN);
    if(action==POWER_OFF && !device_power_can_shutdown()) {
        power_notice("Shut down unavailable","The power controller is unavailable. The pet is still on.");
        return;
    }
    power_action=action;
    power_touch=POWER_TOUCH_WAIT;
    lv_indev_reset(diag_input,NULL);
    lv_label_set_text(power_title,action==POWER_RESTART?"Restart the pet?":"Shut down the pet?");
    lv_label_set_text(power_message,"Saved settings will be kept.");
    lv_label_set_text(lv_obj_get_child(power_cancel,0),"Cancel");
    lv_label_set_text(lv_obj_get_child(power_confirm,0),action==POWER_RESTART?"Restart":"Shut down");
    lv_obj_align(power_cancel,LV_ALIGN_BOTTOM_MID,-80,-174);
    lv_obj_remove_flag(power_cancel,LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(power_confirm,LV_OBJ_FLAG_HIDDEN);
}
static void power_touch_read(lv_indev_t *input,lv_indev_data_t *data) {
    diag_mapped(input,data);
    if(power_action!=POWER_NONE && !power_running)
        power_touch_sample(&power_touch,data->state==LV_INDEV_STATE_RELEASED);
}
static void power_press(lv_event_t *event) {
    lv_event_code_t code=lv_event_get_code(event);
    if(code==LV_EVENT_PRESSED)power_touch_press(&power_touch);
    else if(code==LV_EVENT_PRESS_LOST && power_touch==POWER_TOUCH_PRESSED)
        power_touch=POWER_TOUCH_READY;
}
static void restart_clicked(lv_event_t *event) {(void)event;power_request(POWER_RESTART);}
static void shutdown_clicked(lv_event_t *event) {(void)event;power_request(POWER_OFF);}
static void power_accept(lv_event_t *event) {
    (void)event;
    if(power_running || power_action==POWER_NONE || !power_touch_ready())return;
    if(!power_touch_accept(&power_touch))return;
    /* Recheck at the actual confirmation. Voice retains the media claim
     * through task reaping, including unproven teardown (even if mic off).
     * No stop, cancel, queue drain, or new session is triggered here. */
    if(!tailnet_spike_power_claim()) {
        power_notice("Pet is busy","Turn off the mic and wait for audio to close, then try again.");
        return;
    }
    if(!network_power_claim()) {
        tailnet_spike_power_release();
        power_notice("Pet is busy","Wait for Wi-Fi or phone setup to finish, then try again.");
        return;
    }
    power_running=true;
    lv_obj_add_flag(power_cancel,LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(power_confirm,LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text(power_title,power_action==POWER_RESTART?"Restarting...":"Requesting shutdown...");
    lv_label_set_text(power_message,"");
    device_power_confirmed(power_action);
}
static void power_refresh(lv_timer_t *timer) {
    (void)timer;
    if(power_running) {
        power_result_t result=device_power_result();
        if(result==POWER_PENDING)return;
        network_power_release();
        tailnet_spike_power_release();
        power_running=false;
        if(result==POWER_BOOT_HELD)
            power_notice("Restart not performed","Release BOOT, then try again.");
        else if(result==POWER_BOOT_UNAVAILABLE)
            power_notice("Restart unavailable","The BOOT input could not be checked. The pet is still on.");
        else if(result==POWER_UNAVAILABLE)
            power_notice("Shut down unavailable","The power controller is unavailable. The pet is still on.");
        else
            power_notice("Shutdown not confirmed","The pet is still on. No retry was attempted.");
        return;
    }
    /* The BSP can be in interrupt mode. Sample once here until we observe
     * release; do not arm from LVGL's cached state or change its input mode. */
    if(power_action!=POWER_NONE && power_touch==POWER_TOUCH_WAIT)lv_indev_read(diag_input);
    power_action_t request=device_power_take_key();
    if(request!=POWER_NONE && diag_input && lv_indev_get_state(diag_input)==LV_INDEV_STATE_RELEASED)
        power_request(request);
}
/* Updating an LVGL label frees and reallocates its text, even if it is
 * identical. Use this for ordinary labels whose text LVGL does not elide. */
static void update_label(lv_obj_t *object,const char *text) {
    const char *shown=lv_label_get_text(object);
    if(!shown || strcmp(shown,text)!=0) lv_label_set_text(object,text);
}
static void refresh(lv_timer_t *timer) {
    diag_mark(&input_diag.loop_in);
    audio_test_reap_finished();live_voice_reap_finished();
    audio_test_status_t a;audio_test_snapshot(&a);
    live_status_t voice;live_voice_snapshot(&voice);
    tailnet_spike_status_t tn;tailnet_spike_snapshot(&tn);
    bool tailnet_busy=tn.active||!tn.cleaned_up;
    bool releasing=voice.state==LIVE_CLOSING&&!voice.active;
    bool closing=releasing||(voice.active&&voice.state==LIVE_CLOSING);
    bool connecting=voice.active&&voice.state==LIVE_CONNECTING;
    bool mic_on=voice.active&&!closing&&!connecting;
    update_label(talk_label,closing?"Closing...":connecting?"Tap to cancel":mic_on?"Mic on - tap to mute":"Mic off - tap to talk");
    /* Same button and microphone throughout: lit when on, struck through when off. */
    static int mic_shown=-1;
    if(mic_shown!=(int)mic_on){
        mic_shown=mic_on;
        lv_obj_set_style_bg_color(talk_button,lv_color_hex(mic_on?0x86efac:0xf1f5f9),0);
        if(mic_on)lv_obj_add_flag(mute_slash,LV_OBJ_FLAG_HIDDEN);
        else lv_obj_clear_flag(mute_slash,LV_OBJ_FLAG_HIDDEN);
    }
    if(voice.active)lv_obj_add_state(settings_gear,LV_STATE_DISABLED);
    else lv_obj_remove_state(settings_gear,LV_STATE_DISABLED);
    if(releasing||tailnet_busy||(a.busy && !voice.active))lv_obj_add_state(talk_button,LV_STATE_DISABLED);
    else lv_obj_remove_state(talk_button,LV_STATE_DISABLED);
    char text[96];
    snprintf(text,sizeof(text),"Volume %d%%",a.volume);
    update_label(volume_label,text);
    if(a.busy||tailnet_busy)lv_obj_add_state(audio_back,LV_STATE_DISABLED);
    else lv_obj_remove_state(audio_back,LV_STATE_DISABLED);
    network_status_t s;network_snapshot(&s);
    const char *signal=s.rssi>=-55?"excellent":s.rssi>=-67?"good":s.rssi>=-75?"fair":"weak";
    /* The network's name only while associated with it (wifi_name.h). */
    char connection[40];wifi_name_line(connection,sizeof(connection),s.connected,s.joining,s.ssid);
    /* LV_LABEL_LONG_MODE_DOTS can edit the label's displayed text in place. */
    static char last_connection[sizeof(connection)];
    if(!lv_label_get_text(settings_connection) || strcmp(connection,last_connection)!=0) {
        lv_label_set_text(settings_connection,connection);
        strlcpy(last_connection,connection,sizeof(last_connection));
    }
    static int shown_connected=-1;
    if(shown_connected!=(int)s.connected) {
        shown_connected=s.connected;
        lv_label_set_text_static(wifi_badge,s.connected?LV_SYMBOL_WIFI "  Connected":LV_SYMBOL_WIFI "  Disconnected");
        lv_obj_set_style_text_color(wifi_badge,lv_color_hex(s.connected?0x86efac:0xfbbf24),0);
    }
    if(s.connected) snprintf(text,sizeof(text),"Signal %s",signal);
    else text[0]='\0';
    update_label(settings_signal,text);
    snprintf(text,sizeof(text),"%s\nVoice: %s",
        !s.bridge_valid?"Bridge settings invalid":s.bridge_enabled?"Host bridge configured":s.key_saved?"Standalone OpenAI key saved":"Configure bridge or standalone key",
        voice.active?"mic on":"mic off");
    update_label(settings_key,text);
    bool visible=s.setup&&panel_requested;
    if(visible!=was_setup) {
        was_setup=visible;
        if(visible){lv_obj_clear_flag(setup_panel,LV_OBJ_FLAG_HIDDEN);}
        else lv_obj_add_flag(setup_panel,LV_OBJ_FLAG_HIDDEN);
    }
    update_label(subtitle,(voice.active || voice.state==LIVE_ERROR || s.connected)?voice.message:s.joining?"Connecting Wi-Fi...":"Let's get connected");
    if(s.setup) {
        char details[120];snprintf(details,sizeof(details),"%s\nPassword: %s\nThen open 192.168.4.1",s.ap_ssid,s.ap_password);
        update_label(wifi_details,details);update_label(setup_message,s.joining?"Connecting to your Wi-Fi...":s.connected&&s.settings_saved?"Saved. Tap Done.":s.connected?"Not saved. Check your phone.":"Finish setup on your phone");
        char payload[160];snprintf(payload,sizeof(payload),"WIFI:T:WPA;S:%s;P:%s;;",s.ap_ssid,s.ap_password);
        static char last_payload[160];
        if(strcmp(last_payload,payload)) {lv_qrcode_update(qr,payload,strlen(payload));strlcpy(last_payload,payload,sizeof(last_payload));}
    }
    unsigned frame;
    if(wave_ticks){frame=22+(24-wave_ticks)/6;--wave_ticks;}
    else if(s.joining || voice.state==LIVE_CONNECTING || voice.state==LIVE_THINKING || voice.state==LIVE_RECONNECTING) frame=45+(tick_count/2)%6;
    else frame=(tick_count/3)%6;
    if(lv_image_get_src(pet)!=&frames[frame]) lv_image_set_src(pet,&frames[frame]);
    tick_count++;
    diag_visibility(lv_obj_has_flag(setup_panel,LV_OBJ_FLAG_HIDDEN),panel_requested);
    diag_mark(&input_diag.loop_out);
}
/* The V2 BSP has no LCD reset GPIO. CO5300 2.2.0 sends SWRESET and waits
 * 80 ms. If the panel was awake (e.g. after an MCU-only reset), the CO5300
 * SWRESET specification requires >=120 ms before the next Sleep Out.
 * The BSP's 100 ms delay AFTER Sleep Out does not satisfy this requirement.
 * Only the board component's call is redirected here; keep its command
 * sequence, touch setup and pinned component sources unchanged. */
esp_err_t pet_amoled_panel_reset(esp_lcd_panel_handle_t panel) {
    esp_err_t err = esp_lcd_panel_reset(panel);
    if (err == ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(120) + 1);  /* ~200 ms total; allow for tick phase */
    } else {
        ESP_LOGE("pet_display", "CO5300 reset failed: %s", esp_err_to_name(err));
    }
    return err;
}

void app_main(void) {
    ESP_ERROR_CHECK(audio_hardware_quiet());
    ESP_LOGI("audio_test","Boot: amplifier held off; codec/microphone not opened");
    /* Never erase user settings as an automatic response to an NVS error. */
    ESP_ERROR_CHECK(nvs_flash_init());
    network_init();audio_tests_init();live_voice_init();
    ESP_ERROR_CHECK(tailnet_spike_init());
    lv_display_t *display=bsp_display_start();assert(display);
    ESP_ERROR_CHECK(bsp_display_backlight_on());
    bsp_display_lock(0);
    lv_display_add_event_cb(display,display_event,LV_EVENT_FLUSH_START,NULL);
    lv_display_add_event_cb(display,display_event,LV_EVENT_FLUSH_FINISH,NULL);
    lv_obj_t *screen=lv_screen_active();lv_obj_set_style_bg_color(screen,lv_color_hex(0x0f172a),0);
    lv_obj_set_style_text_color(screen,lv_color_hex(0xf1f5f9),0);lv_obj_clear_flag(screen,LV_OBJ_FLAG_SCROLLABLE);
    title=label(screen,"Amplifier Assistant",350,0,12,&lv_font_montserrat_22);
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
    static lv_point_precise_t slash_points[]={{5,5},{35,35}};
    mute_slash=lv_line_create(microphone_icon);lv_line_set_points(mute_slash,slash_points,2);
    lv_obj_set_style_line_width(mute_slash,3,0);lv_obj_set_style_line_color(mute_slash,lv_color_hex(0xdc2626),0);
    lv_obj_set_style_line_rounded(mute_slash,true,0);lv_obj_remove_flag(mute_slash,LV_OBJ_FLAG_CLICKABLE);
    talk_label=label(screen,"Mic off - tap to talk",300,0,426,&lv_font_montserrat_12);
    setup_panel=lv_obj_create(screen);lv_obj_set_size(setup_panel,368,448);lv_obj_center(setup_panel);
    lv_obj_set_style_bg_color(setup_panel,lv_color_hex(0x0f172a),0);lv_obj_set_style_border_width(setup_panel,0,0);
    lv_obj_set_style_text_color(setup_panel,lv_color_hex(0xf1f5f9),0);
    lv_obj_set_style_pad_all(setup_panel,0,0);lv_obj_clear_flag(setup_panel,LV_OBJ_FLAG_SCROLLABLE);
    label(setup_panel,"Connect your phone",350,0,14,&lv_font_montserrat_22);
    label(setup_panel,"Scan to join Amplifier Assistant Wi-Fi",350,0,46,&lv_font_montserrat_16);
    char version[64];snprintf(version,sizeof(version),"Firmware %s",esp_app_get_description()->version);
    lv_obj_t *version_label=label(setup_panel,version,350,0,68,&lv_font_montserrat_14);
    lv_obj_set_style_text_color(version_label,lv_color_hex(0x8fe1ff),0);
    /* 4-module quiet zone around the Wi-Fi QR code. */
    lv_obj_t *quiet=lv_obj_create(setup_panel);lv_obj_set_size(quiet,204,204);lv_obj_align(quiet,LV_ALIGN_TOP_MID,0,86);
    lv_obj_set_style_bg_color(quiet,lv_color_white(),0);lv_obj_set_style_radius(quiet,5,0);lv_obj_set_style_border_width(quiet,0,0);lv_obj_set_style_pad_all(quiet,0,0);lv_obj_clear_flag(quiet,LV_OBJ_FLAG_SCROLLABLE);
    qr=lv_qrcode_create(quiet);lv_qrcode_set_size(qr,176);lv_qrcode_set_dark_color(qr,lv_color_black());lv_qrcode_set_light_color(qr,lv_color_white());lv_obj_center(qr);
    wifi_details=label(setup_panel,"",350,0,298,&lv_font_montserrat_16);
    setup_message=label(setup_panel,"",350,0,372,&lv_font_montserrat_16);
    back_button=button(setup_panel,"Back / Done",0,-10,240,close_clicked);
    lv_obj_add_event_cb(back_button,diag_back_event,LV_EVENT_ALL,NULL);
    lv_obj_add_flag(setup_panel,LV_OBJ_FLAG_HIDDEN);
    audio_panel=lv_obj_create(screen);lv_obj_set_size(audio_panel,368,448);lv_obj_center(audio_panel);
    lv_obj_set_style_bg_color(audio_panel,lv_color_hex(0x0f172a),0);lv_obj_set_style_text_color(audio_panel,lv_color_hex(0xf1f5f9),0);
    lv_obj_set_style_border_width(audio_panel,0,0);lv_obj_set_style_pad_all(audio_panel,0,0);lv_obj_clear_flag(audio_panel,LV_OBJ_FLAG_SCROLLABLE);
    label(audio_panel,"Volume",340,0,20,&lv_font_montserrat_22);
    volume_label=label(audio_panel,"Volume 60%",340,0,114,&lv_font_montserrat_16);
    lv_obj_t *volume_slider=lv_slider_create(audio_panel);lv_obj_set_size(volume_slider,278,16);lv_obj_align(volume_slider,LV_ALIGN_TOP_MID,0,149);
    audio_test_status_t initial_audio;audio_test_snapshot(&initial_audio);
    lv_slider_set_range(volume_slider,0,100);lv_slider_set_value(volume_slider,initial_audio.volume,LV_ANIM_OFF);
    lv_obj_set_style_bg_color(volume_slider,lv_color_hex(0x38bdf8),LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(volume_slider,lv_color_hex(0x8fe1ff),LV_PART_KNOB);
    lv_obj_add_event_cb(volume_slider,volume_changed,LV_EVENT_VALUE_CHANGED,NULL);
    lv_obj_add_event_cb(volume_slider,volume_released,LV_EVENT_RELEASED,NULL);
    audio_back=button(audio_panel,"Back to settings",0,-10,260,audio_closed);
    lv_obj_add_flag(audio_panel,LV_OBJ_FLAG_HIDDEN);
    settings_panel=lv_obj_create(screen);lv_obj_set_size(settings_panel,368,448);lv_obj_center(settings_panel);
    lv_obj_set_style_bg_color(settings_panel,lv_color_hex(0x0f172a),0);lv_obj_set_style_border_width(settings_panel,0,0);
    lv_obj_set_style_pad_all(settings_panel,0,0);lv_obj_remove_flag(settings_panel,LV_OBJ_FLAG_SCROLLABLE);
    label(settings_panel,"Settings",340,0,14,&lv_font_montserrat_22);
    settings_connection=label(settings_panel,"",330,0,52,&lv_font_montserrat_16);
    /* One line: a name too wide for it ends in dots and never moves the lines below. */
    lv_label_set_long_mode(settings_connection,LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_height(settings_connection,lv_font_get_line_height(&lv_font_montserrat_16));
    settings_signal=label(settings_panel,"",330,0,74,&lv_font_montserrat_16);
    settings_key=label(settings_panel,"",330,0,100,&lv_font_montserrat_14);
    footer=button(settings_panel,"Phone setup",0,0,280,setup_clicked);lv_obj_align(footer,LV_ALIGN_TOP_MID,0,140);lv_obj_set_height(footer,40);
    audio_open_button=button(settings_panel,"Volume",0,0,280,audio_clicked);lv_obj_align(audio_open_button,LV_ALIGN_TOP_MID,0,186);lv_obj_set_height(audio_open_button,40);
    lv_obj_t *alignment=button(settings_panel,"Touch alignment",0,0,280,align_touch_clicked);lv_obj_align(alignment,LV_ALIGN_TOP_MID,0,232);lv_obj_set_height(alignment,40);
    lv_obj_t *restart=button(settings_panel,"Restart",0,0,130,restart_clicked);lv_obj_align(restart,LV_ALIGN_TOP_MID,-75,282);
    lv_obj_t *shutdown=button(settings_panel,"Shut down",0,0,130,shutdown_clicked);lv_obj_align(shutdown,LV_ALIGN_TOP_MID,75,282);
    button(settings_panel,"Back to Amplifier Assistant",0,-20,280,settings_closed);
    lv_obj_add_flag(settings_panel,LV_OBJ_FLAG_HIDDEN);
    uint32_t children=lv_obj_get_child_count(screen);
    diag_install(screen);
    /* The pinned calibration component appends its full-screen overlay. Its
     * reader consumes releases while open, including the NVS save. Don't
     * display a confirmation above it or assume it's idle if not found. */
    if(saved_cal && lv_obj_get_child_count(screen)==children+1) {
        calibration_panel=lv_obj_get_child(screen,children);
        lv_indev_set_read_cb(diag_input,power_touch_read);
    }
    power_panel=lv_obj_create(lv_layer_top());
    lv_obj_remove_style_all(power_panel);lv_obj_set_size(power_panel,lv_pct(100),lv_pct(100));lv_obj_center(power_panel);
    lv_obj_set_style_bg_opa(power_panel,LV_OPA_COVER,0);lv_obj_set_style_bg_color(power_panel,lv_color_hex(0x0f172a),0);
    lv_obj_remove_flag(power_panel,LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(power_panel,LV_OBJ_FLAG_CLICKABLE|LV_OBJ_FLAG_HIDDEN);
    power_title=label(power_panel,"",340,0,112,&lv_font_montserrat_22);
    power_message=label(power_panel,"",310,0,163,&lv_font_montserrat_16);
    power_cancel=button(power_panel,"Cancel",-80,-174,132,power_close);
    power_confirm=button(power_panel,"",80,-174,132,power_accept);
    lv_obj_add_event_cb(power_confirm,power_press,LV_EVENT_PRESSED,NULL);
    lv_obj_add_event_cb(power_confirm,power_press,LV_EVENT_PRESS_LOST,NULL);
    lv_timer_create(power_refresh,60,NULL);
    lv_timer_create(refresh,120,NULL);refresh(NULL);
    bsp_display_unlock();
    diag_start();
    ESP_LOGI("pet","UI ready; microphone only after a tap.");
    device_power_run();
}
