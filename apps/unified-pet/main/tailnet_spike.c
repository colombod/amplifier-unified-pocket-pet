#include "tailnet_spike.h"
#include "tailnet_tls.h"
#include "network.h"
#include "audio_test.h"
#include "live_voice.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_sntp.h"
#include "sdkconfig.h"
#include "driver/usb_serial_jtag.h"
#include <stdatomic.h>
#include <string.h>
#include <stdio.h>
#include <time.h>
#if !CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
#error "Tailnet command receiver requires the configured USB Serial/JTAG console"
#endif
#if CONFIG_ML_ENABLE_CONFIG_HTTPD || CONFIG_ML_ENABLE_CELLULAR || CONFIG_ML_ZERO_COPY_WG || !CONFIG_ML_DERP_VERIFY_CERT
#error "Unsafe TailTiny spike configuration"
#endif
static SemaphoreHandle_t lock;
static QueueHandle_t jobs;
static tailnet_spike_status_t status={.state=TAILNET_IDLE,.cleaned_up=true};
static atomic_int owner; /* 0 idle; 1 tailnet (including retained cleanup); 2 audio */
static atomic_bool cancelled;
static uint64_t began,overall;
static tailtiny_t *runtime;
static bool memory_failed;
static uint64_t now(void) {return (uint64_t)esp_timer_get_time()/1000;}
const char *tailnet_spike_state_name(tailnet_spike_state_t state) {
    static const char *names[]={"IDLE","PREFLIGHT","TIME","IDENTITY","JOIN","PEER","TCP","TLS","HTTP","CLEANUP","PASS","ERROR","CANCELLED"};
    return state<=TAILNET_CANCELLED?names[state]:"ERROR";
}
bool tailnet_spike_audio_acquire(void) {
    audio_test_reap_finished();live_voice_reap_finished();
    int expected=0;return atomic_compare_exchange_strong(&owner,&expected,2);
}
void tailnet_spike_audio_release(void) {int expected=2;atomic_compare_exchange_strong(&owner,&expected,0);}
void tailnet_spike_snapshot(tailnet_spike_status_t *out) {
    if(!out)return;
    if(!lock){*out=status;return;}
    xSemaphoreTake(lock,portMAX_DELAY);*out=status;
    if(out->active)out->elapsed_ms=(uint32_t)(now()-began);
    xSemaphoreGive(lock);
    network_status_t net;network_snapshot(&net);out->key_present=net.tailscale_key_saved;
}
static void stage(tailnet_spike_state_t state) {
    xSemaphoreTake(lock,portMAX_DELAY);status.state=state;
    if(state<TAILNET_PASS)status.stage_ms[state]=(uint32_t)(now()-began);
    xSemaphoreGive(lock);
}
static uint64_t deadline(unsigned ms) {uint64_t end=now()+ms;return end<overall?end:overall;}
static bool sample(void *arg) {
    (void)arg;
    size_t in=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    size_t large=heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    size_t ps=heap_caps_get_free_size(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    unsigned stack=uxTaskGetStackHighWaterMark(NULL);
    tailtiny_runtime_status_t r={0};if(runtime)tailtiny_runtime_snapshot(runtime,&r);
    if(r.stack_min && r.stack_min<stack)stack=r.stack_min;
    xSemaphoreTake(lock,portMAX_DELAY);
    if(!status.internal_min||in<status.internal_min)status.internal_min=in;
    if(!status.largest_internal_min||large<status.largest_internal_min)status.largest_internal_min=large;
    if(!status.psram_min||ps<status.psram_min)status.psram_min=ps;
    if(!status.stack_min||stack<status.stack_min)status.stack_min=stack;
    xSemaphoreGive(lock);
    /* Provisional engineering floors, NOT measured product envelopes. */
    if(in<32*1024||ps<512*1024||stack<1024)memory_failed=true;
    return memory_failed||atomic_load(&cancelled)||now()>=overall;
}
static bool pause_wait(uint64_t until) {
    if(sample(NULL)||now()>=until)return false;
    vTaskDelay(pdMS_TO_TICKS(25));return true;
}
static void entered_http(void *arg) {(void)arg;stage(TAILNET_HTTP);}
static void run(void) {
    esp_err_t err=ESP_OK;bool paused=false,pause_uncertain=false,own_sntp=false;
    tailtiny_tcp_socket_t *tcp=NULL;tailnet_tls_result_t tls={0};
    setup_unified_target_t target;memory_failed=false;runtime=NULL;
    stage(TAILNET_PREFLIGHT);sample(NULL);
    network_status_t net;network_snapshot(&net);
    xSemaphoreTake(lock,portMAX_DELAY);status.key_present=net.tailscale_key_saved;xSemaphoreGive(lock);
    if(!net.connected||net.joining||net.setup_paused){err=ESP_ERR_INVALID_STATE;goto cleanup;}
    err=network_copy_unified_target(&target);
    if(err!=ESP_OK||!target.host[0]){if(err==ESP_OK)err=ESP_ERR_INVALID_STATE;goto cleanup;}
    /* Temporary qualification diagnostic: validated user-owned server, never a key. */
    ESP_LOGI("tailnet_spike","validated_target host=%.63s port=%u",target.host,(unsigned)target.port);
    if(sample(NULL)){err=ESP_ERR_TIMEOUT;goto cleanup;}
    err=network_pause_setup(3000);
    if(err!=ESP_OK){pause_uncertain=true;goto cleanup;}
    paused=true;
    xSemaphoreTake(lock,portMAX_DELAY);status.listener_paused=true;xSemaphoreGive(lock);
    size_t current_internal=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    size_t current_largest_internal=heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    size_t current_psram=heap_caps_get_free_size(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    size_t initial_internal=current_internal,max_internal=current_internal;
    uint64_t reclaim_began=now(),reclaim_until=deadline(2000);
    bool ram_passed=false;
    for(;;) {
        /* Listener ACK precedes asynchronous HTTP task-stack reclamation.
         * Cancellation, runtime floors and the global deadline still win. */
        if(sample(NULL)){err=ESP_ERR_TIMEOUT;break;}
        if(!(current_internal<200*1024||current_largest_internal<32*1024||
             current_psram<3*1024*1024)){ram_passed=true;break;}
        uint64_t current_time=now();
        if(current_time>=overall){err=ESP_ERR_TIMEOUT;break;}
        if(current_time>=reclaim_until){err=ESP_ERR_NO_MEM;break;}
        unsigned wait_ms=(unsigned)(reclaim_until-current_time);
        if(wait_ms>25)wait_ms=25;
        vTaskDelay(pdMS_TO_TICKS(wait_ms));
        current_internal=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
        current_largest_internal=heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
        current_psram=heap_caps_get_free_size(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
        if(current_internal>max_internal)max_internal=current_internal;
    }
    ESP_LOGI("tailnet_spike","preflight_ram initial=%u final=%u max=%u largest=%u psram=%u waitMs=%u passed=%u",
        (unsigned)initial_internal,(unsigned)current_internal,(unsigned)max_internal,
        (unsigned)current_largest_internal,(unsigned)current_psram,
        (unsigned)(now()-reclaim_began),(unsigned)ram_passed);
    if(!ram_passed)goto cleanup;
    stage(TAILNET_TIME);
    if(!esp_sntp_enabled()) {
        own_sntp=true;esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
        esp_sntp_setservername(0,"time.cloudflare.com");esp_sntp_setservername(1,"pool.ntp.org");esp_sntp_init();
    }
    uint64_t until=deadline(15000);
    while(time(NULL)<1760000000&&pause_wait(until)){}
    if(sample(NULL)||time(NULL)<1760000000){err=ESP_ERR_TIMEOUT;goto cleanup;}
    xSemaphoreTake(lock,portMAX_DELAY);status.clock_qualified=true;xSemaphoreGive(lock);
    stage(TAILNET_IDENTITY);
    tailtiny_config_t cfg={.auth_key="",.copy_bootstrap_key=network_copy_tailscale_auth_key,
        .peer_fqdn=target.host,.peer_port=target.port,.max_peers=1,
        .enable_stun=true,.enable_disco=true,.enable_derp=true};
    err=tailtiny_init_checked(&cfg,&runtime);
    if(err!=ESP_OK)goto cleanup;
    tailtiny_runtime_status_t r;tailtiny_runtime_snapshot(runtime,&r);
    xSemaphoreTake(lock,portMAX_DELAY);
    status.identity_loaded=r.identity_loaded;status.persisted=r.persisted;status.bootstrap=r.bootstrap;
    memcpy(status.fingerprint,r.fingerprint,sizeof(status.fingerprint));xSemaphoreGive(lock);
    if(sample(NULL)){err=ESP_ERR_TIMEOUT;goto cleanup;}
    stage(TAILNET_JOIN);err=tailtiny_start(runtime);
    if(err!=ESP_OK)goto cleanup;
    until=deadline(60000);
    while(!tailtiny_is_connected(runtime)&&tailtiny_get_state(runtime)!=ML_STATE_ERROR&&pause_wait(until)){}
    if(sample(NULL)||!tailtiny_is_connected(runtime)){err=ESP_ERR_TIMEOUT;goto cleanup;}
    if(!tailtiny_get_vpn_ip(runtime)){err=ESP_ERR_INVALID_STATE;goto cleanup;}
    xSemaphoreTake(lock,portMAX_DELAY);status.joined=true;xSemaphoreGive(lock);
    stage(TAILNET_PEER);until=deadline(15000);
    do {tailtiny_runtime_snapshot(runtime,&r);if(r.peer_ip||r.peer_conflict)break;}while(pause_wait(until));
    if(sample(NULL)||!r.peer_ip||r.peer_conflict){err=ESP_ERR_NOT_FOUND;goto cleanup;}
    xSemaphoreTake(lock,portMAX_DELAY);status.peer=true;xSemaphoreGive(lock);
    stage(TAILNET_TCP);
    tcp=tailtiny_tcp_connect_until(runtime,r.peer_ip,target.port,deadline(15000),sample,NULL);
    if(!tcp){err=ESP_FAIL;goto cleanup;}
    xSemaphoreTake(lock,portMAX_DELAY);status.tcp=true;xSemaphoreGive(lock);
    stage(TAILNET_TLS);
    err=tailnet_tls_probe(tailtiny_tcp_borrow_fd(tcp),target.host,target.port,deadline(15000),sample,NULL,entered_http,&tls);
    xSemaphoreTake(lock,portMAX_DELAY);
    status.tls=tls.verified;status.http=tls.http_complete;status.http_status=tls.http_status;
    status.tls_error=tls.tls_error;status.tls_verify_flags=tls.verify_flags;
    xSemaphoreGive(lock);
cleanup:
    xSemaphoreTake(lock,portMAX_DELAY);status.error_stage=status.state;xSemaphoreGive(lock);
    stage(TAILNET_CLEANUP);uint64_t cleanup_began=now();
    if(tcp)tailtiny_tcp_close(tcp);
    bool clean=true;
    if(runtime) {
        esp_err_t stopped=tailtiny_stop(runtime);
        tailtiny_runtime_status_t last;tailtiny_runtime_snapshot(runtime,&last);
        const tailtiny_peer_diag_t *d=&last.peer_diag;
        /* live; full,delta,entries,nameMatch,dnsMatch,duplicate,addr0IPv4,anyTailnetIPv4,
         * keyValid,allocFail,queued,queueFail,removed,patches,addDequeued,policyAccepted,
         * policyRejected,allowlistRejected,slotRejected,missingNetif,wgInstalled,wgFailed. */
        ESP_LOGI("tailnet_spike","peer_diag live=%u [%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u]",
            (unsigned)(stopped!=ESP_OK),(unsigned)d->full,(unsigned)d->delta,(unsigned)d->entries,
            (unsigned)d->name_match,(unsigned)d->dns_match,(unsigned)d->duplicate,
            (unsigned)d->addr0_ipv4,(unsigned)d->any_tailnet_ipv4,(unsigned)d->key_valid,
            (unsigned)d->alloc_fail,(unsigned)d->queued,(unsigned)d->queue_fail,
            (unsigned)d->removed,(unsigned)d->patches,(unsigned)d->add_dequeued,
            (unsigned)d->policy_accepted,(unsigned)d->policy_rejected,(unsigned)d->allowlist_rejected,
            (unsigned)d->slot_rejected,(unsigned)d->missing_netif,(unsigned)d->wg_installed,(unsigned)d->wg_failed);
        if(stopped==ESP_OK){
            tailtiny_destroy(runtime);runtime=NULL;
            xSemaphoreTake(lock,portMAX_DELAY);
            if(last.stack_min&&last.stack_min<status.stack_min)status.stack_min=last.stack_min;
            xSemaphoreGive(lock);
        }else {clean=false;err=stopped;}
    }
    if(own_sntp)esp_sntp_stop();
    if(pause_uncertain)clean=false;
    if(clean&&paused) {
        unsigned remaining=now()-cleanup_began<9500?9500-(unsigned)(now()-cleanup_began):0;
        if(!remaining||network_resume_setup(remaining)!=ESP_OK){clean=false;err=ESP_ERR_TIMEOUT;}
    }
    if(memory_failed)err=ESP_ERR_NO_MEM;
    xSemaphoreTake(lock,portMAX_DELAY);
    status.cleaned_up=clean;status.restart_required=!clean;status.active=!clean;
    status.elapsed_ms=(uint32_t)(now()-began);status.error=err;
    if(clean)status.listener_paused=false;
    status.state=!clean?TAILNET_ERROR:atomic_load(&cancelled)?TAILNET_CANCELLED:err==ESP_OK?TAILNET_PASS:TAILNET_ERROR;
    xSemaphoreGive(lock);
    if(clean)atomic_store(&owner,0);
}
static void worker(void *arg) {
    (void)arg;for(;;){bool job;if(xQueueReceive(jobs,&job,portMAX_DELAY)==pdTRUE)run();}
}
esp_err_t tailnet_spike_start(void) {
    if(!lock||!jobs)return ESP_ERR_INVALID_STATE;
    audio_test_reap_finished();live_voice_reap_finished();
    int expected=0;
    if(!atomic_compare_exchange_strong(&owner,&expected,1))return ESP_ERR_INVALID_STATE;
    /* Audio/voice use this same gate. Snapshots additionally reject old activity. */
    audio_test_status_t a;audio_test_snapshot(&a);live_status_t v;live_voice_snapshot(&v);
    if(a.busy||v.active){atomic_store(&owner,0);return ESP_ERR_INVALID_STATE;}
    xSemaphoreTake(lock,portMAX_DELAY);memset(&status,0,sizeof(status));
    status.active=true;status.state=TAILNET_PREFLIGHT;began=now();overall=began+120000;
    atomic_store(&cancelled,false);xSemaphoreGive(lock);
    bool job=true;
    if(xQueueSend(jobs,&job,0)!=pdTRUE) {
        xSemaphoreTake(lock,portMAX_DELAY);status.active=false;status.cleaned_up=true;status.state=TAILNET_ERROR;status.error=ESP_ERR_INVALID_STATE;xSemaphoreGive(lock);
        atomic_store(&owner,0);return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}
void tailnet_spike_cancel(void) {atomic_store(&cancelled,true);}
static void report(esp_err_t command_error) {
    tailnet_spike_status_t s;tailnet_spike_snapshot(&s);char line[1400];
    int n=snprintf(line,sizeof(line),"{\"tailnet\":\"%s\",\"command_error\":%d,\"listener_paused\":%s,\"clock_qualified\":%s,\"active\":%s,\"cleaned_up\":%s,\"restart_required\":%s,\"key_present\":%s,\"identity_loaded\":%s,\"persisted\":%s,\"bootstrap\":%s,\"joined\":%s,\"peer\":%s,\"tcp\":%s,\"tls\":%s,\"http\":%s,\"http_status\":%d,\"error\":%d,\"error_stage\":%d,\"tls_error\":%d,\"verify_flags\":%lu,\"elapsed_ms\":%lu,\"internal_min\":%lu,\"largest_internal_min\":%lu,\"psram_min\":%lu,\"stack_min\":%lu,\"fingerprint\":\"%s\",\"stages_ms\":[%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu]}\n",
        tailnet_spike_state_name(s.state),command_error,s.listener_paused?"true":"false",s.clock_qualified?"true":"false",
        s.active?"true":"false",s.cleaned_up?"true":"false",s.restart_required?"true":"false",
        s.key_present?"true":"false",s.identity_loaded?"true":"false",s.persisted?"true":"false",s.bootstrap?"true":"false",
        s.joined?"true":"false",s.peer?"true":"false",s.tcp?"true":"false",s.tls?"true":"false",s.http?"true":"false",
        s.http_status,s.error,s.error_stage,s.tls_error,(unsigned long)s.tls_verify_flags,(unsigned long)s.elapsed_ms,
        (unsigned long)s.internal_min,(unsigned long)s.largest_internal_min,(unsigned long)s.psram_min,(unsigned long)s.stack_min,s.fingerprint,
        (unsigned long)s.stage_ms[1],(unsigned long)s.stage_ms[2],(unsigned long)s.stage_ms[3],(unsigned long)s.stage_ms[4],
        (unsigned long)s.stage_ms[5],(unsigned long)s.stage_ms[6],(unsigned long)s.stage_ms[7],(unsigned long)s.stage_ms[8],(unsigned long)s.stage_ms[9]);
    if(n>0&&(size_t)n<sizeof(line))usb_serial_jtag_write_bytes(line,n,pdMS_TO_TICKS(100));
}
static void console(void *arg) {
    (void)arg;char line[32];size_t n=0;bool discard=false;
    for(;;) {
        char c;if(usb_serial_jtag_read_bytes(&c,1,pdMS_TO_TICKS(100))!=1)continue;
        if(c=='\r'||c=='\n') {
            line[n]=0;
            if(!discard) {
                if(!strcmp(line,"tailnet start"))report(tailnet_spike_start());
                else if(!strcmp(line,"tailnet cancel")){tailnet_spike_cancel();report(ESP_OK);}
                else if(!strcmp(line,"tailnet status"))report(ESP_OK);
#if defined(PET_PRIVATE_MIC_DIAG)
                else if(!strcmp(line,"voice on")){
                    live_voice_start();
                    static const char ack[]="pet-bench-v1 voice on\n";
                    usb_serial_jtag_write_bytes(ack,sizeof(ack)-1,pdMS_TO_TICKS(100));
                }
                else if(!strcmp(line,"voice core 0")||!strcmp(line,"voice core 1")||!strcmp(line,"voice core any")){
                    live_voice_bench_core(line[11]=='a'?-1:line[11]-'0');
                    static const char ack[]="pet-bench-v1 voice core\n";
                    usb_serial_jtag_write_bytes(ack,sizeof(ack)-1,pdMS_TO_TICKS(100));
                }
                else if(!strcmp(line,"voice off")){
                    live_voice_stop();
                    static const char ack[]="pet-bench-v1 voice off\n";
                    usb_serial_jtag_write_bytes(ack,sizeof(ack)-1,pdMS_TO_TICKS(100));
                }
#endif
            }
            memset(line,0,sizeof(line));n=0;discard=false;
        } else if((unsigned char)c<32||(unsigned char)c>126||n>=sizeof(line)-1)discard=true;
        else if(!discard)line[n++]=c;
    }
}
esp_err_t tailnet_spike_init(void) {
    lock=xSemaphoreCreateMutex();jobs=xQueueCreate(1,sizeof(bool));
    if(!lock||!jobs)return ESP_ERR_NO_MEM;
    if(xTaskCreate(worker,"tailnet_spike",16384,NULL,3,NULL)!=pdPASS)return ESP_ERR_NO_MEM;
    /* USB Serial/JTAG, NOT UART0. Leave stdout's existing nonblocking VFS
     * mode alone; direct driver reads have finite waits and never echo input. */
    if(!usb_serial_jtag_is_driver_installed()) {
        usb_serial_jtag_driver_config_t cfg={.rx_buffer_size=256,.tx_buffer_size=2048};
        esp_err_t e=usb_serial_jtag_driver_install(&cfg);if(e!=ESP_OK)return e;
    }
    if(xTaskCreate(console,"tailnet_usb",4096,NULL,2,NULL)!=pdPASS)return ESP_ERR_NO_MEM;
    return ESP_OK;
}