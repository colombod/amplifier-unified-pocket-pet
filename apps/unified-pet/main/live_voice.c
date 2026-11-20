#include "live_voice.h"
#include "bridge_voice.h"
#include "tailnet_spike.h"
#include "live_protocol.h"
#include "assistant_config.h"
#include "audio_test.h"
#include "network.h"
#include <assert.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_sntp.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "mbedtls/base64.h"
#include "mbedtls/platform_util.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/event_groups.h"

#define CHUNK 480
#define MESSAGE_CAP (96*1024)
/* Live may generate faster than playback. About 21.8 seconds at 24 kHz mono;
 * a bounded queue absorbs a short spoken answer without dropping its tail. */
#define PLAYBACK_CAP (1024*1024)
#define CAPTURE_CAP 96000
#define INPUT_BYTES 4800
#define VOICE_SESSION_LIMIT_MS 60000
/* A host-bridge conversation may run for the host's 600 s limit. The host ends
 * it there; this later local cutoff (startup allowance included) is only the
 * backstop for a host that never closes. The first call counts from the
 * accepted Start; a call re-established after a lost connection gets the same
 * allowance from its own start, so the bridge owns these deadlines. */
#define BRIDGE_SESSION_LIMIT_MS 660000
#define PLAYBACK_START_MS 320
#define PLAYBACK_START_BYTES (48000*PLAYBACK_START_MS/1000)
static const char *TAG="pet_live";
static SemaphoreHandle_t status_lock;
static TaskHandle_t worker_task;
static bool worker_finished;
static live_status_t status;
static live_status_t bridge_pending;
static bool bridge_reaping,bridge_pending_valid;
static atomic_bool stop_requested;
/* Admission cutoff from the accepted user Start, not connection/audio readiness.
 * Protected by status_lock; lifetime remains owned until finished-worker reap. */
static int64_t session_deadline_us;
/* Numeric diagnostics only. Every access is protected by status_lock, including
 * worker/capture updates and snapshots. Stage: 0 none, 1 read, 2 queue, 3 encode,
 * 4 transport write, 5 other session failure. "uncertain" PCM belongs to a frame
 * whose write did not report full success; it is never replayed as new input. */
typedef struct {
 uint64_t captured,queued,dequeued,sent,dropped,uncertain;
 unsigned current,highwater,inflight,abandoned,overflows,stage;
 unsigned max_read_us,max_encode_us,max_send_us,max_dequeue_gap_us;
 int64_t last_dequeue_at;
} voice_pipeline_t;
typedef struct {
 esp_websocket_client_handle_t ws;
 atomic_bool connected,ready,closed,failed,closing,thinking;
 atomic_uint received_bytes,played_bytes;
 atomic_bool audio_stopping;
 atomic_uint speaker_until;
 unsigned returned_peak,captured_peak;
 unsigned playback_underruns,audible_underruns,output_events,max_audio_gap_ms,max_audio_chunk_bytes;
 int64_t last_audio_at;
 unsigned sent_input_bytes,max_send_ms,input_transcript_chars,output_transcript_chars;
 uint64_t capture_samples,capture_squares;
 StreamBufferHandle_t playback,capture;
 StaticStreamBuffer_t playback_meta,capture_meta;
 EventGroupHandle_t audio_done;
 EventBits_t audio_tasks;
 audio_session_t *audio;
 uint8_t *playback_memory,*capture_memory,*decoded,*input;
 char *message,*input_event;
 live_message_t assembly;
 voice_pipeline_t pipeline;
} live_connection_t;

static void set_state(live_state_t state,const char *message) {
 xSemaphoreTake(status_lock,portMAX_DELAY);
 /* A concurrent audio iteration must not erase an error from the WS callback. */
 if(status.active && status.state==LIVE_ERROR && state!=LIVE_ERROR){
  /* A completed retained bridge cleanup must publish after the worker has
   * actually been deleted, without letting ordinary audio mask an error. */
  if(bridge_reaping){bridge_pending.state=state;strlcpy(bridge_pending.message,message,sizeof(bridge_pending.message));bridge_pending_valid=true;}
  xSemaphoreGive(status_lock);return;
 }
 bool changed=status.state!=state || strcmp(status.message,message);
 status.state=state;strlcpy(status.message,message,sizeof(status.message));
 xSemaphoreGive(status_lock);
 if(changed)ESP_LOGI(TAG,"state=%d %s",state,message);
}
void live_voice_snapshot(live_status_t *out) {xSemaphoreTake(status_lock,portMAX_DELAY);*out=status;xSemaphoreGive(status_lock);}
void live_voice_bridge_status(live_state_t state,const char *message){set_state(state,message);}
static bool session_stop_locked(void) {
 if(session_deadline_us && esp_timer_get_time()>=session_deadline_us)
  atomic_store(&stop_requested,true);
 return atomic_load(&stop_requested);
}
static bool session_stop(void) {
 xSemaphoreTake(status_lock,portMAX_DELAY);
 bool stopped=session_stop_locked();
 xSemaphoreGive(status_lock);
 return stopped;
}
/* The bridge clears session_deadline_us before entering its per-call loop.
 * The atomic mic-off latch is therefore the only parent Stop condition here;
 * bridge_voice itself enforces each call's deadline. This callback must not
 * wait on status_lock while the transport owns its I/O mutex. */
