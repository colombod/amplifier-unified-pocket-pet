/* Actual extracted run/sample/cleanup bodies. Only heap, network, RTOS, clock
 * and TailTiny/TLS calls are substituted. No device, NVS or transport access.
 * Init intentionally returns a sentinel error at IDENTITY: admission proof,
 * not proof that initialization, enrollment or networking succeeds. */
#include <assert.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <stdarg.h>
#include "tailnet_spike.h"
#include "tailnet_tls.h"

#define MALLOC_CAP_INTERNAL 1
#define MALLOC_CAP_8BIT 2
#define MALLOC_CAP_SPIRAM 4
#define ESP_ERR_NOT_FOUND 0x105
#define portMAX_DELAY 0
#define pdMS_TO_TICKS(n) (n)
#define SNTP_OPMODE_POLL 0
static void spike_log(const char *tag,const char *format,...);
#define ESP_LOGI spike_log

/* Minimal network hardware seam shapes used by the extracted run body. */
typedef struct { bool connected,joining,setup_paused,tailscale_key_saved; } network_status_t;
typedef struct { char host[64]; uint16_t port; } setup_unified_target_t;
static int lock;
static tailnet_spike_status_t status;
static atomic_int owner;
static atomic_bool cancelled;
static uint64_t began,overall;
static tailtiny_t *runtime;
static bool memory_failed;
static bool paused_heap,invalid_target;
static network_status_t net;
static esp_err_t target_error,pause_error,resume_error;
static unsigned pause_calls,resume_calls,init_calls,target_calls,stack_free;
static size_t before[3],after[3];
static bool create_runtime,stop_failure;
static unsigned stop_calls,destroy_calls,diag_logs;
static int runtime_marker;
static uint64_t clock_ms,recover_at,cancel_at,floor_at;
static unsigned delay_calls,ram_logs;
static unsigned target_logs;
static size_t recovered[3];
static unsigned logged_initial,logged_final,logged_max,logged_largest,logged_psram,logged_wait,logged_passed;
static void spike_log(const char *tag,const char *format,...) {
    (void)tag;char text[200];va_list args;va_start(args,format);
    int n=vsnprintf(text,sizeof(text),format,args);va_end(args);
    assert(n>0&&(size_t)n<sizeof(text));
    if(strncmp(text,"validated_target ",17)==0) {
        target_logs++;
        assert(!strcmp(text,"validated_target host=node.example port=8941"));
        assert(target_calls==1&&!pause_calls&&!init_calls&&target_logs==1);
    }
    if(strncmp(text,"peer_diag ",10)==0) {
        assert(stop_calls==1&&!destroy_calls);diag_logs++;
        assert(text[15]==(stop_failure?'1':'0')); /* "peer_diag live=X" */
        const char *p=strchr(text,'[');assert(p);p++;
        for(unsigned field=0;field<22;field++) {
            char *end;unsigned long value=strtoul(p,&end,10);
            assert(value==65535&&end!=p);assert(*end==(field==21?']':','));p=end+1;
        }
        assert(!*p);
    }
    if(strncmp(text,"preflight_ram ",14)==0) {
        ram_logs++;
        assert(sscanf(text,"preflight_ram initial=%u final=%u max=%u largest=%u psram=%u waitMs=%u passed=%u",
            &logged_initial,&logged_final,&logged_max,&logged_largest,&logged_psram,&logged_wait,&logged_passed)==7);
        assert(ram_logs==1&&logged_wait<=2000);
    }
    puts(text);
}

