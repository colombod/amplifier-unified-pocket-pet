"""Compile verbatim app diagnostic bodies; LVGL/RTOS boundaries only."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

APP=Path(__file__).resolve().parents[1]

def function(text,name):
    lines=text.splitlines(True)
    start=next(i for i,line in enumerate(lines)
               if re.match(rf"^(?:static )?[\w *]+\b{name}\(",line))
    depth=0
    for end in range(start,len(lines)):
        depth+=lines[end].count("{")-lines[end].count("}")
        if depth==0 and "}" in lines[end]:
            return "".join(lines[start:end+1])
    raise AssertionError(name)

def run(out,red=False,collector=None):
    out.mkdir(parents=True,exist_ok=True)
    text=(APP/"main/main.c").read_text(encoding="utf-8")
    if red:
        # Functional RED against installed production's four-shot logger, not
        # a compile error due to the new wrappers not existing.
        body=function(text,"display_diagnostics")
        source=r'''
#include <assert.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdio.h>
static _Atomic unsigned diag_ticks,diag_flush_start,diag_flush_return;
static unsigned samples,delays;
#define pdMS_TO_TICKS(n) (n)
#define ESP_LOGI(...) (++samples)
static void vTaskDelay(unsigned ms){delays+=ms;}
static void vTaskDelete(void*p){(void)p;}
'''+body+r'''
int main(void){display_diagnostics(NULL);printf("samples=%u duration_ms=%u\n",samples,delays);
 assert(samples==1200&&delays==1200000);puts("PASS diagnostic budget");}
'''
    else:
        block=text.split("/* INPUT_DIAG_BEGIN */",1)[1].split("/* INPUT_DIAG_END */",1)[0]
        bodies={name:function(text,name) for name in ("close_clicked",)}
        (out/"production-body-hashes.json").write_text(json.dumps({
            "source_sha256":hashlib.sha256(text.encode()).hexdigest(),
            "block_sha256":hashlib.sha256(block.encode()).hexdigest(),
            "bodies":{n:hashlib.sha256(b.encode()).hexdigest() for n,b in bodies.items()}
        },indent=2)+"\n",encoding="utf-8")
        # The full block is production globals/helpers/wrappers/logger/install.
        source=HOST+block+"\n"+bodies["close_clicked"]+"\n"+TEST
        assert "diag_mark(&input_diag.loop_in)" in function(text,"refresh")
        assert "diag_mark(&input_diag.loop_out)" in function(text,"refresh")
        assert 'back_button=button(setup_panel,"Back / Done",0,-10,240,close_clicked)' in text
        assert "lv_obj_add_event_cb(back_button,diag_back_event,LV_EVENT_ALL,NULL)" in text
    p=out/"input_diag.c";p.write_text(source,encoding="utf-8")
    exe=out/"input_diag"
    subprocess.run(["cc","-Wall","-Wextra","-Werror","-Wno-unused-parameter",
        "-Wno-unused-variable","-g","-pthread","-fsanitize=address,undefined",
        "-fno-omit-frame-pointer",str(p),"-o",str(exe)],check=True)
    executed=subprocess.run([str(exe)],check=True,stdout=subprocess.PIPE,text=True)
    print(executed.stdout,end="")
    if not red:
        print("PASS input diagnostic production-block tests (9 cases)")
        record=json.loads(next(line for line in executed.stdout.splitlines() if line.startswith("{")))
        if collector:
            spec=importlib.util.spec_from_file_location("input_collector",collector)
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            good=json.dumps(record)
            assert module.parse_line(good)==record
            assert module.parse_line("I (123) pet_input_diag: "+good)==record
            assert module.parse_line("\x1b[0;32mI (123) pet_input_diag: "+good+"\x1b[0m")==record
            rejected=0
            for bad in (
                record|{"ssid":"PRIVATE"},
                record|{"v":2},
                record|{"raw_x":"PRIVATE"},
                record|{"raw_x":1.5},
                record|{"active":True},
                record|{"target":6},
                record|{"ms":-1},
                {k:v for k,v in record.items() if k!="seq"},
            ):
                try:
                    module.parse_line(json.dumps(bad))
                except (ValueError,TypeError):
                    rejected+=1
                else:raise AssertionError("invalid parser schema accepted")
            for bad in (good[:-1]+',"v":1}',"PRIVATE"+good,good+"PRIVATE","x"*2049):
                try:
                    module.parse_line(bad)
                except (ValueError,TypeError):rejected+=1
                else:raise AssertionError("invalid parser framing accepted")
            assert rejected==12
            p=subprocess.run(["python3",str(collector)],input="SECRET SSID ignored\n"+good+"\n"+
                json.dumps(record|{"credential":"SECRET"})+"\n",
                text=True,capture_output=True,check=True)
            assert not p.stderr and [json.loads(l) for l in p.stdout.splitlines()]==[record]
            print("PASS collector actual producer record: 3 valid framings, 12 rejected schemas/frames, stdout-only no-echo filter")

HOST=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include <pthread.h>
typedef struct {int32_t x,y;} lv_point_t;
typedef struct lv_indev lv_indev_t;
typedef struct {lv_point_t point;int state;bool continue_reading;int key;} lv_indev_data_t;
typedef void (*lv_indev_read_cb_t)(lv_indev_t*,lv_indev_data_t*);
struct lv_indev {lv_indev_read_cb_t read;void*user_data;void*driver_data;int mode;lv_point_t point;};
typedef struct {int id,hidden;} lv_obj_t;
typedef int lv_event_code_t;
typedef struct {int code;void*target;void*param;} lv_event_t;
static lv_obj_t back,gear,phone,panel,other;
static lv_obj_t *back_button=&back,*settings_gear=&gear,*footer=&phone,*setup_panel=&panel,*settings_panel=&other;
static bool panel_requested=true,return_to_settings=true;
enum {LV_EVENT_PRESSED=10,LV_EVENT_RELEASED=20,LV_EVENT_CLICKED=30,
      LV_EVENT_FLUSH_START=40,LV_EVENT_FLUSH_FINISH=50,LV_EVENT_ALL=99,
      LV_INDEV_STATE_PRESSED=1,LV_INDEV_STATE_RELEASED=0,LV_OBJ_FLAG_HIDDEN=1};
typedef pthread_mutex_t portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED PTHREAD_MUTEX_INITIALIZER
static unsigned held;
#define portENTER_CRITICAL(p) do{assert(!held);assert(!pthread_mutex_lock(p));held++;}while(0)
#define portEXIT_CRITICAL(p) do{assert(held==1);held--;assert(!pthread_mutex_unlock(p));}while(0)
#define pdMS_TO_TICKS(n) (n)
#define pdPASS 1
static int64_t now;
static unsigned samples,deleted,bsp_calls,cal_calls,network_closes;
static bool task_fail;
static lv_indev_t input;
static lv_indev_data_t *expected_data;
static bool down=true;
static lv_indev_read_cb_t cal_previous;
static void (*indev_observer)(lv_event_t*);
static lv_obj_t *observed_target;
static char latest[2048];
static int64_t esp_timer_get_time(void){return now;}
static void vTaskDelay(unsigned ms){assert(!held);now+=(int64_t)ms*1000;}
static void vTaskDelete(void*p){assert(!held&&!p);deleted++;}
static int lv_event_get_code(lv_event_t*e){assert(!held);return e->code;}
static void *lv_event_get_param(lv_event_t*e){assert(!held);return e->param;}
static void *lv_event_get_target(lv_event_t*e){assert(!held);return e->target;}
static void lv_indev_get_point(lv_indev_t*i,lv_point_t*p){assert(!held);*p=i->point;}
static lv_indev_read_cb_t lv_indev_get_read_cb(lv_indev_t*i){assert(!held);return i->read;}
static void lv_indev_set_read_cb(lv_indev_t*i,lv_indev_read_cb_t cb){assert(!held);i->read=cb;}
static lv_indev_t *bsp_display_get_input_dev(void){assert(!held);return &input;}
static void lv_indev_add_event_cb(lv_indev_t*i,void(*cb)(lv_event_t*),int filter,void*data){
 assert(!held&&i==&input&&filter==LV_EVENT_ALL&&!data);indev_observer=cb;
}
static void bsp_read(lv_indev_t*i,lv_indev_data_t*d){
 assert(!held&&i==&input&&d==expected_data);bsp_calls++;
 assert(i->user_data==&back&&i->driver_data==&gear&&i->mode==7);
 d->state=down;d->point=(lv_point_t){12,34};d->continue_reading=true;d->key=123;
}
static void cal_read(lv_indev_t*i,lv_indev_data_t*d){
 assert(!held);cal_calls++;cal_previous(i,d);
 if(d->state==LV_INDEV_STATE_PRESSED){int32_t x=d->point.x;d->point.x=d->point.y+5;d->point.y=x+7;}
}
static void touch_calibration_init(lv_indev_t*i,lv_obj_t*screen){
 assert(!held&&screen==&panel);cal_previous=i->read;i->read=cal_read;
}
static void log_numeric(const char*tag,const char*fmt,...){
 assert(!held&&!strcmp(tag,"pet_input_diag"));va_list ap;va_start(ap,fmt);
 assert(vsnprintf(latest,sizeof(latest),fmt,ap)>0);va_end(ap);
 assert(strlen(latest)<sizeof(latest)-1);samples++;
}
#define ESP_LOGI log_numeric
static int xTaskCreate(void(*fn)(void*),const char*name,unsigned stack,void*arg,unsigned priority,void*out){
 assert(!held&&stack==3072&&priority==2&&!arg&&!out&&!strcmp(name,"input_diag"));return !task_fail;
}
static bool lv_obj_has_flag(lv_obj_t*o,int f){assert(!held&&f==1);return o->hidden;}
static void lv_obj_clear_flag(lv_obj_t*o,int f){assert(!held&&f==1);o->hidden=0;}
static void network_close_setup(void){assert(!held);network_closes++;}
'''

TEST=r'''
int main(void){
 input=(lv_indev_t){.read=bsp_read,.user_data=&back,.driver_data=&gear,.mode=7};
 diag_install(&panel);
 assert(input.read==diag_mapped&&saved_bsp==bsp_read&&saved_cal==cal_read);
 assert(indev_observer&&input_diag.chain==2);
 lv_indev_data_t d={0};expected_data=&d;input.read(&input,&d);
 assert(bsp_calls==1&&cal_calls==1&&d.point.x==39&&d.point.y==19&&d.key==123&&d.continue_reading);
 assert(input_diag.raw_x==12&&input_diag.raw_y==34&&input_diag.map_x==39&&input_diag.map_y==19);
 assert(input_diag.read_in==1&&input_diag.raw_n==1&&input_diag.map_n==1&&input_diag.raw_id==1&&input_diag.map_id==1);
 down=false;input.read(&input,&d);assert(bsp_calls==2&&cal_calls==2);
 assert(input_diag.raw_up==1&&input_diag.map_up==1&&input_diag.raw_id==1&&input_diag.map_id==1);
 assert(input_diag.raw_x==12&&input_diag.map_x==39); /* no stale release-point replacement */
 input.point=(lv_point_t){100,200};
 lv_event_t event={.code=LV_EVENT_PRESSED,.target=&input,.param=&back};
 assert(lv_event_get_target(&event)!=lv_event_get_param(&event));
 indev_observer(&event);assert(input_diag.target==1&&input_diag.point_x==100&&input_diag.point_y==200);
 diag_back_event(&event);event.code=LV_EVENT_RELEASED;diag_back_event(&event);
 event.code=LV_EVENT_CLICKED;diag_back_event(&event);close_clicked(&event);
 assert(network_closes==1&&!panel_requested&&!return_to_settings&&input_diag.close_in==1&&input_diag.close_out==1);
 assert(input_diag.back_pressed==1&&input_diag.back_released==1&&input_diag.back_clicked==1);
 assert(diag_target(&gear)==2&&diag_target(&phone)==3&&diag_target(&panel)==4&&diag_target(&other)==5&&diag_target(NULL)==0);
 panel.hidden=1;diag_visibility(lv_obj_has_flag(&panel,LV_OBJ_FLAG_HIDDEN),false);
 assert(input_diag.hidden==1&&!input_diag.requested);
 event.code=LV_EVENT_FLUSH_START;display_event(&event);event.code=LV_EVENT_FLUSH_FINISH;display_event(&event);
 assert(input_diag.flush_start==1&&input_diag.flush_return==1);
 diag_mark(&input_diag.loop_in); /* freeze before refresh returns */
 display_diagnostics(NULL);assert(samples==1200&&deleted==1&&now==1200000000);
 assert(!input_diag.active&&input_diag.loop_in==1&&input_diag.loop_out==0);
 assert(strstr(latest,"\"loop_in\":1")&&strstr(latest,"\"loop_out\":0"));
 assert(strstr(latest,"\"seq\":1200")&&strstr(latest,"\"active\":0"));
 unsigned old=input_diag.map_n;input.read(&input,&d);
 assert(bsp_calls==3&&cal_calls==3&&input_diag.map_n==old&&d.continue_reading&&d.key==123);
 diag_back_event(&event);assert(input_diag.back_clicked==1);
 /* Creation failure honest numeric result, disabled diagnostics, no callback removal. */
 input_diag.active=1;task_fail=true;unsigned before=samples;
 diag_start();assert(!input_diag.active&&!input_diag.task&&samples==before+1&&input.read==diag_mapped);
 input.read(&input,&d);assert(bsp_calls==4&&cal_calls==4);
 /* Budget measured from boot, not task start: only the remaining second logs. */
 input_diag.active=1;input_diag.task=1;now=1199000000;
 before=samples;display_diagnostics(NULL);
 assert(samples==before+1&&!input_diag.active&&now==1200000000);
 /* No read predecessor: do not install or invoke an invalid calibration chain. */
 input.read=NULL;input_diag.active=1;diag_install(&panel);assert(!input.read&&input_diag.chain==0);
 puts("PASS input chaining, transform, release, event-param, original handler, heartbeat/freeze, 1200 bound, pass-through, failure cases");
 puts(latest);
}
'''

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--red",action="store_true")
    p.add_argument("--collector",type=Path)
    args=p.parse_args()
    run(args.out.expanduser().resolve(),args.red,args.collector)