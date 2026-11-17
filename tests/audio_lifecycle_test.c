/* Execute actual production init/request/worker/reap/admission bodies.
 * Audio hardware and full voice sessions are seams; pthreads exercise the
 * worker-before-handle-publication and simultaneous reaper interleavings. */
#define _GNU_SOURCE
#include <assert.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "audio_test.h"
#include "live_voice.h"
#include "tailnet_spike.h"

typedef struct { pthread_mutex_t mutex; } *SemaphoreHandle_t;
typedef void *QueueHandle_t;
typedef enum { eRunning,eReady,eBlocked,eSuspended,eDeleted } eTaskState;
typedef struct host_task {
    pthread_t thread;
    void (*fn)(void *);
    void *arg;
    atomic_int state;
    bool audio,deleted;
} *TaskHandle_t;
typedef int nvs_handle_t;
struct audio_session { int unused; };
#define pdTRUE 1
#define pdPASS 1
#define pdMS_TO_TICKS(n) (n)
#define portMAX_DELAY 0
#define NVS_READWRITE 0
#define ESP_LOGI(tag, ...) do { (void)(tag); } while (0)
static _Thread_local unsigned held;
static _Thread_local TaskHandle_t self;
static _Thread_local bool attempted_own_mutex;
static _Thread_local bool deleting_audio;
static atomic_uint creates,deletes,opens,closes,tones,mics,sessions;
static atomic_bool work_allowed,work_done,allow_suspend,task_attempted;
static bool fail_create;
static atomic_int owner;
static unsigned release_after_delete;
static bool cleanup_error;

static void check_release(void);
static bool checked_cas(atomic_int *object,int *expected,int desired) {
    if(object==&owner && *expected==2 && desired==0)check_release();
    return atomic_compare_exchange_strong_explicit(object,expected,desired,memory_order_seq_cst,memory_order_seq_cst);
}
#undef atomic_compare_exchange_strong
#define atomic_compare_exchange_strong(object,expected,desired) checked_cas(object,expected,desired)
#define strlcpy test_strlcpy
static size_t test_strlcpy(char *dst,const char *src,size_t cap) {
    size_t length=strlen(src);
    if(cap){size_t n=length<cap-1?length:cap-1;memcpy(dst,src,n);dst[n]=0;}
    return length;
}
static SemaphoreHandle_t xSemaphoreCreateMutex(void) {
    SemaphoreHandle_t lock=calloc(1,sizeof(*lock));assert(lock);
    assert(!pthread_mutex_init(&lock->mutex,NULL));return lock;
}
static void xSemaphoreTake(SemaphoreHandle_t lock,int wait) {
    if(self&&!attempted_own_mutex) {
        attempted_own_mutex=true;
        atomic_store(&task_attempted,true); /* Real worker attempts lock before publish. */
    }
    (void)wait;assert(lock);assert(held==0);assert(!pthread_mutex_lock(&lock->mutex));held++;
}
static void xSemaphoreGive(SemaphoreHandle_t lock) {
    assert(held==1);held--;assert(!pthread_mutex_unlock(&lock->mutex));
}
static QueueHandle_t xQueueCreate(unsigned count,size_t bytes) {
    (void)count;(void)bytes;return (void *)1;
}
static int xQueueReceive(QueueHandle_t queue,void *job,int wait) {
    (void)queue;(void)job;(void)wait;abort(); /* Old boot workers are not run. */
}
static int xQueueSend(QueueHandle_t queue,const void *job,int wait) {
    (void)queue;(void)job;(void)wait;return pdTRUE;
}
static void *host_entry(void *arg) {
    self=arg;self->fn(self->arg);
    abort(); /* Production worker must suspend, never return. */
}
static int xTaskCreate(void (*fn)(void *),const char *name,unsigned stack,void *arg,
                       unsigned priority,TaskHandle_t *published) {
    assert(priority==4);
    bool audio=!strcmp(name,"audio_test");
    assert(stack==(audio?6144:12288));assert(audio||!strcmp(name,"pet_live"));
    if(fail_create){if(published)*published=(TaskHandle_t)(uintptr_t)1;return 0;}
    atomic_fetch_add(&creates,1);
    if(!published)return pdPASS; /* Old init boot creation; RED asserts count. */
    TaskHandle_t task=calloc(1,sizeof(*task));assert(task);
    task->fn=fn;task->arg=arg;task->audio=audio;atomic_store(&task->state,eRunning);
    atomic_store(&task_attempted,false);
    assert(held==1); /* Publication and request state must be protected. */
    assert(!pthread_create(&task->thread,NULL,host_entry,task));
    while(!atomic_load(&task_attempted))sched_yield(); /* Start BEFORE handle publication. */
    *published=task;
    return pdPASS;
}
static eTaskState eTaskGetState(TaskHandle_t task) {
    assert(held==1&&task&&!task->deleted);return atomic_load(&task->state);
}
static void vTaskSuspend(TaskHandle_t task) {
    assert(!task&&self&&held==0);assert(atomic_load(&work_done));
    while(!atomic_load(&allow_suspend))sched_yield();
    atomic_store(&self->state,eSuspended);pthread_exit(NULL);
}
static void vTaskDelete(TaskHandle_t task);
static void vTaskDelay(unsigned ticks) {(void)ticks;}
static esp_err_t nvs_open(const char *name,int mode,nvs_handle_t *out) {
    assert(!strcmp(name,"pet_audio")&&mode==NVS_READWRITE);*out=1;return ESP_OK;
}
static esp_err_t nvs_get_u8(nvs_handle_t store,const char *key,uint8_t *out) {
    assert(store==1&&!strcmp(key,"volume"));*out=61;return ESP_OK;
}
static const char *esp_err_to_name(esp_err_t err) {return err==ESP_OK?"ESP_OK":"ESP_FAIL";}
static void assert_worker_published(void);
static esp_err_t open_audio(audio_session_t *audio,bool mic) {
    (void)audio;(void)mic;assert(self&&held==0&&atomic_load(&owner)==2);
    assert_worker_published();
    atomic_fetch_add(&opens,1);
    while(!atomic_load(&work_allowed))sched_yield();
    return ESP_OK;
}
static esp_err_t tone(audio_session_t *audio) {(void)audio;atomic_fetch_add(&tones,1);return ESP_OK;}
static esp_err_t microphone(audio_session_t *audio) {(void)audio;atomic_fetch_add(&mics,1);return ESP_OK;}
static void close_audio(audio_session_t *audio) {
    (void)audio;atomic_fetch_add(&closes,1);atomic_store(&work_done,true);
}
static void run_session(void);
#define status a_status
#define lock a_lock
#define jobs a_jobs
#define worker audio_worker
#define worker_task a_task
#define worker_finished a_finished
#include "audio_test-lifecycle.inc"
#undef status
#undef lock
#undef jobs
#undef worker
#undef worker_task
#undef worker_finished