/* Lifecycle semantics have their own actual-body concurrent host suite. */
void audio_test_reap_finished(void) {}
void live_voice_reap_finished(void) {}
static void xSemaphoreTake(int handle,int wait) {(void)handle;(void)wait;}
static void xSemaphoreGive(int handle) {(void)handle;}
static int64_t esp_timer_get_time(void) {return (int64_t)clock_ms*1000;}
static unsigned uxTaskGetStackHighWaterMark(void *task) {(void)task;return stack_free;}
static void vTaskDelay(unsigned ticks) {
    assert(ticks>0&&ticks<=25);delay_calls++;clock_ms+=ticks;
    if(recover_at&&clock_ms>=recover_at)memcpy(after,recovered,sizeof(after));
    if(cancel_at&&clock_ms>=cancel_at)atomic_store(&cancelled,true);
    if(floor_at&&clock_ms>=floor_at)stack_free=1023;
}
static size_t heap_caps_get_free_size(unsigned caps) {
    const size_t *values=paused_heap?after:before;
    assert(caps==(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)||caps==(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    return values[(caps&MALLOC_CAP_SPIRAM)?2:0];
}
static size_t heap_caps_get_largest_free_block(unsigned caps) {
    assert(caps==(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT));
    return (paused_heap?after:before)[1];
}
static void network_snapshot(network_status_t *out) {*out=net;}
static esp_err_t network_copy_unified_target(setup_unified_target_t *out) {
    target_calls++;memset(out,0,sizeof(*out));
    if(!invalid_target)strcpy(out->host,"node.example");
    out->port=8941;return target_error;
}
static esp_err_t network_copy_tailscale_auth_key(char *out,size_t capacity) {
    (void)out;(void)capacity;abort(); /* Must never read any credential here. */
}
static esp_err_t network_pause_setup(uint32_t timeout) {
    assert(timeout==3000);assert(target_calls==1);pause_calls++;
    if(pause_error==ESP_OK)paused_heap=true;
    return pause_error;
}
static esp_err_t network_resume_setup(uint32_t timeout) {
    assert(timeout>0&&timeout<=9500);assert(paused_heap);
    resume_calls++;
    if(resume_error==ESP_OK)paused_heap=false;
    return resume_error;
}
static time_t test_time(time_t *out) {
    time_t qualified=1760000001;if(out)*out=qualified;return qualified;
}
#define time test_time
static bool esp_sntp_enabled(void) {return true;}
static void esp_sntp_setoperatingmode(int mode) {(void)mode;abort();}
static void esp_sntp_setservername(int n,const char *name) {(void)n;(void)name;abort();}
static void esp_sntp_init(void) {abort();}
static void esp_sntp_stop(void) {abort();}

esp_err_t tailtiny_init_checked(const tailtiny_config_t *cfg,tailtiny_t **out) {
    assert(paused_heap&&status.listener_paused&&status.state==TAILNET_IDENTITY);
    assert(cfg->peer_port==8941&&!strcmp(cfg->peer_fqdn,"node.example"));
    assert(!cfg->auth_key[0]&&cfg->copy_bootstrap_key==network_copy_tailscale_auth_key);
    assert(cfg->max_peers==1);
    init_calls++;
    *out=create_runtime?(tailtiny_t *)&runtime_marker:NULL;
    return create_runtime?ESP_OK:ESP_FAIL;
}
void tailtiny_runtime_snapshot(tailtiny_t *ml,tailtiny_runtime_status_t *out) {
    assert(create_runtime&&ml==(tailtiny_t *)&runtime_marker);
    memset(out,0,sizeof(*out));
    out->peer_diag=(tailtiny_peer_diag_t){
        65535,65535,65535,65535,65535,65535,65535,65535,65535,65535,65535,
        65535,65535,65535,65535,65535,65535,65535,65535,65535,65535,65535};
}
esp_err_t tailtiny_start(tailtiny_t *ml) {assert(create_runtime&&ml);return ESP_FAIL;}
esp_err_t tailtiny_stop(tailtiny_t *ml) {assert(create_runtime&&ml);stop_calls++;return stop_failure?ESP_FAIL:ESP_OK;}
void tailtiny_destroy(tailtiny_t *ml) {assert(ml&&diag_logs==1&&!stop_failure);destroy_calls++;}
tailtiny_state_t tailtiny_get_state(const tailtiny_t *ml) {(void)ml;abort();}
bool tailtiny_is_connected(const tailtiny_t *ml) {(void)ml;abort();}
uint32_t tailtiny_get_vpn_ip(const tailtiny_t *ml) {(void)ml;abort();}
tailtiny_tcp_socket_t *tailtiny_tcp_connect_until(tailtiny_t *ml,uint32_t ip,uint16_t port,
    uint64_t until,tailtiny_cancel_fn cancel,void *arg) {
    (void)ml;(void)ip;(void)port;(void)until;(void)cancel;(void)arg;abort();
}
int tailtiny_tcp_borrow_fd(const tailtiny_tcp_socket_t *tcp) {(void)tcp;abort();}
void tailtiny_tcp_close(tailtiny_tcp_socket_t *tcp) {(void)tcp;abort();}
esp_err_t tailnet_tls_probe(int fd,const char *host,uint16_t port,uint64_t until,
    tailtiny_cancel_fn cancel,void *arg,void (*http)(void *),tailnet_tls_result_t *out) {
    (void)fd;(void)host;(void)port;(void)until;(void)cancel;(void)arg;(void)http;(void)out;abort();
}
#include "spike-production.inc"

static unsigned cases;
static void reset(void) {
    memset(&status,0,sizeof(status));status.active=true;
    atomic_store(&owner,1);atomic_store(&cancelled,false);
    began=1000;overall=121000;runtime=NULL;memory_failed=false;
    clock_ms=1000;recover_at=cancel_at=floor_at=0;delay_calls=ram_logs=0;
    target_logs=0;
    logged_initial=logged_final=logged_max=logged_largest=logged_psram=logged_wait=logged_passed=0;
    paused_heap=false;invalid_target=false;
    net=(network_status_t){.connected=true};
    target_error=pause_error=resume_error=ESP_OK;
    pause_calls=resume_calls=init_calls=target_calls=0;stack_free=4096;
    create_runtime=stop_failure=false;stop_calls=destroy_calls=diag_logs=0;
    before[0]=after[0]=256*1024;
    before[1]=after[1]=64*1024;
    before[2]=after[2]=4*1024*1024;
    memcpy(recovered,after,sizeof(after));
}
static void completed(const char *name) {cases++;printf("PASS spike %s\n",name);}
static void restored(void) {
    assert(pause_calls==1&&resume_calls==1);
    assert(status.cleaned_up&&!status.restart_required&&!status.active&&!status.listener_paused);
    assert(atomic_load(&owner)==0);
    assert(tailnet_spike_audio_acquire());tailnet_spike_audio_release();
}
static void admitted(void) {
    assert(target_logs==1);
    assert(init_calls==1&&status.clock_qualified&&status.error_stage==TAILNET_IDENTITY);
    assert(status.error==ESP_FAIL);restored();
}
static void rejected(void) {
    assert(init_calls==0&&status.error==ESP_ERR_NO_MEM);
    assert(status.error_stage==TAILNET_PREFLIGHT);restored();
}
int main(void) {
    reset();after[0]=192031;recover_at=1100;run();
    assert(init_calls==1);admitted();assert(delay_calls==4);
    assert(ram_logs==1&&logged_initial==192031&&logged_final==256*1024&&logged_max==256*1024);
    assert(logged_wait==100&&logged_passed==1);completed("delayed-internal-reclaim-100ms");
    reset();before[0]=184375;before[1]=86016;before[2]=8292508;
    run();
    /* RED on old ordering: no pause/init occurs, even with sufficient after[]. */
    assert(pause_calls==1);admitted();
    assert(status.internal_min==184375); /* Historical minima must NOT gate admission. */
    assert(delay_calls==0&&logged_wait==0&&logged_passed==1&&ram_logs==1);
    completed("low-before-high-after");

    reset();after[0]=200*1024-1;run();rejected();completed("low-after-restores-once");
    reset();before[0]=184375;invalid_target=true;run();
    assert(!pause_calls&&!resume_calls&&!init_calls&&status.error==ESP_ERR_INVALID_STATE);
    assert(status.cleaned_up&&atomic_load(&owner)==0);completed("blank-target-no-pause");
    reset();target_error=ESP_FAIL;run();
    assert(!pause_calls&&!init_calls&&status.error==ESP_FAIL);completed("invalid-target-no-pause");
    reset();pause_error=ESP_ERR_TIMEOUT;run();
    assert(pause_calls==1&&!resume_calls&&!init_calls);
    assert(status.restart_required&&status.active&&!status.cleaned_up);
    assert(atomic_load(&owner)==1&&!tailnet_spike_audio_acquire());
    completed("pause-failure-retains-uncertainty-fence");
    reset();after[0]=200*1024-1;resume_error=ESP_FAIL;run();
    assert(pause_calls==1&&resume_calls==1&&!init_calls&&status.error==ESP_ERR_TIMEOUT);
    assert(status.restart_required&&status.active&&!status.cleaned_up&&status.listener_paused);
    assert(atomic_load(&owner)==1&&!tailnet_spike_audio_acquire());
    completed("restore-failure-retains-admission");

    const size_t thresholds[]={200*1024,32*1024,3*1024*1024};
    for(unsigned dimension=0;dimension<3;dimension++)for(int delta=-1;delta<=1;delta++) {
        reset();after[dimension]=thresholds[dimension]+delta;run();
        if(delta<0)rejected();else admitted();
        char name[64];snprintf(name,sizeof(name),"boundary-%u-%+d",dimension,delta);completed(name);
    }
    reset();atomic_store(&cancelled,true);run();
    assert(!pause_calls&&!init_calls&&status.state==TAILNET_CANCELLED);completed("cancel-before-pause");
    for(unsigned floor=0;floor<3;floor++) {
        reset();
        if(floor==0)before[0]=32*1024-1;
        if(floor==1)before[2]=512*1024-1;
        if(floor==2)stack_free=1023;
        run();assert(!pause_calls&&!init_calls&&status.error==ESP_ERR_NO_MEM);
        completed("runtime-floor-before-pause");
    }
    for(unsigned condition=0;condition<3;condition++) {
        reset();
        if(condition==0)net.connected=false;
        if(condition==1)net.joining=true;
        if(condition==2)net.setup_paused=true;
        run();assert(!target_calls&&!pause_calls&&!init_calls&&status.error==ESP_ERR_INVALID_STATE);
        completed("invalid-network-before-pause");
    }
    reset();create_runtime=true;run();
    assert(diag_logs==1&&destroy_calls==1);restored();completed("stable-peer-diag-once-before-destroy");
    reset();create_runtime=stop_failure=true;run();
    assert(diag_logs==1&&!destroy_calls&&!resume_calls&&status.restart_required&&atomic_load(&owner)==1);
    completed("live-peer-diag-once-retains-fence");
    for(unsigned dimension=0;dimension<3;dimension++) {
        reset();after[dimension]=thresholds[dimension]-1;recover_at=1100;run();admitted();
        assert(delay_calls==4&&logged_wait==100&&logged_passed==1);
        completed("delayed-each-tuple-dimension");
    }
    reset();after[0]=192031;after[1]=32767;after[2]=3*1024*1024-1;run();rejected();
    assert(clock_ms==3000&&delay_calls==80&&ram_logs==1&&logged_wait==2000&&!logged_passed);
    completed("never-recover-2s-restore-once");
    reset();after[0]=192031;cancel_at=1100;run();
    assert(!init_calls&&status.error==ESP_ERR_TIMEOUT&&status.state==TAILNET_CANCELLED);
    assert(delay_calls==4&&ram_logs==1&&!logged_passed);restored();completed("cancel-during-reclaim-wait");
    reset();after[0]=192031;overall=1113;run();
    assert(!init_calls&&status.error==ESP_ERR_TIMEOUT&&clock_ms==1113&&delay_calls==5);
    assert(ram_logs==1&&logged_wait==113&&!logged_passed);restored();completed("global-deadline-caps-final-yield");
    reset();after[0]=192031;floor_at=1100;run();
    assert(!init_calls&&status.error==ESP_ERR_NO_MEM&&memory_failed);
    assert(delay_calls==4&&ram_logs==1&&!logged_passed);restored();completed("runtime-floor-during-wait");
    reset();after[0]=192031;recover_at=1100;cancel_at=1100;run();
    assert(!init_calls&&status.error==ESP_ERR_TIMEOUT&&status.state==TAILNET_CANCELLED);
    assert(!logged_passed);restored();completed("cancel-wins-simultaneous-recovery");
    reset();after[0]=192031;recover_at=1100;overall=1100;run();
    assert(!init_calls&&status.error==ESP_ERR_TIMEOUT&&!logged_passed);
    restored();completed("global-deadline-wins-simultaneous-recovery");
    reset();after[0]=192031;recover_at=3000;run();admitted();
    assert(delay_calls==80&&logged_wait==2000&&logged_passed);completed("recovery-at-local-limit");
    reset();after[0]=192031;recover_at=3025;run();rejected();
    assert(delay_calls==80&&logged_wait==2000&&!logged_passed);completed("no-wait-beyond-local-limit");
    printf("PASS spike total=%u (actual production control flow; no allocator/hardware proof)\n",cases);
    return 0;
}