/* Actual capture/send/session/lifecycle, real pthreads + mbedTLS base64.
 * Synthetic 960-byte blocks at 20ms. No provider/socket/hardware invocation. */
#include "voice_host.h"
#define calloc host_calloc
#define free host_free
#define mbedtls_base64_encode host_encode
#include "voice-production.inc"
#undef calloc
#undef free
#undef mbedtls_base64_encode

struct host_ws { live_connection_t *connection; };
static _Thread_local TaskHandle_t self;
static TaskHandle_t audio_tasks_host[2];
static atomic_bool admission;
static atomic_uint reads, opens, closes, capture_exits, playback_exits;
static atomic_uint allocations, wipes, wire_bytes, queue_peak, successful_reads, accepted_bytes;
static unsigned send_calls, partial_injections, successful_sends;
static unsigned case_ms=1400;
static const char *scenario;
static int64_t session_start;
static struct audio_session codec;
static live_connection_t *connection;
static bool observed_metrics;
static unsigned metrics_dropped, metrics_uncertain, metrics_overflows;
static uint64_t metrics_captured, metrics_sent;
static unsigned metrics_stage, metrics_gap, metrics_read_us, metrics_send_us, metrics_peak;
static _Thread_local unsigned held;
static _Thread_local bool partial_unlock,encoded_pending,actor;
static pthread_mutex_t gate=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed=PTHREAD_COND_INITIALIZER;
static bool partial_ready,sender_done,encode_ready,action_done,snapshot_ready,snapshot_attempted,snapshot_done;
static bool stall_overflow;
static unsigned close_calls,snapshot_reads;
static bool is_case(const char *name) {return !strcmp(scenario,name);}
static bool cap_case(void) {return scenario&&!strncmp(scenario,"cap-",4);}
static atomic_int_fast64_t fake_us;
static int64_t accepted_start_us,previous_cap_start;
static bool cap_capture_done;
static atomic_uint read_begin_count;
static unsigned read_wall_max_us;
static bool fail_worker_creation;
static void signal_flag(bool *flag) {
    assert(!pthread_mutex_lock(&gate));*flag=true;assert(!pthread_cond_broadcast(&changed));assert(!pthread_mutex_unlock(&gate));
}
static void wait_flag(bool *flag) {
    assert(!pthread_mutex_lock(&gate));
    struct timespec until;assert(!clock_gettime(CLOCK_REALTIME,&until));until.tv_sec+=4;
    while(!*flag)assert(!pthread_cond_timedwait(&changed,&gate,&until));
    assert(!pthread_mutex_unlock(&gate));
}

