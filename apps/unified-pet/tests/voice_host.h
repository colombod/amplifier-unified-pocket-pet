/* Hardware/RTOS/transport boundary declarations. No sender or capture policy. */
#pragma once
#define _GNU_SOURCE
#include <assert.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include "audio_test.h"
#include "live_voice.h"
#include "live_protocol.h"
#include "assistant_config.h"
#include "mbedtls/base64.h"
#include "mbedtls/platform_util.h"
typedef unsigned EventBits_t;
typedef struct { pthread_mutex_t mutex; } *SemaphoreHandle_t;
typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t changed;
    uint8_t *memory;
    size_t capacity, used, head, tail, trigger;
} StaticStreamBuffer_t, *StreamBufferHandle_t;
typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t changed;
    unsigned bits;
} *EventGroupHandle_t;
typedef enum { eRunning, eSuspended } eTaskState;
typedef struct host_task {
    pthread_t thread;
    void (*fn)(void *);
    void *arg;
    atomic_int state;
    bool joined;
} *TaskHandle_t;
struct audio_session { unsigned unused; };
typedef struct { bool connected, key_saved; } network_status_t;
typedef struct { bool enabled; } setup_bridge_t; /* route boundary, not config validator */
static esp_err_t network_copy_bridge(setup_bridge_t *);
static void bridge_voice_run(const setup_bridge_t *,int64_t,int64_t,bool (*)(void));
static bool bridge_voice_restart_required(void);
static void bridge_voice_reap_finished(void);
static void bridge_voice_stop(void);
static void bridge_voice_init(void);
typedef int wifi_ps_type_t;
typedef struct host_ws *esp_websocket_client_handle_t;
typedef struct {
    const char *uri, *headers;
    void (*crt_bundle_attach)(void);
    bool disable_auto_reconnect;
    unsigned network_timeout_ms, buffer_size, task_stack, task_prio;
    unsigned ping_interval_sec, pingpong_timeout_sec;
} esp_websocket_client_config_t;
typedef const char *esp_event_base_t;
#define WIFI_PS_NONE 0
#define BIT0 1
#define BIT1 2
#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#define pdMS_TO_TICKS(n) (n)
#define portMAX_DELAY (-1)
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
#define MALLOC_CAP_INTERNAL 4
#define SNTP_OPMODE_POLL 1
#define WEBSOCKET_EVENT_ANY 0
#define ESP_LOGI(tag, ...) host_log(tag, __VA_ARGS__)
static void host_log(const char *, const char *, ...);
static size_t host_strlcpy(char *, const char *, size_t);
#define strlcpy host_strlcpy
static SemaphoreHandle_t xSemaphoreCreateMutex(void);
static void xSemaphoreTake(SemaphoreHandle_t, int);
static void xSemaphoreGive(SemaphoreHandle_t);
static int64_t esp_timer_get_time(void);
static unsigned xTaskGetTickCount(void);
static void vTaskDelay(unsigned);
static int xTaskCreate(void (*)(void *),const char *,unsigned,void *,unsigned,TaskHandle_t *);
static int xTaskCreatePinnedToCore(void (*)(void *),const char *,unsigned,void *,unsigned,TaskHandle_t *,int);
static void vTaskDelete(TaskHandle_t);
static void vTaskSuspend(TaskHandle_t);
static eTaskState eTaskGetState(TaskHandle_t);
static StreamBufferHandle_t xStreamBufferCreateStatic(size_t,size_t,uint8_t *,StaticStreamBuffer_t *);
static size_t xStreamBufferSend(StreamBufferHandle_t,const void *,size_t,unsigned);
static size_t xStreamBufferReceive(StreamBufferHandle_t,void *,size_t,unsigned);
static size_t xStreamBufferBytesAvailable(StreamBufferHandle_t);
static void vStreamBufferDelete(StreamBufferHandle_t);
static EventGroupHandle_t xEventGroupCreate(void);
static unsigned xEventGroupSetBits(EventGroupHandle_t,unsigned);
static unsigned xEventGroupWaitBits(EventGroupHandle_t,unsigned,int,int,int);
static void vEventGroupDelete(EventGroupHandle_t);
static void *heap_caps_malloc(size_t,unsigned);
static unsigned heap_caps_get_free_size(unsigned);
static void *host_calloc(size_t,size_t);
static void host_free(void *);
static void network_snapshot(network_status_t *);
static esp_err_t network_copy_api_key(char *,size_t);
static esp_err_t esp_wifi_get_ps(wifi_ps_type_t *);
static esp_err_t esp_wifi_set_ps(wifi_ps_type_t);
static const char *esp_err_to_name(esp_err_t);
static bool esp_sntp_enabled(void);
static void esp_sntp_setoperatingmode(int);
static void esp_sntp_setservername(int,const char *);
static void esp_sntp_init(void);
static void esp_crt_bundle_attach(void);
static esp_websocket_client_handle_t esp_websocket_client_init(const esp_websocket_client_config_t *);
static void esp_websocket_register_events(esp_websocket_client_handle_t,int,void (*)(void *,esp_event_base_t,int32_t,void *),void *);
static esp_err_t esp_websocket_client_start(esp_websocket_client_handle_t);
static int esp_websocket_client_send_text(esp_websocket_client_handle_t,const char *,size_t,unsigned);
static void esp_websocket_client_stop(esp_websocket_client_handle_t);
static void esp_websocket_client_destroy(esp_websocket_client_handle_t);
static void websocket_event(void *,esp_event_base_t,int32_t,void *);
static bool tailnet_spike_audio_acquire(void);
static void tailnet_spike_audio_release(void);
/* Real base64 except explicit encode fault; deterministic boundary scheduling. */
static int host_encode(unsigned char *,size_t,size_t *,const unsigned char *,size_t);