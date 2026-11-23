#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef struct {int dummy;} lv_obj_t;
typedef int lv_indev_mode_t;
typedef struct {struct {int x,y;} point;int state;} lv_indev_data_t;
typedef struct lv_indev lv_indev_t;
typedef void (*lv_indev_read_cb_t)(lv_indev_t *,lv_indev_data_t *);
struct lv_indev {lv_indev_read_cb_t read;lv_indev_mode_t mode;};
#define LV_ALIGN_TOP_MID 0
#define LV_TEXT_ALIGN_CENTER 0
#define LV_OBJ_FLAG_HIDDEN 1
#define LV_OBJ_FLAG_SCROLLABLE 2
#define LV_INDEV_STATE_PRESSED 1
#define LV_INDEV_STATE_RELEASED 0
#define LV_INDEV_MODE_TIMER 1
#define LV_RADIUS_CIRCLE 20
#define LV_OPA_TRANSP 0
#define LV_OPA_COVER 255
static lv_obj_t stub_object;
static uint32_t stub_ticks;
static inline lv_obj_t *lv_label_create(lv_obj_t *p){(void)p;return &stub_object;}
static inline lv_obj_t *lv_obj_create(lv_obj_t *p){(void)p;return &stub_object;}
#define lv_label_set_text(...) ((void)0)
#define lv_label_set_text_fmt(...) ((void)0)
#define lv_obj_set_width(...) ((void)0)
#define lv_obj_align(...) ((void)0)
#define lv_obj_set_style_text_color(...) ((void)0)
#define lv_obj_set_style_text_align(...) ((void)0)
#define lv_obj_add_flag(...) ((void)0)
#define lv_obj_remove_flag(...) ((void)0)
#define lv_obj_set_pos(...) ((void)0)
#define lv_obj_move_foreground(...) ((void)0)
#define lv_obj_set_size(...) ((void)0)
#define lv_obj_center(...) ((void)0)
#define lv_obj_set_style_pad_all(...) ((void)0)
#define lv_obj_set_style_bg_color(...) ((void)0)
#define lv_obj_set_style_border_width(...) ((void)0)
#define lv_obj_set_style_radius(...) ((void)0)
#define lv_obj_set_style_border_color(...) ((void)0)
#define lv_obj_set_style_bg_opa(...) ((void)0)
#define lv_obj_remove_style_all(...) ((void)0)
static inline uint32_t lv_tick_get(void){return stub_ticks;}
static inline uint32_t lv_tick_elaps(uint32_t t){return stub_ticks-t;}
static inline lv_indev_mode_t lv_indev_get_mode(lv_indev_t *p){return p->mode;}
static inline void lv_indev_set_mode(lv_indev_t *p,lv_indev_mode_t m){p->mode=m;}
static inline lv_indev_read_cb_t lv_indev_get_read_cb(lv_indev_t *p){return p->read;}
static inline void lv_indev_set_read_cb(lv_indev_t *p,lv_indev_read_cb_t f){p->read=f;}