static int64_t wall_us(void) {
    struct timespec t;assert(!clock_gettime(CLOCK_MONOTONIC,&t));
    return (int64_t)t.tv_sec*1000000+t.tv_nsec/1000;
}
static int64_t esp_timer_get_time(void) {return cap_case()?atomic_load(&fake_us):wall_us();}
static unsigned xTaskGetTickCount(void) {
    if(is_case("cap-queue-boundary")&&self==audio_tasks_host[0])
        atomic_store(&fake_us,accepted_start_us+60000000);
    return (unsigned)(esp_timer_get_time()/1000);
}
static void vTaskDelay(unsigned ms) {
    struct timespec t={.tv_sec=ms/1000,.tv_nsec=(long)(ms%1000)*1000000};
    while(nanosleep(&t,&t)) {}
}
static size_t host_strlcpy(char *dst,const char *src,size_t size) {
    size_t n=strlen(src);if(size){size_t k=n<size-1?n:size-1;memcpy(dst,src,k);dst[k]=0;}return n;
}
static void host_log(const char *tag,const char *fmt,...) {
    (void)tag;
    /* Only the numeric production pipeline summary is emitted by this harness. */
    if(strncmp(fmt,"Mic pipeline ",13))return;
    va_list args;va_start(args,fmt);
    char text[768];assert(vsnprintf(text,sizeof(text),fmt,args)>0);va_end(args);
    if(actor) {
        unsigned long long captured,queued,dequeued,sent,dropped,uncertain;
        assert(sscanf(text,"Mic pipeline captured=%llu queued=%llu dequeued=%llu sent=%llu dropped=%llu uncertain=%llu",
                      &captured,&queued,&dequeued,&sent,&dropped,&uncertain)==6);
        /* Capture was paused inside Send, before production queue accounting.
         * The actual pipeline_log must not expose that half-updated snapshot. */
        assert(captured==960&&queued==960&&dequeued<=queued&&!dropped&&!uncertain&&sent<=dequeued);
        snapshot_reads++;
    }
    puts(text);
}
static SemaphoreHandle_t xSemaphoreCreateMutex(void) {
    SemaphoreHandle_t p=malloc(sizeof(*p));assert(p);assert(!pthread_mutex_init(&p->mutex,NULL));return p;
}
static void xSemaphoreTake(SemaphoreHandle_t p,int wait) {
    (void)wait;
    if(actor&&is_case("snapshot"))signal_flag(&snapshot_attempted);
    if(encoded_pending&&is_case("failure-encode-lock")) {
        encoded_pending=false;signal_flag(&encode_ready);wait_flag(&action_done);
    }
    assert(!held);assert(!pthread_mutex_lock(&p->mutex));held=1;
}
static void xSemaphoreGive(SemaphoreHandle_t p) {
    assert(held==1);held=0;assert(!pthread_mutex_unlock(&p->mutex));
    if(partial_unlock) {
        partial_unlock=false;signal_flag(&partial_ready);
        /* Capture is descheduled immediately AFTER queue-accounting unlock.
         * Sender runs until append or cleanup while capture cannot publish later. */
        wait_flag(&sender_done);
    }
}
static void *entry(void *p) {self=p;self->fn(self->arg);abort();}
static _Thread_local bool pinned_creation;
static int xTaskCreate(void (*fn)(void *),const char *name,unsigned stack,void *arg,unsigned priority,TaskHandle_t *out) {
    bool main_worker=!strcmp(name,"pet_live");
    assert(!main_worker||pinned_creation);
    assert(stack==(main_worker?12288:4096)&&priority==(main_worker?6:5));
    if(main_worker&&fail_worker_creation)return 0;
    TaskHandle_t p=calloc(1,sizeof(*p));assert(p);p->fn=fn;p->arg=arg;
    if(out)*out=p;else {
        unsigned index=!strcmp(name,"live_capture")?0:1;
        assert(!audio_tasks_host[index]);audio_tasks_host[index]=p;
    }
    assert(!pthread_create(&p->thread,NULL,entry,p));return pdPASS;
}
static int xTaskCreatePinnedToCore(void (*fn)(void *),const char *name,unsigned stack,void *arg,unsigned priority,TaskHandle_t *out,int core) {
    assert(!strcmp(name,"pet_live")&&core==1&&priority==6);
    pinned_creation=true;
    int result=xTaskCreate(fn,name,stack,arg,priority,out);
    pinned_creation=false;return result;
}
static void vTaskSuspend(TaskHandle_t p) {
    assert(!p&&self);atomic_store(&self->state,eSuspended);pthread_exit(NULL);
}
static eTaskState eTaskGetState(TaskHandle_t p) {return atomic_load(&p->state);}
static void vTaskDelete(TaskHandle_t p) {
    if(!p) {
        assert(self);if(self==audio_tasks_host[0])atomic_fetch_add(&capture_exits,1);
        else {assert(self==audio_tasks_host[1]);atomic_fetch_add(&playback_exits,1);}
        if(self==audio_tasks_host[0]&&(is_case("failure-encode")||is_case("failure-encode-lock")||is_case("admitted-failure")))
            signal_flag(&action_done);
        if(self==audio_tasks_host[0]&&cap_case())signal_flag(&cap_capture_done);
        pthread_exit(NULL);
    }
    assert(atomic_load(&p->state)==eSuspended&&!atomic_load(&opens));
    assert(!pthread_join(p->thread,NULL));free(p);
}
static StreamBufferHandle_t xStreamBufferCreateStatic(size_t size,size_t trigger,uint8_t *memory,StaticStreamBuffer_t *meta) {
    *meta=(StaticStreamBuffer_t){.capacity=size-1,.trigger=trigger,.memory=memory};
    assert(!pthread_mutex_init(&meta->mutex,NULL));assert(!pthread_cond_init(&meta->changed,NULL));return meta;
}
static size_t xStreamBufferBytesAvailable(StreamBufferHandle_t p) {
    assert(!pthread_mutex_lock(&p->mutex));size_t n=p->used;assert(!pthread_mutex_unlock(&p->mutex));return n;
}
static size_t xStreamBufferSend(StreamBufferHandle_t p,const void *src,size_t size,unsigned wait) {
    assert(!wait);assert(!pthread_mutex_lock(&p->mutex));
    size_t n=size<p->capacity-p->used?size:p->capacity-p->used;
    /* A legal partial RTOS return, isolated from the scheduling experiments. */
    if((is_case("partial")||is_case("partial-race"))&&!partial_injections){
        n=479;partial_injections++;if(is_case("partial-race"))partial_unlock=true;
    }
    for(size_t i=0;i<n;i++){p->memory[p->tail]=((const uint8_t *)src)[i];p->tail=(p->tail+1)%p->capacity;}
    if(is_case("stall") && n<size) signal_flag(&stall_overflow);
    p->used+=n;
    atomic_fetch_add(&accepted_bytes,n);
    if(p->used>atomic_load(&queue_peak))atomic_store(&queue_peak,p->used);
    assert(!pthread_cond_signal(&p->changed));assert(!pthread_mutex_unlock(&p->mutex));
    if(is_case("snapshot")&&!snapshot_ready) {
        signal_flag(&snapshot_ready);wait_flag(&snapshot_attempted);
    }
    return n;
}
static size_t xStreamBufferReceive(StreamBufferHandle_t p,void *dst,size_t size,unsigned wait) {
    if(is_case("partial-race"))wait_flag(&partial_ready);
    assert(!pthread_mutex_lock(&p->mutex));
    if(!p->used&&wait) {
        struct timespec until;assert(!clock_gettime(CLOCK_REALTIME,&until));
        until.tv_nsec+=(long)wait*1000000;until.tv_sec+=until.tv_nsec/1000000000;until.tv_nsec%=1000000000;
        while(p->used<p->trigger)if(pthread_cond_timedwait(&p->changed,&p->mutex,&until))break;
    }
    size_t n=size<p->used?size:p->used;
    for(size_t i=0;i<n;i++){((uint8_t *)dst)[i]=p->memory[p->head];p->head=(p->head+1)%p->capacity;}
    p->used-=n;assert(!pthread_mutex_unlock(&p->mutex));
    if(is_case("stop-receive")&&n) {signal_flag(&encode_ready);wait_flag(&action_done);}
    return n;
}
static void vStreamBufferDelete(StreamBufferHandle_t p) {
    assert(!pthread_cond_destroy(&p->changed));assert(!pthread_mutex_destroy(&p->mutex));
}
static EventGroupHandle_t xEventGroupCreate(void) {
    EventGroupHandle_t p=calloc(1,sizeof(*p));assert(p);
    assert(!pthread_mutex_init(&p->mutex,NULL));assert(!pthread_cond_init(&p->changed,NULL));return p;
}
static unsigned xEventGroupSetBits(EventGroupHandle_t p,unsigned bits) {
    assert(!pthread_mutex_lock(&p->mutex));p->bits|=bits;unsigned value=p->bits;
    assert(!pthread_cond_signal(&p->changed));assert(!pthread_mutex_unlock(&p->mutex));return value;
}
static unsigned xEventGroupWaitBits(EventGroupHandle_t p,unsigned bits,int clear,int all,int wait) {
    if(is_case("partial-race"))signal_flag(&sender_done);
    assert(!clear&&all&&wait==portMAX_DELAY);assert(!pthread_mutex_lock(&p->mutex));
    while((p->bits&bits)!=bits)assert(!pthread_cond_wait(&p->changed,&p->mutex));
    assert(!pthread_mutex_unlock(&p->mutex));
    /* Event bit alone is not host pthread exit: join before any buffer free. */
    for(unsigned i=0;i<2;i++)if((bits&(1u<<i))&&!audio_tasks_host[i]->joined) {
        assert(!pthread_join(audio_tasks_host[i]->thread,NULL));audio_tasks_host[i]->joined=true;
    }
    return bits;
}
static void vEventGroupDelete(EventGroupHandle_t p) {
    assert(!pthread_mutex_destroy(&p->mutex));assert(!pthread_cond_destroy(&p->changed));free(p);
}
static void *host_calloc(size_t n,size_t size) {
    assert(n==1&&size==sizeof(live_connection_t));connection=calloc(n,size);
    atomic_fetch_add(&allocations,1);return connection;
}
/* Check full zeroization immediately before actual free, including queued PCM. */
static struct { void *p; size_t size; } buffers[6];
static void *heap_caps_malloc(size_t size,unsigned flags) {
    assert(flags==(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    for(unsigned i=0;i<6;i++)if(!buffers[i].p) {
        void *p=malloc(size);assert(p);memset(p,0xa5,size);buffers[i].p=p;buffers[i].size=size;
        atomic_fetch_add(&allocations,1);return p;
    }
    abort();
}
static void host_free(void *p) {
    if(p==connection) {
#ifdef PET_VOICE_METRICS
        observed_metrics=true;
        metrics_captured=connection->pipeline.captured;
        metrics_sent=connection->pipeline.sent;
        metrics_dropped=connection->pipeline.dropped;
        metrics_uncertain=connection->pipeline.uncertain;
        metrics_overflows=connection->pipeline.overflows;
        metrics_stage=connection->pipeline.stage;
        metrics_gap=connection->pipeline.max_dequeue_gap_us;
        metrics_read_us=connection->pipeline.max_read_us;
        metrics_send_us=connection->pipeline.max_send_us;
        metrics_peak=connection->pipeline.highwater;
        assert(connection->pipeline.queued==atomic_load(&accepted_bytes));
        assert(metrics_captured==(uint64_t)atomic_load(&successful_reads)*960);
        assert(connection->pipeline.current==0);
        assert(metrics_captured==metrics_sent+metrics_dropped+metrics_uncertain);
#endif
        connection=NULL;atomic_fetch_sub(&allocations,1);free(p);return;
    }
    for(unsigned i=0;i<6;i++)if(buffers[i].p==p) {
        for(size_t j=0;j<buffers[i].size;j++)assert(((uint8_t *)p)[j]==0);
        buffers[i].p=NULL;atomic_fetch_add(&wipes,1);atomic_fetch_sub(&allocations,1);free(p);return;
    }
    abort();
}
static unsigned heap_caps_get_free_size(unsigned flags) {(void)flags;return 9999999;}
void audio_test_snapshot(audio_test_status_t *p) {*p=(audio_test_status_t){.volume=50};}
esp_err_t audio_stream_open(audio_session_t **p) {
    assert(atomic_load(&admission)&&!atomic_exchange(&opens,1));*p=&codec;session_start=esp_timer_get_time();
    if(is_case("cap-connect-origin"))assert(session_start==accepted_start_us+20000000);
    if(is_case("cap-read-boundary")||is_case("cap-read-error"))
        atomic_store(&fake_us,accepted_start_us+59999000);
    return ESP_OK;
}
esp_err_t audio_stream_read(audio_session_t *p,void *pcm,unsigned size) {
    assert(p==&codec&&size==960&&atomic_load(&opens));
    if(cap_case()) {
        /* Test oracle uses the exact specified interval, not production policy. */
        assert(esp_timer_get_time()<accepted_start_us+60000000);
        atomic_fetch_add(&read_begin_count,1);
    }
    int64_t wall_began=wall_us();vTaskDelay(20);
    unsigned count=atomic_fetch_add(&reads,1)+1;
    if(cap_case()) {
        if(is_case("cap-connect-origin")||is_case("cap-read-boundary")||is_case("cap-read-error"))
            atomic_store(&fake_us,accepted_start_us+60000000);
        if(count==2&&(is_case("cap-encode")||is_case("cap-encode-error")||
                      is_case("cap-blocked-send")||is_case("cap-error-restart"))) {
            wait_flag(&encode_ready);
            atomic_store(&fake_us,accepted_start_us+60000000);
        }
        unsigned elapsed=wall_us()-wall_began;
        if(elapsed>read_wall_max_us)read_wall_max_us=elapsed;
        if(is_case("cap-read-error"))return ESP_FAIL;
    }
    if(count==2&&(is_case("failure-encode")||is_case("failure-encode-lock")||is_case("admitted-failure"))) {
        wait_flag(&encode_ready);return ESP_FAIL;
    }
    if(count==2&&is_case("snapshot"))wait_flag(&snapshot_done);
    if(!strcmp(scenario,"read-failure")&&count==4)return ESP_FAIL;
    atomic_fetch_add(&successful_reads,1);
    for(unsigned i=0;i<size;i++)((uint8_t *)pcm)[i]=(uint8_t)count;
    if(!strcmp(scenario,"stop-in-read")&&count==4)live_voice_stop();
    return ESP_OK;
}
esp_err_t audio_stream_write(audio_session_t *p,void *pcm,unsigned size) {(void)pcm;assert(p==&codec&&size==960);vTaskDelay(20);return ESP_OK;}
void audio_stream_close(audio_session_t *p) {
    assert(p==&codec&&atomic_load(&capture_exits)&&atomic_load(&playback_exits));
    assert(atomic_exchange(&opens,0)==1);atomic_fetch_add(&closes,1);
}
static bool tailnet_spike_audio_acquire(void) {bool old=false;return atomic_compare_exchange_strong(&admission,&old,true);}
static void tailnet_spike_audio_release(void) {assert(!worker_task&&!atomic_load(&opens));assert(atomic_exchange(&admission,false));}
static void network_snapshot(network_status_t *p) {*p=(network_status_t){true,true};}
static int bridge_mode,bridge_runs,key_reads;
static bool bridge_cleanup_held,bridge_reap_publish;
static unsigned bridge_reap_checks;
static wifi_ps_type_t wifi_mode=1;
static esp_err_t wifi_get_result=ESP_OK,wifi_set_result=ESP_OK;
static unsigned wifi_sets;
static esp_err_t network_copy_bridge(setup_bridge_t *p){p->enabled=bridge_mode==1;return bridge_mode==2?ESP_FAIL:ESP_OK;}
static int64_t bridge_deadline_seen,bridge_limit_seen;
static void bridge_voice_run(const setup_bridge_t *c,int64_t deadline,int64_t limit,bool (*stopped)(void)){assert(c->enabled&&deadline>0&&!stopped()&&wifi_mode==WIFI_PS_NONE);bridge_deadline_seen=deadline;bridge_limit_seen=limit;bridge_runs++;}
static bool bridge_voice_restart_required(void){return bridge_cleanup_held;}
static void bridge_voice_reap_finished(void){
    assert(!held); /* cleanup publishes state; status_lock must not span it */
    bridge_reap_checks++;
    if(bridge_reap_publish)set_state(LIVE_OFF,"Mic off"); /* bridge wrapper calls set_state */
}
static void bridge_voice_stop(void){}
static void bridge_voice_init(void){}
void audio_stream_prepare(void){}
static esp_err_t network_copy_api_key(char *p,size_t size) {key_reads++;host_strlcpy(p,"SYNTHETIC-NOT-A-CREDENTIAL",size);return ESP_OK;}
static esp_err_t esp_wifi_get_ps(wifi_ps_type_t *p) {*p=wifi_mode;return wifi_get_result;}
static esp_err_t esp_wifi_set_ps(wifi_ps_type_t p) {wifi_sets++;if(wifi_set_result==ESP_OK)wifi_mode=p;return wifi_set_result;}
static const char *esp_err_to_name(esp_err_t err) {(void)err;return "host";}
static bool esp_sntp_enabled(void) {return true;}
static void esp_sntp_setoperatingmode(int n) {(void)n;}
static void esp_sntp_setservername(int n,const char *s) {(void)n;(void)s;}
static void esp_sntp_init(void) {}
static void esp_crt_bundle_attach(void) {}
static void websocket_event(void *a,esp_event_base_t b,int32_t id,void *p) {(void)a;(void)b;(void)id;(void)p;}
static esp_websocket_client_handle_t esp_websocket_client_init(const esp_websocket_client_config_t *config) {
    assert(config->disable_auto_reconnect&&config->task_prio==5);
    struct host_ws *p=calloc(1,sizeof(*p));assert(p);return p;
}
static void esp_websocket_register_events(esp_websocket_client_handle_t p,int event,void (*callback)(void *,esp_event_base_t,int32_t,void *),void *c) {
    (void)callback;assert(event==WEBSOCKET_EVENT_ANY);p->connection=c;
}
static esp_err_t esp_websocket_client_start(esp_websocket_client_handle_t p) {atomic_store(&p->connection->connected,true);return ESP_OK;}
static int esp_websocket_client_send_text(esp_websocket_client_handle_t p,const char *text,size_t size,unsigned timeout) {
    assert(timeout==1000&&!held); /* never hold status_lock over transport */
    if(!strcmp(text,PET_LIVE_START)){
        if(is_case("cap-connect-origin"))atomic_store(&fake_us,accepted_start_us+20000000);
        atomic_store(&p->connection->ready,true);return (int)size;
    }
    if(!strcmp(text,PET_GREETING_EVENT))return (int)size;
    if(!strcmp(text,"{\"type\":\"session.close\"}")) {
        close_calls++;
        if(!strcmp(scenario,"stall"))return -1; /* must not replace first queue error */
        atomic_store(&p->connection->closed,true);return (int)size;
    }
    send_calls++;
    if(cap_case()) {
        assert(esp_timer_get_time()<accepted_start_us+60000000);
        if(is_case("cap-blocked-send")) {
            signal_flag(&encode_ready);wait_flag(&cap_capture_done);
            /* Capture exits independently while the actual sender is blocked.
             * Cleanup must NOT yet free buffers/release ownership/claim OFF. */
            assert(capture_exits==1&&allocations==7&&admission&&opens&&status.active);
            assert(atomic_load(&stop_requested));
            puts("PASS cap capture exited before blocked-send release; runtime still owned");
        }
    }
    if(is_case("partial-race"))signal_flag(&sender_done);
    if(is_case("admitted-stop"))live_voice_stop(); /* already-admitted call may complete */
    if(is_case("admitted-failure")){signal_flag(&encode_ready);wait_flag(&action_done);}
    const char *prefix="{\"type\":\"session.input_audio.append\",\"audio\":\"";
    size_t offset=strlen(prefix);assert(!strncmp(text,prefix,offset)&&!strcmp(text+size-2,"\"}"));
    uint8_t decoded[4800];size_t count=0;
    assert(!mbedtls_base64_decode(decoded,sizeof(decoded),&count,(const unsigned char *)text+offset,size-offset-2));
    assert(count&&count<=4800);
    if(!is_case("partial")&&!is_case("partial-race"))assert(count%960==0);
    unsigned total=atomic_load(&wire_bytes);
    for(size_t i=0;i<count;i++)assert(decoded[i]==(uint8_t)((total+i)/960+1));
    if(!strcmp(scenario,"short"))return (int)size-1;
    if(!strcmp(scenario,"hard-failure"))return -1;
    if(is_case("stall") && send_calls==1) {
        /* Hold the transport through a real short enqueue, even when host
         * scheduling slows the fake codec. Keep the original latency floor. */
        vTaskDelay(2300);
        wait_flag(&stall_overflow);
    }
    else if(!strcmp(scenario,"burst")&&send_calls==1)vTaskDelay(1750);
    else if(!strcmp(scenario,"slow"))vTaskDelay(150);
    else if(!strcmp(scenario,"jitter")&&send_calls%5==1)vTaskDelay(280);
    else vTaskDelay(2);
    successful_sends++;atomic_fetch_add(&wire_bytes,count);return (int)size;
}
static void esp_websocket_client_stop(esp_websocket_client_handle_t p) {atomic_store(&p->connection->connected,false);}
static void esp_websocket_client_destroy(esp_websocket_client_handle_t p) {free(p);}
static int host_encode(unsigned char *out,size_t cap,size_t *written,const unsigned char *in,size_t size) {
    if(is_case("encode-failure"))return MBEDTLS_ERR_BASE64_BUFFER_TOO_SMALL;
    int result=mbedtls_base64_encode(out,cap,written,in,size);
    if(is_case("cap-encode")||is_case("cap-encode-error")||is_case("cap-error-restart")) {
        if(is_case("cap-error-restart"))pipeline_fail(connection,1,"Microphone read failed.");
        atomic_store(&fake_us,accepted_start_us+60000000);signal_flag(&encode_ready);
        if(is_case("cap-encode-error"))return MBEDTLS_ERR_BASE64_BUFFER_TOO_SMALL;
    }
    if(is_case("failure-encode")||is_case("stop-encode")) {
        signal_flag(&encode_ready);wait_flag(&action_done);
    }
    if(is_case("failure-encode-lock"))encoded_pending=true;
    return result;
}
static void *action_thread(void *unused) {
    (void)unused;actor=true;
    if(is_case("snapshot")) {
        wait_flag(&snapshot_ready);
        pipeline_log(connection);signal_flag(&snapshot_done);
    } else {
        wait_flag(&encode_ready);live_voice_stop();signal_flag(&action_done);
    }
    return NULL;
}

static void test_capture_backpressure(const char *name) {
    scenario=name;
    case_ms=is_case("slow")?18000:is_case("stall")?8000:is_case("burst")?3000:1400;
    stall_overflow=false;
    reads=0;closes=0;capture_exits=0;playback_exits=0;wire_bytes=0;queue_peak=0;wipes=0;successful_reads=0;accepted_bytes=0;
    send_calls=partial_injections=successful_sends=0;observed_metrics=false;
    partial_ready=sender_done=encode_ready=action_done=snapshot_ready=snapshot_attempted=snapshot_done=false;
    close_calls=snapshot_reads=0;
    cap_capture_done=false;read_begin_count=0;read_wall_max_us=0;
    if(cap_case()) {
        accepted_start_us=previous_cap_start?previous_cap_start+70000000:100000000;
        previous_cap_start=accepted_start_us;
        atomic_store(&fake_us,accepted_start_us);
    }
    pthread_t action;
    bool has_action=is_case("stop-encode")||is_case("stop-receive")||is_case("snapshot");
    if(has_action)assert(!pthread_create(&action,NULL,action_thread,NULL));
    live_voice_start();TaskHandle_t task=worker_task;assert(task&&atomic_load(&admission));
#ifdef VOICE_SESSION_LIMIT_MS
    xSemaphoreTake(status_lock,portMAX_DELAY);
    int64_t start_deadline=session_deadline_us;
    assert(start_deadline>esp_timer_get_time()||cap_case());
    if(cap_case())assert(VOICE_SESSION_LIMIT_MS==60000&&start_deadline==accepted_start_us+60000000);
    xSemaphoreGive(status_lock);
#endif
    live_voice_start();assert(worker_task==task); /* no duplicate microphone owner */
    int64_t deadline=wall_us()+(int64_t)case_ms*1000;
    while(atomic_load(&task->state)!=eSuspended&&wall_us()<deadline)vTaskDelay(5);
    if(cap_case())assert(atomic_load(&task->state)==eSuspended); /* no manual Stop rescues cap */
    live_voice_stop();
#ifdef VOICE_SESSION_LIMIT_MS
    xSemaphoreTake(status_lock,portMAX_DELAY);assert(session_deadline_us==start_deadline);xSemaphoreGive(status_lock);
#endif
    deadline=wall_us()+4000000;
    while(atomic_load(&task->state)!=eSuspended&&wall_us()<deadline)vTaskDelay(5);
    assert(atomic_load(&task->state)==eSuspended);
    if(has_action)assert(!pthread_join(action,NULL));
    assert(capture_exits==1&&playback_exits==1&&closes==1&&!opens&&!allocations&&wipes==6);
    unsigned stopped_reads=atomic_load(&reads);vTaskDelay(30);assert(reads==stopped_reads);
    bool serviceable=!strcmp(name,"normal")||!strcmp(name,"jitter")||!strcmp(name,"burst")||!strcmp(name,"stop-in-read")||
        is_case("stop-encode")||is_case("stop-receive")||is_case("admitted-stop")||is_case("snapshot");
    if(cap_case())serviceable=!is_case("cap-error-restart");
    live_status_t s;live_voice_snapshot(&s);
    printf("schedule=%s read_bytes=%u wire_pcm_bytes=%u queue_peak=%u state=%d sends=%u\n",
           name,reads*960,wire_bytes,queue_peak,s.state,successful_sends);
    if(serviceable)assert(s.state==LIVE_OFF);
    else assert(s.state==LIVE_ERROR);
    /* A partial RTOS enqueue fails capture before the receive timeout wakes.
     * That wakeup must not append an odd-length straggler after first failure. */
    if(is_case("partial")||is_case("partial-race")||is_case("failure-encode")||
       is_case("failure-encode-lock")||is_case("stop-encode")||is_case("stop-receive")||is_case("encode-failure"))
        assert(wire_bytes==0);
    assert(close_calls==1);
    if(is_case("admitted-stop")||is_case("admitted-failure"))assert(wire_bytes>0);
    if(is_case("snapshot"))assert(snapshot_reads==1);
    if(cap_case()) {
        assert(!metrics_overflows&&closes==1);
        if(is_case("cap-blocked-send"))assert(send_calls==1&&metrics_sent>0);
        else assert(!wire_bytes&&!send_calls);
        if(is_case("cap-connect-origin")||is_case("cap-read-boundary")||is_case("cap-queue-boundary"))
            assert(read_begin_count==1&&accepted_bytes==0&&metrics_captured==960&&metrics_dropped==960);
        if(is_case("cap-read-error"))assert(read_begin_count==1&&metrics_captured==0);
        if(is_case("cap-error-restart"))assert(metrics_stage==1&&!strcmp(s.message,"Microphone read failed."));
        printf("PASS cap %s start=%lld expiry=%lld read_begins=%u max_inflight_read_wall_us=%u stage=%u\n",
            name,(long long)accepted_start_us,(long long)accepted_start_us+60000000,
            atomic_load(&read_begin_count),read_wall_max_us,metrics_stage);
    }
    if(!strcmp(name,"normal")||!strcmp(name,"jitter"))assert(successful_sends>0);
    /* RED on copied baseline: losses have no complete numerical disposition. */
    if(!getenv("VOICE_CHARACTERIZE"))assert(observed_metrics);
    if(observed_metrics) {
        assert(metrics_sent==wire_bytes);
        assert((cap_case()||metrics_read_us>=19000)&&metrics_peak<=96000);
        if(serviceable)assert(!metrics_overflows&&!metrics_uncertain&&!metrics_stage);
        if(!strcmp(name,"stall")||!strcmp(name,"slow")||!strcmp(name,"partial")||is_case("partial-race"))
            assert(metrics_overflows==1&&metrics_stage==2);
        if(is_case("partial-race"))assert(metrics_captured==960&&metrics_dropped==960);
        if(is_case("failure-encode")||is_case("failure-encode-lock")||is_case("admitted-failure"))
            assert(metrics_stage==1&&!strcmp(s.message,"Microphone read failed."));
        if(is_case("encode-failure"))assert(metrics_stage==3);
        if(!strcmp(name,"stall")) {
            assert(metrics_gap>=1900000&&metrics_send_us>=2300000);
            assert(!strcmp(s.message,"Mic stream cannot keep up. Tap Start voice to retry."));
        }
        if(!strcmp(name,"burst"))assert(metrics_gap>=1700000&&metrics_peak>=70000);
        if(!strcmp(name,"short")||!strcmp(name,"hard-failure"))assert(metrics_uncertain>0&&metrics_stage==4);
        if(!strcmp(name,"read-failure"))assert(metrics_stage==1);
        printf("accounted captured=%llu sent=%llu dropped=%u uncertain=%u overflow=%u\n",
               (unsigned long long)metrics_captured,(unsigned long long)metrics_sent,
               metrics_dropped,metrics_uncertain,metrics_overflows);
    }
    assert(atomic_load(&admission));
    bridge_cleanup_held=true;unsigned before=bridge_reap_checks;
    live_voice_reap_finished();
    assert(worker_task&&atomic_load(&admission)&&bridge_reap_checks==before+1);
    bridge_cleanup_held=false;bridge_reap_publish=true;
    /* The recovered call can publish its honest final status over the earlier
     * held-error label even when this is the first successful reap. */
    xSemaphoreTake(status_lock,portMAX_DELAY);status.active=true;status.state=LIVE_ERROR;
    strcpy(status.message,"Voice cleanup incomplete. Connection held.");xSemaphoreGive(status_lock);
    live_voice_reap_finished();bridge_reap_publish=false;
    live_voice_snapshot(&s);assert(s.state==LIVE_OFF&&!strcmp(s.message,"Mic off"));
    assert(!worker_task&&!atomic_load(&admission));
#ifdef VOICE_SESSION_LIMIT_MS
    xSemaphoreTake(status_lock,portMAX_DELAY);assert(session_deadline_us==0);xSemaphoreGive(status_lock);
#endif
    for(unsigned i=0;i<2;i++){assert(audio_tasks_host[i]->joined);free(audio_tasks_host[i]);audio_tasks_host[i]=NULL;}
    printf("PASS pipeline %s cleanup/reap\n",name);
}
static void test_creation_failure(void) {
    scenario="normal";fail_worker_creation=true;
    live_voice_start();fail_worker_creation=false;
    assert(!worker_task&&!admission&&!status.active&&!opens&&!allocations);
#ifdef VOICE_SESSION_LIMIT_MS
    xSemaphoreTake(status_lock,portMAX_DELAY);assert(!session_deadline_us);xSemaphoreGive(status_lock);
#endif
    puts("PASS cap worker-create failure clears deadline without runtime");
}
static void test_retry_after_failure(void) {
    test_capture_backpressure("hard-failure");
    assert(!worker_task&&!admission&&!opens&&!allocations);
    test_capture_backpressure("normal");
    puts("PASS pipeline test_retry_after_failure previous worker deleted before fresh session");
}
int main(int argc,char **argv) {
    assert(argc==2);setvbuf(stdout,NULL,_IONBF,0);
    live_voice_init();assert(!worker_task&&!opens&&!reads&&!allocations);
    /* A bridge conversation gets the longer deadline, from the SAME accepted
     * Start, and the same allowance for any call re-established later. The
     * bridge owns those per-call deadlines, so the worker's own cutoff is
     * cleared (mic off still stops it). The standalone path keeps its exact
     * 60000 ms (cap cases below). */
    int64_t accepted=esp_timer_get_time();
    bridge_mode=1;session_deadline_us=accepted+60000000;run_session();
    assert(bridge_runs==1&&key_reads==0&&opens==0&&wifi_mode==1&&wifi_sets==2);
    assert(BRIDGE_SESSION_LIMIT_MS==660000&&VOICE_SESSION_LIMIT_MS==60000);
    assert(bridge_deadline_seen==accepted+660000000&&bridge_limit_seen==660000000&&session_deadline_us==0);
    assert(!session_stop());
    /* Mic turned off before the call began: said plainly, no bridge run, no key. */
    live_status_t early;
    atomic_store(&stop_requested,true);session_deadline_us=esp_timer_get_time()+60000000;run_session();
    live_voice_snapshot(&early);
    assert(bridge_runs==1&&early.state==LIVE_OFF&&!strcmp(early.message,"Mic off")&&key_reads==0&&opens==0);
    atomic_store(&stop_requested,false);session_deadline_us=accepted+60000000;wifi_sets=2;
    wifi_mode=WIFI_PS_NONE;wifi_sets=0;run_session();
    assert(bridge_runs==2&&wifi_mode==WIFI_PS_NONE&&wifi_sets==0);
    wifi_mode=1;wifi_get_result=ESP_FAIL;run_session();
    assert(bridge_runs==2&&wifi_mode==1&&wifi_sets==0&&key_reads==0);
    wifi_get_result=ESP_OK;wifi_set_result=ESP_FAIL;run_session();
    assert(bridge_runs==2&&wifi_mode==1&&wifi_sets==1&&key_reads==0);
    wifi_set_result=ESP_OK;wifi_sets=0;
    bridge_mode=2;run_session();assert(bridge_runs==2&&key_reads==0&&opens==0&&wifi_sets==0);
    bridge_mode=0;session_deadline_us=0;
    puts("PASS production run_session routes enabled/corrupt bridge without provider key, direct connection or mic open");
    puts("PASS bridge Wi-Fi low-latency mode, exact restoration, already-disabled mode and get/set failures");
    if(!strcmp(argv[1],"bridge-wifi")){
        assert(!pthread_mutex_destroy(&status_lock->mutex));free(status_lock);return 0;
    }
    puts("PASS pipeline boot/idle no capture");
    const char *cases[]={"normal","jitter","burst","stall","slow","partial","short","hard-failure","read-failure","stop-in-read",
        "partial-race","failure-encode","failure-encode-lock","stop-encode","stop-receive","encode-failure","admitted-stop","admitted-failure","snapshot",
        "cap-connect-origin","cap-read-boundary","cap-read-error","cap-queue-boundary","cap-encode","cap-encode-error","cap-blocked-send","cap-error-restart"};
    unsigned count=0;
    for(unsigned i=0;i<sizeof(cases)/sizeof(*cases);i++)if(!strcmp(argv[1],"all")||!strcmp(argv[1],cases[i])) {
        test_capture_backpressure(cases[i]);count++;
    }
    assert(count);
    if(!strcmp(argv[1],"all")||!strcmp(argv[1],"cap-error-restart")) {
        /* The preceding error session is fully reaped before explicit Start.
         * The new accepted Start receives its own exact 60000-ms interval. */
        assert(!worker_task&&!admission&&!opens&&!allocations);
        test_capture_backpressure("cap-read-boundary");count++;
        puts("PASS cap explicit fresh Start after error gets fresh deadline");
    }
    /* Fresh real run_session after preceding failure, not a success-only stub. */
    test_retry_after_failure();
    test_creation_failure();
    assert(!pthread_mutex_destroy(&status_lock->mutex));free(status_lock);
    printf("PASS pipeline total=%u schedules + boot + retry; production bodies, host boundaries only\n",count+2);
}