#define status v_status
#define jobs v_jobs
#define worker voice_worker
#define worker_task v_task
#define worker_finished v_finished
#include "live_voice-lifecycle.inc"
#undef status
#undef jobs
#undef worker
#undef worker_task
#undef worker_finished
static void run_session(void) {
    assert_worker_published();
    assert(self&&held==0&&atomic_load(&owner)==2);atomic_fetch_add(&sessions,1);
    while(!atomic_load(&work_allowed))sched_yield();
    set_state(cleanup_error?LIVE_ERROR:LIVE_OFF,cleanup_error?"session error retained":"session cleaned");
    atomic_store(&work_done,true);
}
static void assert_worker_published(void) {
    assert(attempted_own_mutex);
    assert(self&&(self->audio?a_task:v_task)==self);
}
static void check_release(void) {
    /* Reaper must clear lifecycle state only AFTER synchronous deletion. */
    if(deleting_audio) {
        assert(!a_task&&!a_finished&&!a_status.busy);release_after_delete++;
    } else if(release_after_delete&0x80000000u) {
        assert(!v_task&&!v_finished&&!v_status.active);
        release_after_delete=(release_after_delete&0x7fffffffu)+1;
    }
}
static void vTaskDelete(TaskHandle_t task) {
    assert(task&&held==1&&!task->deleted&&atomic_load(&task->state)==eSuspended);
    assert(atomic_load(&owner)==2&&atomic_load(&work_done));
    if(task->audio){assert(a_task==task&&a_finished&&a_status.busy);deleting_audio=true;}
    else {assert(v_task==task&&v_finished&&v_status.active);release_after_delete|=0x80000000u;}
    assert(!pthread_join(task->thread,NULL));task->deleted=true;
    atomic_fetch_add(&deletes,1);free(task);
}
static SemaphoreHandle_t t_lock;
static QueueHandle_t t_jobs;
static tailnet_spike_status_t t_status;
static uint64_t began,overall;
static atomic_bool cancelled;
static uint64_t now(void) {return 1000;}
#define lock t_lock
#define jobs t_jobs
#define status t_status
#include "audio-admission.inc"
#undef lock
#undef jobs
#undef status

