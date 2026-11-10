/* ES8311 wiring and duplex setup follow Waveshare's official AMOLED 1.8
 * firmware/brookesia/components/bsp_extra/src/bsp_board_extra.c (Apache-2.0).
 * This adapter owns its codec/I2S resources; the shared BSP I2C bus stays alive.
 */
#include "audio_test.h"
#include <stdio.h>
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "bsp/esp-bsp.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "nvs.h"
#include "driver/i2s_std.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#define RATE 24000
#define CHUNK 480
static QueueHandle_t jobs;
static SemaphoreHandle_t lock;
static audio_test_status_t status;
static nvs_handle_t volume_store;
static bool volume_store_ready;
struct audio_session {
 i2s_chan_handle_t tx,rx;
 const audio_codec_data_if_t *data;
 const audio_codec_ctrl_if_t *ctrl;
 const audio_codec_gpio_if_t *gpio;
 const audio_codec_if_t *codec;
 esp_codec_dev_handle_t dev;
 int volume;
 bool stream_output_started;
};
static void update(bool busy,const char *text) {
 xSemaphoreTake(lock,portMAX_DELAY);status.busy=busy;strlcpy(status.message,text,sizeof(status.message));xSemaphoreGive(lock);
}
void audio_test_snapshot(audio_test_status_t *out) {xSemaphoreTake(lock,portMAX_DELAY);*out=status;xSemaphoreGive(lock);}
static void close_audio(audio_session_t *a) {
 if(a->dev){esp_codec_dev_close(a->dev);esp_codec_dev_delete(a->dev);}
 if(a->codec)audio_codec_delete_codec_if(a->codec);
 if(a->ctrl)audio_codec_delete_ctrl_if(a->ctrl);
 if(a->gpio)audio_codec_delete_gpio_if(a->gpio);
 if(a->data)audio_codec_delete_data_if(a->data);
 if(a->tx){i2s_channel_disable(a->tx);i2s_del_channel(a->tx);}
 if(a->rx){i2s_channel_disable(a->rx);i2s_del_channel(a->rx);}
 memset(a,0,sizeof(*a));
}
void audio_test_set_volume(int percent) {
 if(percent<0)percent=0;
 if(percent>100)percent=100;
 xSemaphoreTake(lock,portMAX_DELAY);status.volume=percent;xSemaphoreGive(lock);
}
void audio_test_save_volume(void) {
 if(!volume_store_ready)return;
 audio_test_status_t current;audio_test_snapshot(&current);
 esp_err_t err=nvs_set_u8(volume_store,"volume",current.volume);
 if(err==ESP_OK)err=nvs_commit(volume_store);
 if(err!=ESP_OK)ESP_LOGW("audio_test","Volume persistence failed: %s",esp_err_to_name(err));
}
static esp_err_t apply_volume(audio_session_t *a) {
 audio_test_status_t current;audio_test_snapshot(&current);
 if(a->volume==current.volume)return ESP_OK;
 if(esp_codec_dev_set_out_vol(a->dev,current.volume)!=ESP_CODEC_DEV_OK)return ESP_FAIL;
 a->volume=current.volume;return ESP_OK;
}
static esp_err_t open_audio(audio_session_t *a,bool mic) {
 esp_err_t err=bsp_i2c_init();if(err!=ESP_OK)return err;
 i2s_chan_config_t channels=I2S_CHANNEL_DEFAULT_CONFIG(CONFIG_BSP_I2S_NUM,I2S_ROLE_MASTER);channels.auto_clear=true;
 err=i2s_new_channel(&channels,&a->tx,&a->rx);if(err!=ESP_OK)return err;
 i2s_std_config_t bus={.clk_cfg=I2S_STD_CLK_DEFAULT_CONFIG(RATE),
  .slot_cfg=I2S_STD_PHILIP_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,I2S_SLOT_MODE_MONO),
  .gpio_cfg={.mclk=BSP_I2S_MCLK,.bclk=BSP_I2S_SCLK,.ws=BSP_I2S_LCLK,.dout=BSP_I2S_DOUT,.din=BSP_I2S_DSIN}};
 err=i2s_channel_init_std_mode(a->tx,&bus);if(err!=ESP_OK)return err;
 err=i2s_channel_init_std_mode(a->rx,&bus);if(err!=ESP_OK)return err;
 audio_codec_i2s_cfg_t data={.port=CONFIG_BSP_I2S_NUM,.tx_handle=a->tx,.rx_handle=a->rx};
 a->data=audio_codec_new_i2s_data(&data);a->gpio=audio_codec_new_gpio();
 audio_codec_i2c_cfg_t ctrl={.port=BSP_I2C_NUM,.addr=ES8311_CODEC_DEFAULT_ADDR,.bus_handle=bsp_i2c_get_handle()};
 a->ctrl=audio_codec_new_i2c_ctrl(&ctrl);if(!a->data||!a->gpio||!a->ctrl)return ESP_ERR_NO_MEM;
 es8311_codec_cfg_t cfg={.ctrl_if=a->ctrl,.gpio_if=a->gpio,.codec_mode=mic?ESP_CODEC_DEV_WORK_MODE_BOTH:ESP_CODEC_DEV_WORK_MODE_DAC,
  .pa_pin=BSP_POWER_AMP_IO,.master_mode=false,.use_mclk=true,.hw_gain={.pa_voltage=5.0,.codec_dac_voltage=3.3}};
 a->codec=es8311_codec_new(&cfg);if(!a->codec)return ESP_ERR_NO_MEM;
 esp_codec_dev_cfg_t dev={.dev_type=mic?ESP_CODEC_DEV_TYPE_IN_OUT:ESP_CODEC_DEV_TYPE_OUT,.codec_if=a->codec,.data_if=a->data};
 a->dev=esp_codec_dev_new(&dev);if(!a->dev)return ESP_ERR_NO_MEM;
 esp_codec_dev_sample_info_t format={.sample_rate=RATE,.channel=1,.bits_per_sample=16};
 if(esp_codec_dev_open(a->dev,&format)!=ESP_CODEC_DEV_OK)return ESP_FAIL;
 a->volume=-1;if(apply_volume(a)!=ESP_OK)return ESP_FAIL;
 if(esp_codec_dev_set_out_mute(a->dev,false)!=ESP_CODEC_DEV_OK)return ESP_FAIL;
 if(mic&&esp_codec_dev_set_in_gain(a->dev,24.0)!=ESP_CODEC_DEV_OK)return ESP_FAIL;
 return ESP_OK;
}
static esp_err_t tone(audio_session_t *a) {
 int16_t block[CHUNK];
 const int samples=RATE;
 for(int offset=0;offset<samples;offset+=CHUNK) {
  if(apply_volume(a)!=ESP_OK)return ESP_FAIL;
  for(int i=0;i<CHUNK;i++) {
   int n=offset+i;float fade=fminf(1.0f,fminf(n/240.0f,(samples-1-n)/240.0f));
   block[i]=(int16_t)(8000.0f*fade*sinf(2.0f*3.14159265f*440.0f*n/RATE));
  }
  if(esp_codec_dev_write(a->dev,block,sizeof(block))!=ESP_CODEC_DEV_OK)return ESP_FAIL;
 }
 return ESP_OK;
}
esp_err_t audio_stream_open(audio_session_t **out) {
 *out=NULL;
 xSemaphoreTake(lock,portMAX_DELAY);
 if(status.busy){xSemaphoreGive(lock);return ESP_ERR_INVALID_STATE;}
 status.busy=true;xSemaphoreGive(lock);
 audio_session_t *a=calloc(1,sizeof(*a));
 esp_err_t err=a?open_audio(a,true):ESP_ERR_NO_MEM;
 if(err!=ESP_OK){if(a){close_audio(a);free(a);}update(false,"Audio released.");return err;}
 *out=a;return ESP_OK;
}
esp_err_t audio_stream_read(audio_session_t *a,void *pcm,unsigned bytes) {
 return esp_codec_dev_read(a->dev,pcm,bytes)==ESP_CODEC_DEV_OK?ESP_OK:ESP_FAIL;
}
esp_err_t audio_stream_write(audio_session_t *a,void *pcm,unsigned bytes) {
 if(!a->stream_output_started) {
  /* Match the accepted record/playback path: explicitly unmute at playback,
   * including after a potentially long network wait with the DAC idle. */
  if(esp_codec_dev_set_out_mute(a->dev,false)!=ESP_CODEC_DEV_OK)return ESP_FAIL;
  a->volume=-1;a->stream_output_started=true;
 }
 if(apply_volume(a)!=ESP_OK)return ESP_FAIL;
 return esp_codec_dev_write(a->dev,pcm,bytes)==ESP_CODEC_DEV_OK?ESP_OK:ESP_FAIL;
}
void audio_stream_close(audio_session_t *a) {
 if(a){close_audio(a);free(a);}update(false,"Start with the speaker test.");
}
static esp_err_t microphone(audio_session_t *a) {
 const size_t samples=RATE*3;int16_t *clip=heap_caps_malloc(samples*2,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
 if(!clip)return ESP_ERR_NO_MEM;
 esp_err_t result=ESP_OK;int peak=0,clipped=0;int64_t sum=0,squares=0;
 if(esp_codec_dev_set_out_mute(a->dev,true)!=ESP_CODEC_DEV_OK){result=ESP_FAIL;goto cleanup;}
 for(size_t offset=0;offset<samples;offset+=CHUNK) {
  if(offset%RATE==0){char text[64];snprintf(text,sizeof(text),"Listening... %u seconds left",(unsigned)(3-offset/RATE));update(true,text);}
  if(esp_codec_dev_read(a->dev,clip+offset,CHUNK*2)!=ESP_CODEC_DEV_OK){result=ESP_FAIL;goto cleanup;}
  for(int i=0;i<CHUNK;i++){int64_t sample=clip[offset+i];int value=abs((int)sample);if(value>peak)peak=value;sum+=sample;squares+=sample*sample;if(value>=32760)clipped++;}
 }
 update(true,"Playing your clip back...");
 if(esp_codec_dev_set_out_mute(a->dev,false)!=ESP_CODEC_DEV_OK){result=ESP_FAIL;goto cleanup;}
 for(size_t offset=0;offset<samples;offset+=CHUNK) {
  if(apply_volume(a)!=ESP_OK || esp_codec_dev_write(a->dev,clip+offset,CHUNK*2)!=ESP_CODEC_DEV_OK){result=ESP_FAIL;break;}
 }
 ESP_LOGI("audio_test","Microphone test completed; peak=%d rms=%d dc=%d clipped=%d/%u; RAM only",peak,(int)sqrt((double)squares/samples),(int)(sum/(int64_t)samples),clipped,(unsigned)samples);
cleanup:
 volatile unsigned char *wipe=(volatile unsigned char *)clip;
 for(size_t i=0;i<samples*2;i++)wipe[i]=0;
 free(clip);return result;
}
static void worker(void *arg) {
 for(;;) {
  bool mic;if(xQueueReceive(jobs,&mic,portMAX_DELAY)!=pdTRUE)continue;
  audio_session_t a={0};update(true,"Preparing audio...");
  esp_err_t err=open_audio(&a,mic);
  if(err==ESP_OK){update(true,mic?"Listening for 3 seconds...":"Playing a short tone...");err=mic?microphone(&a):tone(&a);}
  if(err==ESP_OK)vTaskDelay(pdMS_TO_TICKS(120)); /* Drain queued speaker DMA before muting. */
  close_audio(&a);
  if(err==ESP_OK)update(false,mic?"Did you hear your voice clearly?":"Did you hear the tone?");
  else {char text[112];snprintf(text,sizeof(text),"Audio test failed: %s",esp_err_to_name(err));update(false,text);}
  ESP_LOGI("audio_test","%s test result=%s",mic?"microphone":"speaker",esp_err_to_name(err));
 }
}
void audio_test_request(bool mic) {
 xSemaphoreTake(lock,portMAX_DELAY);
 if(!status.busy){status.busy=true;strlcpy(status.message,"Preparing audio...",sizeof(status.message));if(xQueueSend(jobs,&mic,0)!=pdTRUE){status.busy=false;strlcpy(status.message,"Audio is busy. Try again.",sizeof(status.message));}}
 xSemaphoreGive(lock);
}
void audio_tests_init(void) {lock=xSemaphoreCreateMutex();jobs=xQueueCreate(1,sizeof(bool));assert(lock&&jobs);uint8_t initial=60;volume_store_ready=nvs_open("pet_audio",NVS_READWRITE,&volume_store)==ESP_OK;
 if(volume_store_ready){uint8_t saved;if(nvs_get_u8(volume_store,"volume",&saved)==ESP_OK && saved<=100)initial=saved;}
 audio_test_set_volume(initial);update(false,"Start with the speaker test.");assert(xTaskCreate(worker,"audio_test",6144,NULL,4,NULL)==pdPASS);}
