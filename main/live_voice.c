#include "live_voice.h"
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
#include "mbedtls/base64.h"
#include "mbedtls/platform_util.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
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
static const char *TAG="pet_live";
static SemaphoreHandle_t status_lock;
static QueueHandle_t jobs;
static live_status_t status;
static atomic_bool stop_requested;
typedef struct {
 esp_websocket_client_handle_t ws;
 atomic_bool connected,ready,closed,failed,closing,thinking;
 atomic_uint received_bytes,played_bytes;
 atomic_bool audio_stopping;
 atomic_uint speaker_until;
 unsigned returned_peak,captured_peak;
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
} live_connection_t;

static void set_state(live_state_t state,const char *message) {
 xSemaphoreTake(status_lock,portMAX_DELAY);
 /* A concurrent audio iteration must not erase an error from the WS callback. */
 if(status.active && status.state==LIVE_ERROR && state!=LIVE_ERROR){xSemaphoreGive(status_lock);return;}
 bool changed=status.state!=state || strcmp(status.message,message);
 status.state=state;strlcpy(status.message,message,sizeof(status.message));
 xSemaphoreGive(status_lock);
 if(changed)ESP_LOGI(TAG,"state=%d %s",state,message);
}
void live_voice_snapshot(live_status_t *out) {xSemaphoreTake(status_lock,portMAX_DELAY);*out=status;xSemaphoreGive(status_lock);}
static void fail(live_connection_t *c,const char *message) {
 if(!atomic_exchange(&c->failed,true))set_state(LIVE_ERROR,message);
}
static bool send_text(live_connection_t *c,const char *text) {
 size_t length=strlen(text);
 if(esp_websocket_client_send_text(c->ws,text,length,pdMS_TO_TICKS(1000))!=(int)length){fail(c,"Voice connection write failed. Tap Talk to retry.");return false;}
 return true;
}
static bool speaker_active(live_connection_t *c) {
 return (int32_t)(atomic_load(&c->speaker_until)-(uint32_t)xTaskGetTickCount())>0;
}
static void capture_audio(void *arg) {
 live_connection_t *c=arg;int16_t pcm[CHUNK];
 while(!atomic_load(&c->audio_stopping) && !atomic_load(&c->failed)) {
  if(audio_stream_read(c->audio,pcm,sizeof(pcm))!=ESP_OK){fail(c,"Microphone read failed.");break;}
  unsigned peak=0;for(size_t i=0;i<CHUNK;i++){unsigned value=abs((int)pcm[i]);if(value>peak)peak=value;}
  for(size_t i=0;i<CHUNK;i++){int64_t sample=pcm[i];c->capture_squares+=sample*sample;}c->capture_samples+=CHUNK;
  if(peak>c->captured_peak)c->captured_peak=peak;
  if(speaker_active(c))memset(pcm,0,sizeof(pcm));
  if(xStreamBufferSend(c->capture,pcm,sizeof(pcm),0)!=sizeof(pcm)){fail(c,"Mic stream cannot keep up. Tap Start voice to retry.");break;}
 }
 mbedtls_platform_zeroize(pcm,sizeof(pcm));xEventGroupSetBits(c->audio_done,BIT0);vTaskDelete(NULL);
}
static void play_audio(void *arg) {
 live_connection_t *c=arg;int16_t pcm[CHUNK];
 while(!atomic_load(&c->audio_stopping) && !atomic_load(&c->failed)) {
  size_t bytes=xStreamBufferReceive(c->playback,pcm,sizeof(pcm),pdMS_TO_TICKS(20));
  if(!bytes)continue;
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
 for(int i=0;i<150 && !atomic_load(&stop_requested);i++) {if(time(NULL)>1760000000)return true;vTaskDelay(pdMS_TO_TICKS(100));}
 return false;
}
static void run_session(void) {
 char key[256]={0},headers[300]={0};audio_session_t *audio=NULL;
 live_connection_t *c=calloc(1,sizeof(*c));
 if(!c){set_state(LIVE_ERROR,"Not enough memory for voice.");return;}
 network_status_t network;network_snapshot(&network);
 if(!network.connected || !network.key_saved){set_state(LIVE_ERROR,"Connect Wi-Fi and save your OpenAI key first.");goto cleanup;}
 if(!synchronize_clock()){if(!atomic_load(&stop_requested))set_state(LIVE_ERROR,"Clock sync failed. Check internet access and try again.");goto cleanup;}
 if(atomic_load(&stop_requested))goto cleanup;
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
 while(!atomic_load(&c->connected) && !atomic_load(&c->failed) && !atomic_load(&stop_requested) && esp_timer_get_time()<deadline)vTaskDelay(pdMS_TO_TICKS(20));
 if(!atomic_load(&c->connected)){if(!atomic_load(&stop_requested) && !atomic_load(&c->failed))fail(c,"Voice connection timed out.");goto cleanup;}
 if(atomic_load(&stop_requested))goto cleanup;
 if(!send_text(c,PET_LIVE_START))goto cleanup;
 while(!atomic_load(&c->ready) && !atomic_load(&c->failed) && atomic_load(&c->connected) && !atomic_load(&stop_requested) && esp_timer_get_time()<deadline)vTaskDelay(pdMS_TO_TICKS(20));
 if(!atomic_load(&c->ready)){if(!atomic_load(&stop_requested) && !atomic_load(&c->failed))fail(c,"GPT-Live did not start. Check model access.");goto cleanup;}
 if(atomic_load(&stop_requested))goto cleanup;
 if(audio_stream_open(&audio)!=ESP_OK){fail(c,"Audio device is busy or unavailable.");goto cleanup;}
 c->audio=audio;
 if(xTaskCreate(capture_audio,"live_capture",4096,c,5,NULL)!=pdPASS){fail(c,"Cannot start microphone worker.");goto cleanup;}
 c->audio_tasks|=BIT0;
 if(xTaskCreate(play_audio,"live_playback",4096,c,5,NULL)!=pdPASS){fail(c,"Cannot start speaker worker.");goto cleanup;}
 c->audio_tasks|=BIT1;
 ESP_LOGI(TAG,"Audio streaming ready; internal_free=%u psram_free=%u",(unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),(unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
 set_state(LIVE_LISTENING,"Listening. Say hello to Tachikoma.");
 if(!send_text(c,PET_GREETING_EVENT))goto cleanup;
 int64_t started=esp_timer_get_time();
 while(!atomic_load(&stop_requested) && !atomic_load(&c->failed) && !atomic_load(&c->closed) && atomic_load(&c->connected)) {
  size_t input_bytes=xStreamBufferReceive(c->capture,c->input,INPUT_BYTES,pdMS_TO_TICKS(100));
  if(atomic_load(&stop_requested))break;
  int64_t now=esp_timer_get_time();
  bool speaking=speaker_active(c);
  if(!input_bytes)continue;
  size_t count=0;
  const char prefix[]="{\"type\":\"session.input_audio.append\",\"audio\":\"";
  memcpy(c->input_event,prefix,sizeof(prefix)-1);
  if(mbedtls_base64_encode((unsigned char*)c->input_event+sizeof(prefix)-1,7000-sizeof(prefix)-3,&count,c->input,input_bytes)!=0){fail(c,"Audio encoding failed.");break;}
  memcpy(c->input_event+sizeof(prefix)-1+count,"\"}",3);
  int64_t send_started=esp_timer_get_time();
  if(!send_text(c,c->input_event))break;
  c->sent_input_bytes+=input_bytes;unsigned send_ms=(esp_timer_get_time()-send_started)/1000;if(send_ms>c->max_send_ms)c->max_send_ms=send_ms;
  set_state(speaking?LIVE_SPEAKING:atomic_load(&c->thinking)?LIVE_THINKING:LIVE_LISTENING,
   speaking?"Speaking. Mic resumes after playback.":atomic_load(&c->thinking)?"Thinking...":"Listening. Tap Stop to finish.");
  xSemaphoreTake(status_lock,portMAX_DELAY);status.seconds=(now-started)/1000000;xSemaphoreGive(status_lock);
  /* Keep this explicitly started conversation open until Stop or a failure. */
 }
 if(!atomic_load(&stop_requested) && !atomic_load(&c->closed) && !atomic_load(&c->failed))fail(c,"Voice disconnected. Final usage unconfirmed.");
cleanup:
 /* Stop physical capture/playback before waiting for finalization. */
 atomic_store(&c->audio_stopping,true);
 if(c->audio_tasks)xEventGroupWaitBits(c->audio_done,c->audio_tasks,pdFALSE,pdTRUE,portMAX_DELAY);
 if(audio)audio_stream_close(audio);
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
 audio_test_status_t audio_status;audio_test_snapshot(&audio_status);
 ESP_LOGI(TAG,"Session audio bytes received=%u played=%u output_peak=%u input_peak=%u volume=%d; finalization=%s",atomic_load(&c->received_bytes),atomic_load(&c->played_bytes),c->returned_peak,c->captured_peak,audio_status.volume,finalized?"confirmed":"unconfirmed");
 ESP_LOGI(TAG,"Input captured=%llu sent=%u rms=%u max_send_ms=%u; transcript character counts input=%u output=%u (content not logged)",c->capture_samples*2,c->sent_input_bytes,c->capture_samples?(unsigned)sqrt((double)c->capture_squares/c->capture_samples):0,c->max_send_ms,c->input_transcript_chars,c->output_transcript_chars);
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
 for(;;){bool job;if(xQueueReceive(jobs,&job,portMAX_DELAY)!=pdTRUE)continue;run_session();
  xSemaphoreTake(status_lock,portMAX_DELAY);status.active=false;xSemaphoreGive(status_lock);}
}
void live_voice_start(void) {
 audio_test_status_t audio;audio_test_snapshot(&audio);
 xSemaphoreTake(status_lock,portMAX_DELAY);
 if(!status.active && !audio.busy){status.active=true;status.seconds=0;status.state=LIVE_CONNECTING;strlcpy(status.message,"Preparing voice...",sizeof(status.message));atomic_store(&stop_requested,false);bool job=true;
  if(xQueueSend(jobs,&job,0)!=pdTRUE){status.active=false;status.state=LIVE_ERROR;strlcpy(status.message,"Voice worker is busy.",sizeof(status.message));}}
 xSemaphoreGive(status_lock);
}
void live_voice_stop(void) {atomic_store(&stop_requested,true);}
void live_voice_init(void) {
 status_lock=xSemaphoreCreateMutex();jobs=xQueueCreate(1,sizeof(bool));assert(status_lock&&jobs);
 set_state(LIVE_OFF,"Tap Talk to start voice.");
 assert(xTaskCreate(worker,"pet_live",12288,NULL,4,NULL)==pdPASS);
}