static unsigned cases;
static void passed(const char *name) {cases++;printf("PASS lifecycle %s\n",name);}
static void prepare_job(void) {
    assert(atomic_load(&owner)==0);
    atomic_store(&work_allowed,false);atomic_store(&work_done,false);
    atomic_store(&allow_suspend,false);cleanup_error=false;deleting_audio=false;
    assert(!(release_after_delete&0x80000000u));
}
static void wait_state(TaskHandle_t task,eTaskState state) {
    for(unsigned i=0;i<1000000;i++){if(atomic_load(&task->state)==(int)state)return;sched_yield();}
    assert(!"worker state timeout");
}
static bool finished(bool audio) {
    bool value;
    xSemaphoreTake(audio?a_lock:status_lock,portMAX_DELAY);
    value=audio?a_finished:v_finished;
    xSemaphoreGive(audio?a_lock:status_lock);
    return value;
}
static void complete_to_running(bool audio) {
    atomic_store(&work_allowed,true);
    for(unsigned i=0;i<1000000;i++){if(finished(audio))return;sched_yield();}
    assert(!"finished timeout");
}
static void *reap_both(void *arg) {
    (void)arg;audio_test_reap_finished();live_voice_reap_finished();return NULL;
}
static void concurrent_reap(void) {
    pthread_t threads[2];
    for(unsigned i=0;i<2;i++)assert(!pthread_create(&threads[i],NULL,reap_both,NULL));
    for(unsigned i=0;i<2;i++)assert(!pthread_join(threads[i],NULL));
}
static pthread_barrier_t request_barrier;
static void *concurrent_request(void *arg) {
    pthread_barrier_wait(&request_barrier);
    if(arg)audio_test_request(true);else live_voice_start();
    return NULL;
}
static void release_tailnet(void) {
    assert(atomic_load(&owner)==1);atomic_store(&owner,0);
}
int main(void) {
    (void)eTaskGetState;(void)vTaskDelete;(void)vTaskSuspend;
    (void)xQueueCreate;(void)xQueueReceive;(void)audio_worker;(void)voice_worker;
    audio_tests_init();live_voice_init();t_lock=xSemaphoreCreateMutex();t_jobs=(void *)1;
    /* Expected RED on old code: init allocates both idle workers. */
    assert(atomic_load(&creates)==0);
    assert(!a_task&&!v_task&&!a_status.busy&&!v_status.active&&a_status.volume==61);
    passed("boot-zero-workers-volume-retained");
    assert(!atomic_load(&opens)&&!atomic_load(&sessions));
    atomic_store(&owner,1);audio_test_request(false);live_voice_start();
    assert(!atomic_load(&creates));release_tailnet();passed("tailnet-ownership-rejects");
    for(unsigned module=0;module<2;module++) {
        prepare_job();fail_create=true;
        if(module==0)audio_test_request(false);else live_voice_start();
        assert(!a_task&&!v_task&&!a_finished&&!v_finished&&!a_status.busy&&!v_status.active);
        assert(atomic_load(&owner)==0&&!atomic_load(&opens)&&!atomic_load(&sessions));
        assert(module==0?strstr(a_status.message,"worker")!=NULL:v_status.state==LIVE_ERROR);
        fail_create=false;passed(module==0?"audio-create-failure":"voice-create-failure");
    }
    for(unsigned job=0;job<3;job++) {
        bool audio=job<2;prepare_job();
        unsigned c=atomic_load(&creates),d=atomic_load(&deletes),r=release_after_delete;
        if(audio)audio_test_request(job==1);else live_voice_start();
        TaskHandle_t task=audio?a_task:v_task;
        assert(task&&atomic_load(&creates)==c+1&&atomic_load(&owner)==2);
        audio_test_request(false);live_voice_start();
        assert(atomic_load(&creates)==c+1);passed("duplicate-cross-module-request-rejected");
        complete_to_running(audio);
        assert(audio?a_status.busy:v_status.active);
        reap_both(NULL);
        assert(atomic_load(&deletes)==d&&atomic_load(&owner)==2);
        assert(tailnet_spike_start()==ESP_ERR_INVALID_STATE);
        atomic_store(&task->state,eReady);reap_both(NULL);
        assert(atomic_load(&deletes)==d&&atomic_load(&owner)==2);
        atomic_store(&task->state,eBlocked);reap_both(NULL);
        assert(atomic_load(&deletes)==d&&atomic_load(&owner)==2);
        atomic_store(&task->state,eRunning);
        passed("finished-running-ready-retains-admission");
        atomic_store(&allow_suspend,true);wait_state(task,eSuspended);
        if(job==1) {
            assert(tailnet_spike_start()==ESP_OK);release_tailnet();
        } else concurrent_reap();
        assert(atomic_load(&deletes)==d+1&&atomic_load(&owner)==0);
        assert(release_after_delete==r+1&&!a_task&&!v_task);
        assert(!a_status.busy&&!v_status.active);deleting_audio=false;
        reap_both(NULL);assert(atomic_load(&deletes)==d+1);
        passed("suspended-delete-before-clear-release-no-double-delete");
        assert(tailnet_spike_start()==ESP_OK);release_tailnet();
        passed(audio?"audio-mic-then-tailnet":"voice-then-tailnet");
    }
    assert(atomic_load(&tones)==1&&atomic_load(&mics)==1&&atomic_load(&opens)==2&&atomic_load(&closes)==2&&atomic_load(&sessions)==1);
    prepare_job();cleanup_error=true;live_voice_start();complete_to_running(false);
    TaskHandle_t task=v_task;atomic_store(&allow_suspend,true);wait_state(task,eSuspended);
    /* Admission helper, not UI polling, must reap a finished voice worker. */
    assert(tailnet_spike_audio_acquire());assert(!v_task&&!v_status.active&&v_status.state==LIVE_ERROR);
    assert(!strcmp(v_status.message,"session error retained"));tailnet_spike_audio_release();
    passed("voice-error-preserved-next-audio-reaps");
    for(unsigned repeat=0;repeat<3;repeat++) {
        prepare_job();unsigned count=atomic_load(&sessions),d=atomic_load(&deletes);
        live_voice_start();assert(v_task&&!a_task);
        task=v_task;complete_to_running(false);
        assert(atomic_load(&sessions)==count+1);
        atomic_store(&allow_suspend,true);wait_state(task,eSuspended);
        assert(tailnet_spike_start()==ESP_OK);
        assert(!v_task&&!v_status.active&&atomic_load(&deletes)==d+1);
        release_tailnet();
        passed("forced-repeated-voice-finish-reap-tailnet");
    }
    for(unsigned repeat=0;repeat<4;repeat++) {
        prepare_job();unsigned c=atomic_load(&creates),d=atomic_load(&deletes);
        pthread_t requesters[2];assert(!pthread_barrier_init(&request_barrier,NULL,2));
        assert(!pthread_create(&requesters[0],NULL,concurrent_request,(void *)1));
        assert(!pthread_create(&requesters[1],NULL,concurrent_request,NULL));
        for(unsigned i=0;i<2;i++)assert(!pthread_join(requesters[i],NULL));
        pthread_barrier_destroy(&request_barrier);
        assert(atomic_load(&creates)==c+1&&atomic_load(&owner)==2);
        bool audio=a_task!=NULL;task=audio?a_task:v_task;
        assert(task);complete_to_running(audio);
        atomic_store(&allow_suspend,true);wait_state(task,eSuspended);
        assert(tailnet_spike_start()==ESP_OK);
        assert(atomic_load(&deletes)==d+1&&!a_task&&!v_task);
        release_tailnet();deleting_audio=false;
        passed("concurrent-requests-one-worker-repeated-tailnet");
    }
    assert(atomic_load(&creates)==atomic_load(&deletes));
    pthread_mutex_destroy(&a_lock->mutex);free(a_lock);
    pthread_mutex_destroy(&status_lock->mutex);free(status_lock);
    pthread_mutex_destroy(&t_lock->mutex);free(t_lock);
    printf("PASS lifecycle total=%u; production lifecycle bodies, RTOS/hardware/session seams\n",cases);
    return 0;
}