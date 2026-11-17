/* Timing probes at link seams (--wrap): no production source is changed and
 * no behaviour is altered, only durations are recorded per second of the call.
 * They separate where the conversation owner's time goes while it sends
 * microphone audio: waiting for a transport lock, waiting for the socket to
 * accept bytes, or neither (then the owner itself was not scheduled). */
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_timer.h"
#include "lwip/sockets.h"
#include "rig.h"

#define SECONDS 240
typedef struct {atomic_llong send,send_max,lock,write_wait,ws_lock,ws_read_wait;atomic_int send_failed,sends;} second_t;
static second_t seconds[SECONDS];
static second_t *bucket(void){
    int64_t s=rig_listening_seen()?rig_since_listening_ms()/1000+1:0; /* 0: before listening */
    return &seconds[s<SECONDS?s:SECONDS-1];
}
static int who(void){
    const char *name=pcTaskGetName(NULL);
    return !name?0:!strcmp(name,"pet_live")?1:!strcmp(name,"websocket_task")?2:0;
}
int __real_esp_websocket_client_send_text(void *client,const char *data,int len,TickType_t timeout);
int __wrap_esp_websocket_client_send_text(void *client,const char *data,int len,TickType_t timeout){
    int64_t start=esp_timer_get_time();
    int result=__real_esp_websocket_client_send_text(client,data,len,timeout);
    long long took=esp_timer_get_time()-start;second_t *b=bucket();
    atomic_fetch_add(&b->send,took);atomic_fetch_add(&b->sends,1);
    if(took>atomic_load(&b->send_max))atomic_store(&b->send_max,took);
    if(result!=len)atomic_fetch_add(&b->send_failed,1);
    return result;
}
BaseType_t __real_xQueueTakeMutexRecursive(QueueHandle_t mutex,TickType_t ticks);
BaseType_t __wrap_xQueueTakeMutexRecursive(QueueHandle_t mutex,TickType_t ticks){
    int task=who();
    if(!task)return __real_xQueueTakeMutexRecursive(mutex,ticks);
    int64_t start=esp_timer_get_time();
    BaseType_t result=__real_xQueueTakeMutexRecursive(mutex,ticks);
    long long took=esp_timer_get_time()-start;
    atomic_fetch_add(task==1?&bucket()->lock:&bucket()->ws_lock,took);
    return result;
}
int __real_lwip_select(int n,fd_set *r,fd_set *w,fd_set *e,struct timeval *t);
int __wrap_lwip_select(int n,fd_set *r,fd_set *w,fd_set *e,struct timeval *t){
    int task=who();
    if(!task)return __real_lwip_select(n,r,w,e,t);
    int64_t start=esp_timer_get_time();
    int result=__real_lwip_select(n,r,w,e,t);
    long long took=esp_timer_get_time()-start;
    if(task==1&&w)atomic_fetch_add(&bucket()->write_wait,took);
    if(task==2&&r)atomic_fetch_add(&bucket()->ws_read_wait,took);
    return result;
}
void rig_probe_report(void){
    for(int i=0;i<SECONDS;i++){
        second_t *b=&seconds[i];
        if(!atomic_load(&b->sends)&&!atomic_load(&b->ws_read_wait))continue;
        printf("RIG probe second=%d sends=%d failed=%d owner_send_ms=%lld owner_send_max_ms=%lld owner_lock_wait_ms=%lld "
               "owner_socket_wait_ms=%lld reader_lock_wait_ms=%lld reader_socket_wait_ms=%lld\n",
            i-1,atomic_load(&b->sends),atomic_load(&b->send_failed),atomic_load(&b->send)/1000,atomic_load(&b->send_max)/1000,
            atomic_load(&b->lock)/1000,atomic_load(&b->write_wait)/1000,atomic_load(&b->ws_lock)/1000,atomic_load(&b->ws_read_wait)/1000);
    }
}