static bool bridge_session_stop(void) {return atomic_load(&stop_requested);}
bool live_voice_bridge_audio_begin(void) {
 xSemaphoreTake(status_lock,portMAX_DELAY);
 if(session_stop_locked()){xSemaphoreGive(status_lock);return false;}
 return true;
}
void live_voice_bridge_audio_end(void) {xSemaphoreGive(status_lock);}
static void pipeline_fail(live_connection_t *c,unsigned stage,const char *message) {
 /* Failure, Stop and append admission share one short arbitration section.
  * The winning UI publication is outside it; transport never holds this lock. */
 xSemaphoreTake(status_lock,portMAX_DELAY);
 bool won=!atomic_exchange(&c->failed,true);
 if(won)c->pipeline.stage=stage;
 xSemaphoreGive(status_lock);
 if(won)set_state(LIVE_ERROR,message);
}
static void fail(live_connection_t *c,const char *message) {
 pipeline_fail(c,5,message);
}
static void pipeline_log(live_connection_t *c) {
 xSemaphoreTake(status_lock,portMAX_DELAY);voice_pipeline_t p=c->pipeline;xSemaphoreGive(status_lock);
 ESP_LOGI(TAG,"Mic pipeline captured=%llu queued=%llu dequeued=%llu sent=%llu dropped=%llu uncertain=%llu current=%u highwater=%u abandoned=%u overflow=%u max_dequeue_gap_us=%u max_read_us=%u max_encode_us=%u max_send_us=%u stage=%u",
  (unsigned long long)p.captured,(unsigned long long)p.queued,(unsigned long long)p.dequeued,
  (unsigned long long)p.sent,(unsigned long long)p.dropped,(unsigned long long)p.uncertain,
  p.current,p.highwater,p.abandoned,p.overflows,p.max_dequeue_gap_us,p.max_read_us,p.max_encode_us,p.max_send_us,p.stage);
}
static bool send_text(live_connection_t *c,const char *text) {
 size_t length=strlen(text);int64_t began=esp_timer_get_time();
 int written=esp_websocket_client_send_text(c->ws,text,length,pdMS_TO_TICKS(1000));
 unsigned elapsed=esp_timer_get_time()-began;
 xSemaphoreTake(status_lock,portMAX_DELAY);
 if(elapsed>c->pipeline.max_send_us)c->pipeline.max_send_us=elapsed;
 xSemaphoreGive(status_lock);
 if(written!=(int)length){pipeline_fail(c,4,"Voice connection write failed. Tap Talk to retry.");return false;}
 return true;
}
static bool speaker_active(live_connection_t *c) {
 return (int32_t)(atomic_load(&c->speaker_until)-(uint32_t)xTaskGetTickCount())>0;
}
static void capture_audio(void *arg) {
 live_connection_t *c=arg;int16_t pcm[CHUNK];
 while(!atomic_load(&c->audio_stopping) && !atomic_load(&c->failed) && !session_stop()) {
  int64_t began=esp_timer_get_time();
  esp_err_t read_result=audio_stream_read(c->audio,pcm,sizeof(pcm));
  unsigned elapsed=esp_timer_get_time()-began;
  xSemaphoreTake(status_lock,portMAX_DELAY);
  if(elapsed>c->pipeline.max_read_us)c->pipeline.max_read_us=elapsed;
  if(read_result==ESP_OK)c->pipeline.captured+=sizeof(pcm);
  bool stopping=session_stop_locked()||atomic_load(&c->audio_stopping)||atomic_load(&c->failed);
  if(read_result==ESP_OK&&stopping)c->pipeline.dropped+=sizeof(pcm);
  xSemaphoreGive(status_lock);
  if(stopping)break;
  if(read_result!=ESP_OK){pipeline_fail(c,1,"Microphone read failed.");break;}
  unsigned peak=0;for(size_t i=0;i<CHUNK;i++){unsigned value=abs((int)pcm[i]);if(value>peak)peak=value;}
  for(size_t i=0;i<CHUNK;i++){int64_t sample=pcm[i];c->capture_squares+=sample*sample;}c->capture_samples+=CHUNK;
  if(peak>c->captured_peak)c->captured_peak=peak;
  if(speaker_active(c))memset(pcm,0,sizeof(pcm));
  xSemaphoreTake(status_lock,portMAX_DELAY);
  if(session_stop_locked()||atomic_load(&c->audio_stopping)||atomic_load(&c->failed)) {
   c->pipeline.dropped+=sizeof(pcm);
   xSemaphoreGive(status_lock);
   break;
  }
  size_t written=xStreamBufferSend(c->capture,pcm,sizeof(pcm),0);
  c->pipeline.queued+=written;c->pipeline.dropped+=sizeof(pcm)-written;
  c->pipeline.current=xStreamBufferBytesAvailable(c->capture);
  if(c->pipeline.current>c->pipeline.highwater)c->pipeline.highwater=c->pipeline.current;
  unsigned gap=esp_timer_get_time()-c->pipeline.last_dequeue_at;
  if(gap>c->pipeline.max_dequeue_gap_us)c->pipeline.max_dequeue_gap_us=gap;
  bool queue_failure=written!=sizeof(pcm),won=false;
  if(queue_failure) {
   c->pipeline.overflows++;
   won=!atomic_exchange(&c->failed,true);
   if(won)c->pipeline.stage=2;
  }
  xSemaphoreGive(status_lock);
  if(queue_failure) {
   if(won)set_state(LIVE_ERROR,"Mic stream cannot keep up. Tap Start voice to retry.");
   break;
  }
 }
 mbedtls_platform_zeroize(pcm,sizeof(pcm));xEventGroupSetBits(c->audio_done,BIT0);vTaskDelete(NULL);
}
static void play_audio(void *arg) {
 live_connection_t *c=arg;int16_t pcm[CHUNK];bool primed=false,fade_in=false;int16_t last=0;
 while(!atomic_load(&c->audio_stopping) && !atomic_load(&c->failed) && !atomic_load(&stop_requested)) {
  if(!primed) {
   if(xStreamBufferBytesAvailable(c->playback)<PLAYBACK_START_BYTES){vTaskDelay(pdMS_TO_TICKS(5));continue;}
   primed=true;fade_in=true;
  }
  size_t bytes=xStreamBufferReceive(c->playback,pcm,sizeof(pcm),0);
  if(bytes<sizeof(pcm)) {
   c->playback_underruns++;primed=false;
   if(abs((int)last)>=250)c->audible_underruns++;
   size_t samples=bytes/2;
   int start=samples?pcm[samples-1]:last;
   for(size_t i=samples;i<CHUNK;i++){int remaining=240-(int)(i-samples);pcm[i]=remaining>0?start*remaining/240:0;}
   bytes=sizeof(pcm);
  }
  if(fade_in){for(unsigned i=0;i<240;i++)pcm[i]=(int32_t)pcm[i]*i/240;fade_in=false;}
  last=pcm[CHUNK-1];
  /* Live emits continuous audio, including digital silence. Only audible
   * samples suppress the mic; packet arrival alone must never do so. */
  if(live_pcm_audible(pcm,bytes))atomic_store(&c->speaker_until,(uint32_t)xTaskGetTickCount()+pdMS_TO_TICKS(300));
  if(audio_stream_write(c->audio,pcm,bytes)!=ESP_OK){fail(c,"Speaker playback failed.");break;}
  atomic_fetch_add(&c->played_bytes,bytes);
 }
 mbedtls_platform_zeroize(pcm,sizeof(pcm));xEventGroupSetBits(c->audio_done,BIT1);vTaskDelete(NULL);
}
static void handle_message(live_connection_t *c) {
 cJSON *root=cJSON_ParseWithLength(c->message,c->assembly.used+1);
 if(!root){fail(c,"Invalid voice event received.");return;}
 const cJSON *type=cJSON_GetObjectItemCaseSensitive(root,"type");
 if(!cJSON_IsString(type)){fail(c,"Voice event has no type.");goto done;}
 if(!strcmp(type->valuestring,"session.started")) {
  atomic_store(&c->ready,true);ESP_LOGI(TAG,"session.started received");
 } else if(!strcmp(type->valuestring,"session.closed")) {
  atomic_store(&c->closed,true);ESP_LOGI(TAG,"session.closed received; finalization confirmed");
 } else if(!strcmp(type->valuestring,"session.output_audio.delta")) {
  if(atomic_load(&c->closing))goto done;
  const cJSON *delta=cJSON_GetObjectItemCaseSensitive(root,"delta");size_t count=0;
  if(!cJSON_IsString(delta) || mbedtls_base64_decode(c->decoded,MESSAGE_CAP,&count,(const unsigned char *)delta->valuestring,strlen(delta->valuestring))!=0 || count%2) {
   fail(c,"Invalid voice audio received.");goto done;
  }
  atomic_fetch_add(&c->received_bytes,count);
  int64_t received_at=esp_timer_get_time();unsigned gap=c->last_audio_at?(received_at-c->last_audio_at)/1000:0;
  if(gap>c->max_audio_gap_ms)c->max_audio_gap_ms=gap;
  c->last_audio_at=received_at;c->output_events++;if(count>c->max_audio_chunk_bytes)c->max_audio_chunk_bytes=count;
  unsigned peak=live_pcm_peak(c->decoded,count);
  if(peak>c->returned_peak)c->returned_peak=peak;
  if(xStreamBufferSend(c->playback,c->decoded,count,0)!=count)fail(c,"Speaker buffer full. Tap Talk to retry.");
  if(peak>=250)atomic_store(&c->thinking,false);
 } else if(!strcmp(type->valuestring,"session.input_transcript.delta") || !strcmp(type->valuestring,"session.output_transcript.delta")) {
  const cJSON *delta=cJSON_GetObjectItemCaseSensitive(root,"delta");
  if(cJSON_IsString(delta)){if(!strcmp(type->valuestring,"session.input_transcript.delta"))c->input_transcript_chars+=strlen(delta->valuestring);else c->output_transcript_chars+=strlen(delta->valuestring);}
 } else if(!strcmp(type->valuestring,"session.commentary.appended")) {
  ESP_LOGI(TAG,"Greeting instruction acknowledged; speech not yet confirmed");
 } else if(!strcmp(type->valuestring,"session.delegation.created")) {
  atomic_store(&c->thinking,true);ESP_LOGI(TAG,"Reasoning backend requested");
 } else if(!strcmp(type->valuestring,"error")) {
  /* Never print server message bodies: these can quote submitted material. */
  const cJSON *error=cJSON_GetObjectItemCaseSensitive(root,"error");
  const cJSON *code=cJSON_GetObjectItemCaseSensitive(error,"code");
  char text[112]="OpenAI rejected the session. Check key and model access.";
  if(cJSON_IsString(code) && strlen(code->valuestring)<64 && strspn(code->valuestring,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")==strlen(code->valuestring))
   snprintf(text,sizeof(text),"OpenAI error: %s",code->valuestring);
  fail(c,text);
 } else if(!strcmp(type->valuestring,"response.event")) {
  const cJSON *event=cJSON_GetObjectItemCaseSensitive(root,"event");
  const cJSON *nested=cJSON_GetObjectItemCaseSensitive(event,"type");
  if(cJSON_IsString(nested) && (!strcmp(nested->valuestring,"response.failed") || !strcmp(nested->valuestring,"error")))fail(c,"Reasoning backend failed. Check model access and quota.");
 }
 /* Transcript and all other content stays unlogged and unpersisted. */
done:
 cJSON_Delete(root);
}
static void websocket_event(void *arg,esp_event_base_t base,int32_t id,void *data) {
 live_connection_t *c=arg;esp_websocket_event_data_t *event=data;
 if(id==WEBSOCKET_EVENT_CONNECTED)atomic_store(&c->connected,true);
 else if(id==WEBSOCKET_EVENT_DISCONNECTED || id==WEBSOCKET_EVENT_CLOSED)atomic_store(&c->connected,false);
 else if(id==WEBSOCKET_EVENT_ERROR) {
  int http=event?event->error_handle.esp_ws_handshake_status_code:0;char text[112];
  if(http>=400 && http<=599)snprintf(text,sizeof(text),"OpenAI connection rejected (HTTP %d). Check key, quota and access.",http);
  else strlcpy(text,"Voice connection failed. Check Wi-Fi and try again.",sizeof(text));
  fail(c,text);
 } else if(id==WEBSOCKET_EVENT_DATA) {
  if(event->data_len<0 || event->payload_len<0 || event->payload_offset<0){fail(c,"Invalid voice frame.");return;}
  int complete=live_message_feed(&c->assembly,c->message,MESSAGE_CAP,event->op_code,event->fin,
   event->payload_offset,event->payload_len,event->data_ptr,event->data_len);
  if(complete<0)fail(c,"Voice event exceeded buffer or arrived out of order.");
  else if(complete==1)handle_message(c);
 }
}
static bool synchronize_clock(void) {
 if(time(NULL)>1760000000)return true;
 set_state(LIVE_CONNECTING,"Setting the clock for secure voice...");
 if(!esp_sntp_enabled()){esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);esp_sntp_setservername(0,"time.cloudflare.com");esp_sntp_setservername(1,"pool.ntp.org");esp_sntp_init();}
 for(int i=0;i<150 && !session_stop();i++) {if(time(NULL)>1760000000)return true;vTaskDelay(pdMS_TO_TICKS(100));}
 return false;
}
static void run_session(void) {
 setup_bridge_t bridge_cfg;
 esp_err_t bridge_err=network_copy_bridge(&bridge_cfg);
 if(bridge_err!=ESP_OK){set_state(LIVE_ERROR,"Bridge settings invalid; no fallback.");return;}
 if(bridge_cfg.enabled){
  network_status_t net;network_snapshot(&net);
  if(!net.connected){set_state(LIVE_ERROR,"Connect Wi-Fi first.");return;}
  /* Mic turned off before the call began: say so, never leave "connecting". */
  if(session_stop()){set_state(LIVE_OFF,"Mic off");return;}
  xSemaphoreTake(status_lock,portMAX_DELAY);
  int64_t bridge_deadline=session_deadline_us+(int64_t)(BRIDGE_SESSION_LIMIT_MS-VOICE_SESSION_LIMIT_MS)*1000;
  session_deadline_us=0; /* per-call deadlines below; mic off still stops everything */
  xSemaphoreGive(status_lock);
  /* Match the working standalone streaming path: modem sleep adds avoidable
   * latency while the bounded microphone queue is being drained over TLS. */
  wifi_ps_type_t saved_ps=WIFI_PS_NONE;
  if(esp_wifi_get_ps(&saved_ps)!=ESP_OK){set_state(LIVE_ERROR,"Cannot check Wi-Fi streaming mode.");return;}
  bool restore_ps=saved_ps!=WIFI_PS_NONE;
  if(restore_ps&&esp_wifi_set_ps(WIFI_PS_NONE)!=ESP_OK){set_state(LIVE_ERROR,"Cannot enable low-latency Wi-Fi for bridge.");return;}
  if(!synchronize_clock())set_state(session_stop()?LIVE_OFF:LIVE_ERROR,session_stop()?"Mic off":"Clock sync failed. Check internet access.");
  else if(!session_stop())bridge_voice_run(&bridge_cfg,bridge_deadline,(int64_t)BRIDGE_SESSION_LIMIT_MS*1000,bridge_session_stop);
  else set_state(LIVE_OFF,"Mic off");
  if(restore_ps&&esp_wifi_set_ps(saved_ps)!=ESP_OK)ESP_LOGI(TAG,"Cannot restore Wi-Fi power save after bridge.");
  return; /* no direct key, greeting or fallback on ANY bridge outcome */
 }
 char key[256]={0},headers[300]={0};audio_session_t *audio=NULL;
 wifi_ps_type_t saved_ps=WIFI_PS_NONE;bool restore_ps=false;
 live_connection_t *c=calloc(1,sizeof(*c));
 if(!c){set_state(LIVE_ERROR,"Not enough memory for voice.");return;}
 network_status_t network;network_snapshot(&network);
 if(!network.connected || !network.key_saved){set_state(LIVE_ERROR,"Connect Wi-Fi and save your OpenAI key first.");goto cleanup;}
 if(esp_wifi_get_ps(&saved_ps)!=ESP_OK){fail(c,"Cannot check Wi-Fi streaming mode.");goto cleanup;}
 if(saved_ps!=WIFI_PS_NONE) {
  if(esp_wifi_set_ps(WIFI_PS_NONE)!=ESP_OK){fail(c,"Cannot enable low-latency Wi-Fi for voice.");goto cleanup;}
  restore_ps=true;
 }
 ESP_LOGI(TAG,"Voice Wi-Fi power save off; previous_mode=%d",saved_ps);
 if(!synchronize_clock()){if(!session_stop())set_state(LIVE_ERROR,"Clock sync failed. Check internet access and try again.");goto cleanup;}
 if(session_stop())goto cleanup;
 if(network_copy_api_key(key,sizeof(key))!=ESP_OK){set_state(LIVE_ERROR,"Cannot read saved OpenAI key. Open Wi-Fi setup.");goto cleanup;}
 snprintf(headers,sizeof(headers),"Authorization: Bearer %s\r\n",key);mbedtls_platform_zeroize(key,sizeof(key));
 c->message=heap_caps_malloc(MESSAGE_CAP,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 c->decoded=heap_caps_malloc(MESSAGE_CAP,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 c->playback_memory=heap_caps_malloc(PLAYBACK_CAP+1,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 c->capture_memory=heap_caps_malloc(CAPTURE_CAP+1,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 c->input=heap_caps_malloc(INPUT_BYTES,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 c->input_event=heap_caps_malloc(7000,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 if(!c->message || !c->decoded || !c->playback_memory || !c->capture_memory || !c->input || !c->input_event){set_state(LIVE_ERROR,"Not enough audio buffer memory.");goto cleanup;}
 c->playback=xStreamBufferCreateStatic(PLAYBACK_CAP+1,1,c->playback_memory,&c->playback_meta);assert(c->playback);
 c->capture=xStreamBufferCreateStatic(CAPTURE_CAP+1,INPUT_BYTES,c->capture_memory,&c->capture_meta);assert(c->capture);
 c->audio_done=xEventGroupCreate();if(!c->audio_done){set_state(LIVE_ERROR,"Cannot prepare audio workers.");goto cleanup;}
 /* Transport error logs contain numeric diagnostics only at this level.
  * Never enable HTTP/transport debug logs: they may contain authorization. */
 esp_websocket_client_config_t config={.uri="wss://api.openai.com/v1/live/sessions",.headers=headers,
  .crt_bundle_attach=esp_crt_bundle_attach,.disable_auto_reconnect=true,.network_timeout_ms=5000,
  .buffer_size=8192,.task_stack=8192,.task_prio=5,.ping_interval_sec=15,.pingpong_timeout_sec=30};
 c->ws=esp_websocket_client_init(&config);mbedtls_platform_zeroize(headers,sizeof(headers));
 if(!c->ws){set_state(LIVE_ERROR,"Cannot prepare secure voice connection.");goto cleanup;}
 esp_websocket_register_events(c->ws,WEBSOCKET_EVENT_ANY,websocket_event,c);
 set_state(LIVE_CONNECTING,"Connecting to GPT-Live...");
 if(esp_websocket_client_start(c->ws)!=ESP_OK){fail(c,"Cannot start voice connection.");goto cleanup;}
 int64_t deadline=esp_timer_get_time()+25000000;
 while(!atomic_load(&c->connected) && !atomic_load(&c->failed) && !session_stop() && esp_timer_get_time()<deadline)vTaskDelay(pdMS_TO_TICKS(20));
 if(!atomic_load(&c->connected)){if(!session_stop() && !atomic_load(&c->failed))fail(c,"Voice connection timed out.");goto cleanup;}
 if(session_stop())goto cleanup;
 if(!send_text(c,PET_LIVE_START))goto cleanup;
 while(!atomic_load(&c->ready) && !atomic_load(&c->failed) && atomic_load(&c->connected) && !session_stop() && esp_timer_get_time()<deadline)vTaskDelay(pdMS_TO_TICKS(20));
 if(!atomic_load(&c->ready)){if(!session_stop() && !atomic_load(&c->failed))fail(c,"GPT-Live did not start. Check model access.");goto cleanup;}
 if(session_stop())goto cleanup;
 esp_err_t audio_result=audio_stream_open(&audio);
 if(session_stop())goto cleanup;
 if(audio_result!=ESP_OK){fail(c,"Audio device is busy or unavailable.");goto cleanup;}
 c->audio=audio;
 xSemaphoreTake(status_lock,portMAX_DELAY);c->pipeline.last_dequeue_at=esp_timer_get_time();xSemaphoreGive(status_lock);
 if(xTaskCreate(capture_audio,"live_capture",4096,c,5,NULL)!=pdPASS){fail(c,"Cannot start microphone worker.");goto cleanup;}
 c->audio_tasks|=BIT0;
 if(xTaskCreate(play_audio,"live_playback",4096,c,5,NULL)!=pdPASS){fail(c,"Cannot start speaker worker.");goto cleanup;}
 c->audio_tasks|=BIT1;
 ESP_LOGI(TAG,"Audio streaming ready; internal_free=%u psram_free=%u",(unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),(unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
 set_state(LIVE_LISTENING,"Listening. Say hello to Tachikoma.");
 if(!send_text(c,PET_GREETING_EVENT))goto cleanup;
 int64_t started=esp_timer_get_time(),last_diagnostic=started;
 while(!session_stop() && !atomic_load(&c->failed) && !atomic_load(&c->closed) && atomic_load(&c->connected)) {
  size_t input_bytes=xStreamBufferReceive(c->capture,c->input,INPUT_BYTES,pdMS_TO_TICKS(100));
  if(input_bytes) {
   xSemaphoreTake(status_lock,portMAX_DELAY);
   int64_t dequeued_at=esp_timer_get_time();unsigned gap=dequeued_at-c->pipeline.last_dequeue_at;
   c->pipeline.last_dequeue_at=dequeued_at;
   if(gap>c->pipeline.max_dequeue_gap_us)c->pipeline.max_dequeue_gap_us=gap;
   c->pipeline.dequeued+=input_bytes;c->pipeline.inflight=input_bytes;
   c->pipeline.current=xStreamBufferBytesAvailable(c->capture);
   xSemaphoreGive(status_lock);
  }
  /* Failure can arrive while Receive waits for its trigger/timeout. Do not
   * encode/send that wakeup's straggler (possibly a partial PCM sample).
   * Cleanup accounts its inflight bytes after capture has exited. */
  if(session_stop() || atomic_load(&c->failed))break;
  int64_t now=esp_timer_get_time();
  bool speaking=speaker_active(c);
  if(!input_bytes)continue;
  size_t count=0;
  const char prefix[]="{\"type\":\"session.input_audio.append\",\"audio\":\"";
  memcpy(c->input_event,prefix,sizeof(prefix)-1);
  int64_t encode_started=esp_timer_get_time();
  int encoded=mbedtls_base64_encode((unsigned char*)c->input_event+sizeof(prefix)-1,7000-sizeof(prefix)-3,&count,c->input,input_bytes);
  unsigned encode_us=esp_timer_get_time()-encode_started;
  xSemaphoreTake(status_lock,portMAX_DELAY);
  if(encode_us>c->pipeline.max_encode_us)c->pipeline.max_encode_us=encode_us;
  bool stopped=session_stop_locked();
  xSemaphoreGive(status_lock);
  if(stopped)break;
  if(encoded!=0){pipeline_fail(c,3,"Audio encoding failed.");break;}
  memcpy(c->input_event+sizeof(prefix)-1+count,"\"}",3);
  /* Linearization point for a NEW audio append, after encoding. Failure and
   * Stop use the same lock. Once admitted, this single sender's call may finish
   * even if Stop/failure follows; no attempt is made to abort an in-flight write.
   * Keep inflight bytes for cleanup when admission is denied. session.close
   * deliberately uses send_text directly and is not blocked by this gate. */
  xSemaphoreTake(status_lock,portMAX_DELAY);
  bool admitted=!session_stop_locked()&&!atomic_load(&c->failed);
  xSemaphoreGive(status_lock);
  if(!admitted)break;
  int64_t send_started=esp_timer_get_time();
  bool sent=send_text(c,c->input_event);
  xSemaphoreTake(status_lock,portMAX_DELAY);
  if(sent)c->pipeline.sent+=input_bytes;else c->pipeline.uncertain+=input_bytes;
  c->pipeline.inflight=0;
  xSemaphoreGive(status_lock);
  if(!sent)break;
  c->sent_input_bytes+=input_bytes;unsigned send_ms=(esp_timer_get_time()-send_started)/1000;if(send_ms>c->max_send_ms)c->max_send_ms=send_ms;
  set_state(speaking?LIVE_SPEAKING:atomic_load(&c->thinking)?LIVE_THINKING:LIVE_LISTENING,
   speaking?"Speaking. Mic resumes after playback.":atomic_load(&c->thinking)?"Thinking...":"Listening. Tap Stop to finish.");
  xSemaphoreTake(status_lock,portMAX_DELAY);status.seconds=(now-started)/1000000;xSemaphoreGive(status_lock);
  if(now-last_diagnostic>=1000000){pipeline_log(c);last_diagnostic=now;}
  /* Keep this explicitly started conversation open until Stop or a failure. */
 }
 if(!session_stop() && !atomic_load(&c->closed) && !atomic_load(&c->failed))fail(c,"Voice disconnected. Final usage unconfirmed.");
cleanup:
 /* Stop physical capture/playback before waiting for finalization. */
 atomic_store(&c->audio_stopping,true);
 if(c->audio_tasks)xEventGroupWaitBits(c->audio_done,c->audio_tasks,pdFALSE,pdTRUE,portMAX_DELAY);
 if(audio)audio_stream_close(audio);
 /* Capture is now stopped. Dispose of every queued/dequeued-but-unsent byte,
  * including a partial RTOS write and Stop racing with read/dequeue. Failed
  * transport frames remain explicitly uncertain, never counted as delivered. */
 xSemaphoreTake(status_lock,portMAX_DELAY);
 c->pipeline.abandoned=c->pipeline.queued-c->pipeline.dequeued+c->pipeline.inflight;
 c->pipeline.dropped+=c->pipeline.abandoned;c->pipeline.inflight=0;c->pipeline.current=0;
 xSemaphoreGive(status_lock);
 atomic_store(&c->closing,true);
 bool finalized=atomic_load(&c->closed);
 if(c->ws && atomic_load(&c->connected) && atomic_load(&c->ready) && !finalized) {
  if(!atomic_load(&c->failed))set_state(LIVE_CLOSING,"Microphone off. Closing session...");
  if(send_text(c,"{\"type\":\"session.close\"}")) {
   int64_t until=esp_timer_get_time()+15000000;
   while(!atomic_load(&c->closed) && atomic_load(&c->connected) && esp_timer_get_time()<until)vTaskDelay(pdMS_TO_TICKS(50));
  }
  finalized=atomic_load(&c->closed);
  if(!finalized && !atomic_load(&c->failed))fail(c,"Microphone off. Session finalization unconfirmed.");
 }
 if(c->ws){esp_websocket_client_stop(c->ws);esp_websocket_client_destroy(c->ws);}
 pipeline_log(c);
 audio_test_status_t audio_status;audio_test_snapshot(&audio_status);
 ESP_LOGI(TAG,"Session audio bytes received=%u played=%u output_peak=%u input_peak=%u volume=%d; finalization=%s",atomic_load(&c->received_bytes),atomic_load(&c->played_bytes),c->returned_peak,c->captured_peak,audio_status.volume,finalized?"confirmed":"unconfirmed");
 ESP_LOGI(TAG,"Input captured=%llu sent=%u rms=%u max_send_ms=%u; transcript character counts input=%u output=%u (content not logged)",c->capture_samples*2,c->sent_input_bytes,c->capture_samples?(unsigned)sqrt((double)c->capture_squares/c->capture_samples):0,c->max_send_ms,c->input_transcript_chars,c->output_transcript_chars);
 ESP_LOGI(TAG,"Playback jitter buffer=%ums underruns=%u audible_tail_underruns=%u; output_events=%u max_gap_ms=%u max_chunk_bytes=%u",PLAYBACK_START_MS,c->playback_underruns,c->audible_underruns,c->output_events,c->max_audio_gap_ms,c->max_audio_chunk_bytes);
 if(restore_ps){esp_err_t err=esp_wifi_set_ps(saved_ps);ESP_LOGI(TAG,"Wi-Fi power-save restore=%s",esp_err_to_name(err));}
 if(c->playback)vStreamBufferDelete(c->playback);
 if(c->capture)vStreamBufferDelete(c->capture);
 if(c->audio_done)vEventGroupDelete(c->audio_done);
 if(c->capture_memory){mbedtls_platform_zeroize(c->capture_memory,CAPTURE_CAP+1);free(c->capture_memory);}
 if(c->input){mbedtls_platform_zeroize(c->input,INPUT_BYTES);free(c->input);}
 if(c->input_event){mbedtls_platform_zeroize(c->input_event,7000);free(c->input_event);}
 if(c->playback_memory){mbedtls_platform_zeroize(c->playback_memory,PLAYBACK_CAP+1);free(c->playback_memory);}
 if(c->decoded){mbedtls_platform_zeroize(c->decoded,MESSAGE_CAP);free(c->decoded);}
 if(c->message){mbedtls_platform_zeroize(c->message,MESSAGE_CAP);free(c->message);}
 mbedtls_platform_zeroize(key,sizeof(key));mbedtls_platform_zeroize(headers,sizeof(headers));
 live_status_t current;live_voice_snapshot(&current);
 if(current.state!=LIVE_ERROR)set_state(LIVE_OFF,finalized?"Disconnected. Session closed.":"Disconnected. Tap Talk when ready.");
 free(c);
}
static void worker(void *arg) {
 (void)arg;
 xSemaphoreTake(status_lock,portMAX_DELAY);xSemaphoreGive(status_lock);
 run_session();
 xSemaphoreTake(status_lock,portMAX_DELAY);worker_finished=true;xSemaphoreGive(status_lock);
 for(;;)vTaskSuspend(NULL);
}
void live_voice_reap_finished(void) {
 if(!status_lock)return;
 xSemaphoreTake(status_lock,portMAX_DELAY);
 if(!bridge_reaping&&worker_finished&&worker_task&&eTaskGetState(worker_task)==eSuspended) {
  /* A late callback/task may finish after the owner's bounded proof wait.
   * Reap local storage only; never reconnect or replay an accepted input. */
  bridge_reaping=true;bridge_pending_valid=false;
  TaskHandle_t finished_task=worker_task;
  xSemaphoreGive(status_lock);
  bridge_voice_reap_finished();
  xSemaphoreTake(status_lock,portMAX_DELAY);
  bridge_reaping=false;
  if(worker_task!=finished_task||!worker_finished){
   bridge_pending_valid=false;xSemaphoreGive(status_lock);return;
  }
  if(bridge_voice_restart_required()){
   bridge_pending_valid=false;
   status.active=false; /* retain worker/admission and every quarantined resource */
   xSemaphoreGive(status_lock);return;
  }
  vTaskDelete(worker_task);
  worker_task=NULL;worker_finished=false;status.active=false;
  if(bridge_pending_valid){
   status.state=bridge_pending.state;strlcpy(status.message,bridge_pending.message,sizeof(status.message));
   bridge_pending_valid=false;
  }
  session_deadline_us=0;
  tailnet_spike_audio_release();
 }
 xSemaphoreGive(status_lock);
}
/* The voice owner runs on core 1. The codec's volume and gain calls use the
 * FPU, and this chip pins a task to its current core at its first FPU use, so
 * an unpinned owner stayed on whichever core it happened to be on for the whole
 * call. On core 0, beside the Wi-Fi and timer tasks, handling one host audio
 * message took 42-44 ms on the pet against 24-25 ms on core 1, and calls fell
 * behind real time (working-files/voice-bench, 20 November 2026). */
#define VOICE_OWNER_CORE 1
/* Above the display task (LVGL, priority 4, about three quarters of a core
 * while the pet is animated) and the socket reader (5). At the display's own
 * priority the owner shared its core with it in time slices and fell behind
 * real time even on core 1, while the other core was a third idle. Its work
 * per 100 ms of audio is bounded, so the display keeps the rest. */
#define VOICE_OWNER_PRIORITY 6
#if defined(PET_PRIVATE_MIC_DIAG)
/* Bench only: 0 or 1 pins the next call's owner there; -1 leaves it unpinned,
 * as it was, to compare the placements. */
static atomic_int bench_voice_core=VOICE_OWNER_CORE,bench_voice_priority=VOICE_OWNER_PRIORITY;
void live_voice_bench_core(int core){atomic_store(&bench_voice_core,core<0?-1:core?1:0);}
void live_voice_bench_priority(int priority){atomic_store(&bench_voice_priority,priority<4?4:priority>6?6:priority);}
#endif
void live_voice_start(void) {
 if(!tailnet_spike_audio_acquire())return;
 audio_test_status_t audio;audio_test_snapshot(&audio);
 xSemaphoreTake(status_lock,portMAX_DELAY);
 if(worker_task||status.active||audio.busy){tailnet_spike_audio_release();xSemaphoreGive(status_lock);return;}
 worker_finished=false;status.active=true;status.seconds=0;status.state=LIVE_CONNECTING;
 audio_stream_prepare(); /* under the same Start/Stop arbitration lock */
 strlcpy(status.message,"Preparing voice...",sizeof(status.message));atomic_store(&stop_requested,false);
 session_deadline_us=esp_timer_get_time()+(int64_t)VOICE_SESSION_LIMIT_MS*1000;
#if defined(PET_PRIVATE_MIC_DIAG)
 int bench_core=atomic_load(&bench_voice_core),bench_priority=atomic_load(&bench_voice_priority);
 if((bench_core<0?xTaskCreate(worker,"pet_live",12288,NULL,bench_priority,&worker_task):
     xTaskCreatePinnedToCore(worker,"pet_live",12288,NULL,bench_priority,&worker_task,bench_core))!=pdPASS) {
#else
 if(xTaskCreatePinnedToCore(worker,"pet_live",12288,NULL,VOICE_OWNER_PRIORITY,&worker_task,VOICE_OWNER_CORE)!=pdPASS) {
#endif
  worker_task=NULL;worker_finished=false;status.active=false;status.state=LIVE_ERROR;
  session_deadline_us=0;
  strlcpy(status.message,"Cannot start voice worker.",sizeof(status.message));
  tailnet_spike_audio_release();
 }
 xSemaphoreGive(status_lock);
}
void live_voice_stop(void) {
 xSemaphoreTake(status_lock,portMAX_DELAY);
 atomic_store(&stop_requested,true);
 xSemaphoreGive(status_lock);
 bridge_voice_stop();
}
void live_voice_init(void) {
 status_lock=xSemaphoreCreateMutex();assert(status_lock);
 bridge_voice_init();
 set_state(LIVE_OFF,"Ready");
}
