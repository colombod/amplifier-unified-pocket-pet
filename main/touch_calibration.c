#include "touch_calibration.h"
#include "touch_alignment.h"
#include <string.h>
#include "nvs.h"
#include "esp_log.h"
static lv_indev_read_cb_t original_read;
static lv_obj_t *panel,*target_ring,*help,*progress;
static bool collecting,result_visible,pressed,mapped,store_ready;
static unsigned stage;
static uint32_t press_time;
static touch_point_t points[3],last_raw;
static touch_map_t current,candidate;
static nvs_handle_t store;
static const touch_point_t targets[]={{64,140},{304,140},{184,340},{184,224}};
typedef struct {uint32_t version;touch_map_t map;} stored_map_t;
static lv_obj_t *text(const char *value,int y) {
 lv_obj_t *o=lv_label_create(panel);lv_label_set_text(o,value);lv_obj_set_width(o,336);lv_obj_align(o,LV_ALIGN_TOP_MID,0,y);
 lv_obj_set_style_text_color(o,lv_color_hex(0xf1f5f9),0);lv_obj_set_style_text_align(o,LV_TEXT_ALIGN_CENTER,0);return o;
}
static void finish(const char *message) {
 collecting=false;result_visible=true;lv_obj_add_flag(target_ring,LV_OBJ_FLAG_HIDDEN);
 lv_label_set_text(help,message);lv_label_set_text(progress,"Tap anywhere to return to Settings.");
}
static void advance(touch_point_t raw) {
 if(stage<3)points[stage]=raw;
 stage++;
 if(stage==3 && !touch_map_solve(points,targets,&candidate)){finish("Points were too close.\nAlignment unchanged; please retry.");return;}
 if(stage==4) {
  touch_point_t check=touch_map_apply(&candidate,raw);float dx=check.x-targets[3].x,dy=check.y-targets[3].y;
  if(dx*dx+dy*dy>25*25){finish("Center check missed.\nAlignment unchanged; please retry.");return;}
  current=candidate;mapped=true;
  stored_map_t saved={.version=1,.map=current};esp_err_t err=store_ready?nvs_set_blob(store,"map",&saved,sizeof(saved)):ESP_FAIL;
  if(err==ESP_OK)err=nvs_commit(store);
  finish(err==ESP_OK?"Alignment saved.\nTry the Settings buttons now.":"Aligned for this boot.\nCould not save; retry after restart.");
  ESP_LOGI("pet_touch","Four-point alignment checked; persistence=%s",esp_err_to_name(err));return;
 }
 lv_obj_set_pos(target_ring,(int)targets[stage].x-20,(int)targets[stage].y-20);
 lv_label_set_text_fmt(progress,stage==3?"Point 4 of 4: verify the center":"Point %u of 4",stage+1);
}
static void calibrated_read(lv_indev_t *input,lv_indev_data_t *data) {
 original_read(input,data);bool down=data->state==LV_INDEV_STATE_PRESSED;
 touch_point_t raw={data->point.x,data->point.y};
 if(collecting || result_visible) {
  if(down){last_raw=raw;if(!pressed)press_time=lv_tick_get();}
  if(!down && pressed) {
   if(result_visible){result_visible=false;lv_obj_add_flag(panel,LV_OBJ_FLAG_HIDDEN);}
   else if(lv_tick_elaps(press_time)>1500)finish("Alignment cancelled.\nPrevious alignment kept.");
   else advance(last_raw);
  }
  pressed=down;data->state=LV_INDEV_STATE_RELEASED;return;
 }
 pressed=down;
 if(mapped && down){touch_point_t point=touch_map_apply(&current,raw);data->point.x=(int)lroundf(fmaxf(0,fminf(367,point.x)));data->point.y=(int)lroundf(fmaxf(0,fminf(447,point.y)));}
}
void touch_calibration_begin(void) {
 stage=0;pressed=false;collecting=true;result_visible=false;
 lv_label_set_text(help,"Tap the center of each ring, then lift.");
 lv_label_set_text(progress,"Point 1 of 4\nHold then lift to cancel.");
 lv_obj_set_pos(target_ring,44,120);lv_obj_remove_flag(target_ring,LV_OBJ_FLAG_HIDDEN);
 lv_obj_remove_flag(panel,LV_OBJ_FLAG_HIDDEN);lv_obj_move_foreground(panel);
}
void touch_calibration_init(lv_indev_t *input,lv_obj_t *screen) {
 store_ready=nvs_open("pet_touch",NVS_READWRITE,&store)==ESP_OK;
 stored_map_t saved;size_t length=sizeof(saved);
 if(store_ready && nvs_get_blob(store,"map",&saved,&length)==ESP_OK && length==sizeof(saved) && saved.version==1) {
  touch_point_t test=touch_map_apply(&saved.map,(touch_point_t){184,224});
  if(isfinite(test.x)&&isfinite(test.y)&&fabsf(test.x)<1000&&fabsf(test.y)<1000){current=saved.map;mapped=true;}
 }
 panel=lv_obj_create(screen);lv_obj_set_size(panel,368,448);lv_obj_center(panel);lv_obj_set_style_pad_all(panel,0,0);
 lv_obj_set_style_bg_color(panel,lv_color_hex(0x0f172a),0);lv_obj_set_style_border_width(panel,0,0);lv_obj_remove_flag(panel,LV_OBJ_FLAG_SCROLLABLE);
 text("Touch alignment",24);help=text("",60);progress=text("",385);
 target_ring=lv_obj_create(panel);lv_obj_set_size(target_ring,40,40);lv_obj_set_style_radius(target_ring,LV_RADIUS_CIRCLE,0);
 lv_obj_set_style_border_width(target_ring,3,0);lv_obj_set_style_border_color(target_ring,lv_color_hex(0x38bdf8),0);lv_obj_set_style_bg_opa(target_ring,LV_OPA_TRANSP,0);
 lv_obj_t *dot=lv_obj_create(target_ring);lv_obj_remove_style_all(dot);lv_obj_set_size(dot,4,4);lv_obj_center(dot);lv_obj_set_style_radius(dot,2,0);lv_obj_set_style_bg_opa(dot,LV_OPA_COVER,0);lv_obj_set_style_bg_color(dot,lv_color_hex(0xf1f5f9),0);
 lv_obj_add_flag(panel,LV_OBJ_FLAG_HIDDEN);
 original_read=lv_indev_get_read_cb(input);lv_indev_set_read_cb(input,calibrated_read);
}
