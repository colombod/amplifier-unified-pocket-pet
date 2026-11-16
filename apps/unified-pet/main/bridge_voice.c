#include "bridge_voice.h"
#include "bridge_transport.h"
#include "live_voice.h"
#include "live_protocol.h"
#include "assistant_config.h"
#include "network.h"
#include "audio_test.h"
#include <assert.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "mbedtls/base64.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "freertos/stream_buffer.h"
#include "freertos/event_groups.h"
#define BV_MIC 96000
#define BV_SPEAKER (256*1024)
#define BV_FRAME 96000
#define BV_CHUNK 960
#define BV_APPEND 4800
#define BV_PACING_JITTER_US 5000
/* Conversation mode: a continuous host stream for up to ten minutes. */
#define BV_FRAMES 32             /* queued host messages; a short network burst must not end the call */
#define BV_PREBUFFER 15360       /* 320 ms jitter buffer before (re)starting playback */
#define BV_PRIME_WAIT_US 400000  /* a short burst before a gap is played, not held for the next one */
#define BV_CATCHUP 24000         /* above 500 ms queued, inaudible chunks are skipped to shed lag */
#define BV_SPEAKER_HOLD_MS 300   /* microphone is sent as silence this long after audible output */
#define BV_NOTICE_US 4000000     /* how long a host notice replaces the status line */
#define BV_CLEANUP_US 15000000   /* never wait a whole conversation deadline for release proof */
#define BV_BEGIN_US 30000000     /* connected, but the host never began the conversation */
#define BV_LINGER_US 1500000     /* after our close, let the host close its side before the fence */
#define BV_SEND_WAITING 4800      /* a further full 100 ms frame of microphone audio is already waiting */
#define BV_SEND_CATCHUP 2         /* then the next frame may start early: at most this many times real time */
#define BV_IDLE BIT0
#define BV_CAPTURE_DONE BIT1
#define BV_PLAY_DONE BIT2
#define BV_DRAINED BIT3
/* The FIRST recorded cause names how a call ended; later consequences of the
 * same ending (socket errors after the user's own mic-off, say) never rename it. */
enum { BV_EXIT_NONE, BV_EXIT_MIC, BV_EXIT_QUEUE, BV_EXIT_GUARD,
       BV_EXIT_CALLBACK, BV_EXIT_SEND, BV_EXIT_DISCONNECTED, BV_EXIT_DEADLINE,
       BV_EXIT_USER, BV_EXIT_HOST, BV_EXIT_CONNECT, BV_EXIT_SETUP,
       BV_EXIT_AUDIO, BV_EXIT_SPEAKER, BV_EXIT_PROTOCOL, BV_EXIT_BEGIN };
/* Free callback/worker storage only after transport task-deletion proof and
 * audio workers' final atomic publications AFTER SetBits returns. */
typedef struct {
    bridge_model_t model;
    bridge_transport_t *transport;
    esp_websocket_client_handle_t ws;
    audio_session_t *audio;
    QueueHandle_t frames;
    StreamBufferHandle_t capture,playback;
    StaticStreamBuffer_t capture_meta,playback_meta;
    EventGroupHandle_t events;
    EventBits_t audio_tasks;
    atomic_bool active,accepting,connected,failed,stop,capture_gate,play_gate,drain;
    atomic_bool capture_exited,play_exited;
    atomic_uint command,confirm_revision,audio_epoch,exit_reason;
    /* Conversation mode. speaker_until is a millisecond stamp, as live_voice.c:
     * only AUDIBLE output mutes the microphone, never packet arrival. */
    atomic_bool converse;
    atomic_uint speaker_until;
    int64_t notice_until;
    unsigned actions,received_pcm;
    int64_t hard,work,start;
    bool (*parent_stopped)(void);
    bool disposal_started,retained;
    uint8_t *capture_memory,*playback_memory,*append_pcm,*decoded;
    char *assembly_memory,*append_json;
    live_message_t assembly;
} bridge_voice_t;
static SemaphoreHandle_t guard;
static bridge_voice_t *current;
static bridge_view_t terminal_view;
static bool terminal_valid;
static portMUX_TYPE completion_mux=portMUX_INITIALIZER_UNLOCKED;
static atomic_bool restart_required;
static bool take(void){return guard&&xSemaphoreTake(guard,pdMS_TO_TICKS(100))==pdTRUE;}
static void give(void){xSemaphoreGive(guard);}
static void quiet(bridge_voice_t *c){
    atomic_store(&c->capture_gate,false);atomic_store(&c->play_gate,false);
    atomic_fetch_add(&c->audio_epoch,1);audio_stream_inhibit();
}
static void note_exit(bridge_voice_t *c,unsigned reason){
    unsigned empty=BV_EXIT_NONE;
    atomic_compare_exchange_strong(&c->exit_reason,&empty,reason);
}
static bool stopping(bridge_voice_t *c){
    if(c->parent_stopped&&c->parent_stopped()){
        /* The parent stops for the user's tap, or at its own session deadline. */
        note_exit(c,esp_timer_get_time()<c->work?BV_EXIT_USER:BV_EXIT_DEADLINE);
        atomic_store(&c->stop,true);
    }
    return atomic_load(&c->stop);
}
static const char *exit_status(bridge_voice_t *c){
    switch(atomic_load(&c->exit_reason)){
    case BV_EXIT_MIC:return "Microphone read failed.";
    case BV_EXIT_QUEUE:return "Microphone stream interrupted.";
    case BV_EXIT_GUARD:return "Voice connection interrupted.";
    case BV_EXIT_CALLBACK:return "Host connection interrupted.";
    case BV_EXIT_SEND:return "Voice send failed. No replay.";
    case BV_EXIT_DISCONNECTED:return "Host disconnected.";
    case BV_EXIT_DEADLINE:return "Voice time limit reached.";
    case BV_EXIT_USER:return "Stopped. Unified work is not cancelled.";
    case BV_EXIT_CONNECT:return "Could not reach the host.";
    case BV_EXIT_SETUP:return "Could not start the host connection.";
    case BV_EXIT_AUDIO:return "Microphone or speaker unavailable.";
    case BV_EXIT_SPEAKER:return "Speaker playback failed.";
    case BV_EXIT_PROTOCOL:return "Host sent an invalid message.";
    case BV_EXIT_BEGIN:return "Host did not start the call.";
    default:return atomic_load(&c->stop)?"Stopped. Unified work is not cancelled.":"Voice disconnected.";
    }
}
static void callback_failed(bridge_voice_t *c){
    note_exit(c,BV_EXIT_CALLBACK);atomic_store(&c->failed,true);
}
static void failure(bridge_voice_t *c,const char *code){
    if(!strcmp(code,"overflow"))note_exit(c,BV_EXIT_QUEUE);
    else if(!strcmp(code,"deadline"))note_exit(c,BV_EXIT_DEADLINE);
    atomic_store(&c->failed,true);quiet(c);
    if(take()){
        if(c->model.confirmed&&c->model.send==BR_SEND_NONE)c->model.send=BR_SEND_UNKNOWN;
        if(c->model.send==BR_SEND_ACCEPTED&&c->model.reply==BR_REPLY_NONE)c->model.reply=BR_REPLY_UNAVAILABLE;
        if(c->model.draining)c->model.reply=BR_REPLY_UNAVAILABLE;
        /* A conversation's code is only ever the HOST's stated error; a local
         * failure is named by its exit reason and must not overwrite that. */
        if(!c->model.converse)snprintf(c->model.code,sizeof(c->model.code),"%s",code);
        c->model.phase=BR_CLOSING;c->model.draining=false;
        setup_bridge_wipe(c->model.text,sizeof(c->model.text));setup_bridge_wipe(c->model.display,sizeof(c->model.display));give();
    }
}
static bool speaker_active(bridge_voice_t *c){
    return (int32_t)(atomic_load(&c->speaker_until)-(uint32_t)(esp_timer_get_time()/1000))>0;
}
static void capture_task(void *arg){
    bridge_voice_t *c=arg;uint8_t pcm[BV_CHUNK];unsigned discard=0;bool was_enabled=false;
    while(atomic_load(&c->active)&&!atomic_load(&c->stop)&&!atomic_load(&c->failed)){
        /* Admission/idle handoff shares the model lock. A read admitted before
         * Finish may block; idle is set only AFTER that read has returned. */
        if(!take()){note_exit(c,BV_EXIT_GUARD);failure(c,"reply_unavailable");break;}
        bool enabled=atomic_load(&c->capture_gate);
        if(!enabled){xEventGroupSetBits(c->events,BV_IDLE);give();was_enabled=false;vTaskDelay(pdMS_TO_TICKS(10));continue;}
        xEventGroupClearBits(c->events,BV_IDLE);give();
        if(!was_enabled){discard=9600;was_enabled=true;} /* first 4800 samples of stale DMA */
        esp_err_t e=audio_stream_read(c->audio,pcm,sizeof(pcm));
        if(e!=ESP_OK){note_exit(c,BV_EXIT_MIC);failure(c,"reply_unavailable");break;}
        if(!atomic_load(&c->capture_gate)||atomic_load(&c->stop))continue;
        if(discard){discard-=sizeof(pcm);continue;}
        /* A conversation keeps streaming while the pet speaks, but never sends
         * its own audible speech back as the user's words (as live_voice.c). */
        if(atomic_load(&c->converse)&&speaker_active(c))memset(pcm,0,sizeof(pcm));
        if(xStreamBufferSend(c->capture,pcm,sizeof(pcm),0)!=sizeof(pcm)){failure(c,"overflow");break;}
    }
    setup_bridge_wipe(pcm,sizeof(pcm));xEventGroupSetBits(c->events,BV_IDLE|BV_CAPTURE_DONE);
    portENTER_CRITICAL(&completion_mux);
    atomic_store_explicit(&c->capture_exited,true,memory_order_release);
    portEXIT_CRITICAL(&completion_mux); /* no c access after unlock */
    vTaskDelete(NULL);
}
static void play_task(void *arg){
    bridge_voice_t *c=arg;uint8_t pcm[BV_CHUNK];unsigned epoch=atomic_load(&c->audio_epoch);bool primed=false;
    bool converse=atomic_load(&c->converse);int64_t waiting=0;
    while(atomic_load(&c->active)){
        unsigned next=atomic_load(&c->audio_epoch);
        if(!atomic_load(&c->play_gate)||atomic_load(&c->stop)||atomic_load(&c->failed)){
            while(xStreamBufferReceive(c->playback,pcm,sizeof(pcm),0)){}
            epoch=next;primed=false;waiting=0;vTaskDelay(pdMS_TO_TICKS(5));continue;
        }
        /* One reply per connection; every accepted buffer byte belongs to the
         * newly opened reply epoch. Opening that epoch must not discard bytes
         * already queued before the playback worker observes reply.started. */
        if(next!=epoch){epoch=next;primed=false;}
        size_t available=xStreamBufferBytesAvailable(c->playback);
        bool draining=atomic_load(&c->drain);
        if(!available){
            if(draining)xEventGroupSetBits(c->events,BV_DRAINED);
            /* Conversation audio may be continuous or gappy. After a gap the
             * jitter buffer is rebuilt; a gap is never the end of the call. */
            if(converse){primed=false;waiting=0;}
            vTaskDelay(pdMS_TO_TICKS(5));continue;
        }
        /* Finite-tail drain bypasses the old 320ms prebuffer. A packet gap never
         * seals the queue or declares completed speech. */
        if(!primed&&available<BV_PREBUFFER&&!draining){
            int64_t now=esp_timer_get_time();
            if(!waiting)waiting=now;
            if(!converse||now-waiting<BV_PRIME_WAIT_US){vTaskDelay(pdMS_TO_TICKS(5));continue;}
        }
        primed=true;waiting=0;
        size_t n=xStreamBufferReceive(c->playback,pcm,sizeof(pcm),0);
        if(!n)continue;
        if(epoch!=atomic_load(&c->audio_epoch)||!atomic_load(&c->play_gate)||atomic_load(&c->stop))continue;
        if(converse){
            if(live_pcm_audible(pcm,n))
                atomic_store(&c->speaker_until,(uint32_t)(esp_timer_get_time()/1000)+BV_SPEAKER_HOLD_MS);
            /* Only digital silence is ever skipped, and only while behind. */
            else if(xStreamBufferBytesAvailable(c->playback)>BV_CATCHUP)continue;
        }
        if(audio_stream_write(c->audio,pcm,n)!=ESP_OK){note_exit(c,BV_EXIT_SPEAKER);failure(c,"reply_unavailable");break;}
    }
    setup_bridge_wipe(pcm,sizeof(pcm));xEventGroupSetBits(c->events,BV_PLAY_DONE);
    portENTER_CRITICAL(&completion_mux);
    atomic_store_explicit(&c->play_exited,true,memory_order_release);
    portEXIT_CRITICAL(&completion_mux); /* no c access after unlock */
    vTaskDelete(NULL);
}
static void ws_event(void *arg,esp_event_base_t base,int32_t id,void *data){
    (void)base;bridge_voice_t *c=arg;esp_websocket_event_data_t *e=data;
    if(!atomic_load(&c->accepting))return;
    if(id==WEBSOCKET_EVENT_CONNECTED){
        if(!bridge_transport_upgraded(c->transport)){callback_failed(c);return;}
        atomic_store(&c->connected,true);
    }else if(id==WEBSOCKET_EVENT_DISCONNECTED||id==WEBSOCKET_EVENT_CLOSED)atomic_store(&c->connected,false);
    else if(id==WEBSOCKET_EVENT_ERROR)callback_failed(c);
    else if(id==WEBSOCKET_EVENT_DATA){
        if(!e||e->data_len<0||e->payload_len<0||e->payload_offset<0){callback_failed(c);return;}
        int complete=live_message_feed(&c->assembly,c->assembly_memory,BV_FRAME,e->op_code,e->fin,
            e->payload_offset,e->payload_len,e->data_ptr,e->data_len);
        if(complete<0){callback_failed(c);return;}
        if(complete==1){
            /* Queue strings only after rejecting embedded raw NUL. Otherwise
             * owner-side strlen could accept a valid prefix and hide a suffix. */
            if(memchr(c->assembly_memory,0,c->assembly.used)){callback_failed(c);return;}
            char *message=heap_caps_malloc(c->assembly.used+1,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
            if(!message){callback_failed(c);return;}
            memcpy(message,c->assembly_memory,c->assembly.used);message[c->assembly.used]=0;
            if(xQueueSend(c->frames,&message,0)!=pdTRUE){setup_bridge_wipe(message,c->assembly.used);free(message);callback_failed(c);}
        }
    }
}
static bool transport_lost(bridge_voice_t *c){
    /* DATA callbacks precede DISCONNECTED on the vendor event loop. Consume
     * their already-queued messages before handling clean transport EOF. */
    return !atomic_load(&c->connected)&&!atomic_load(&c->drain)&&uxQueueMessagesWaiting(c->frames)==0;
}
static bool send_frame_timed(bridge_voice_t *c,const char *json,int64_t *send_started){
    bool closing=!strcmp(json,"{\"type\":\"pet.stop\"}")||!strcmp(json,"{\"type\":\"session.close\"}");
    size_t n=strlen(json);
    if(!atomic_load(&c->connected)||bridge_transport_finished(c->transport)||!bridge_transport_upgraded(c->transport))return false;
    if(!take())return false;
    /* Stop and the NEW send admission share this critical section. An already
     * admitted send may settle, never replay; Stop also inhibits socket writes.
     * Check the work fence after encoding and lock acquisition, not only at the
     * owner-loop head. Closing traffic retains the separate hard-bound budget. */
    bool admitted=closing||(!stopping(c)&&!atomic_load(&c->failed));
    int64_t started=esp_timer_get_time();
    admitted=admitted&&(closing||started<c->work);
    int64_t left=c->hard-started;
    unsigned ms=left>0?(unsigned)(left/1000):0;if(ms>1000)ms=1000;
    give();
    if(!admitted||!ms)return false;
    /* No model lock spans blocking I/O. This stamp is the final local send
     * admission boundary, not a claim of peer receipt or TLS transfer timing. */
    if(esp_websocket_client_send_text(c->ws,json,n,pdMS_TO_TICKS(ms))!=(int)n)return false;
    if(send_started)*send_started=started;
    return true;
}
static bool send_frame(bridge_voice_t *c,const char *json){
    return send_frame_timed(c,json,NULL);
}
static bool send_action(bridge_voice_t *c,unsigned action){
    char json[256];if(!take())return false;
    int n=bridge_encode(&c->model,action,json,sizeof(json));give();
    return n>0&&send_frame(c,json);
}
static void pace_sent_audio(int64_t *next_audio,int64_t send_started,size_t n){
    /* Keep the running sample timeline across the owner's 5ms scheduling waits,
     * rather than accumulating wake-up delay on every packet. Credit at most
     * one such wait; a long stall cannot buy an unbounded catch-up burst.
     * Synchronous send time is part of the interval, not an additional wait. */
    int64_t anchor=*next_audio;
    int64_t floor=send_started-BV_PACING_JITTER_US;
    if(!anchor)anchor=send_started;
    else if(anchor<floor)anchor=floor;
    *next_audio=anchor+(int64_t)n*1000000/48000;
}
static bool append_audio(bridge_voice_t *c,size_t n,int64_t *next_audio){
    if(!next_audio||n==0||n>BV_APPEND||n%2)return false;
    size_t count=0;const char prefix[]="{\"type\":\"session.input_audio.append\",\"audio\":\"";
    memcpy(c->append_json,prefix,sizeof(prefix)-1);
    int e=mbedtls_base64_encode((unsigned char*)c->append_json+sizeof(prefix)-1,7000-sizeof(prefix)-3,&count,c->append_pcm,n);
    if(e)return false;
    memcpy(c->append_json+sizeof(prefix)-1+count,"\"}",3);
    int64_t admitted_at=0;
    if(!send_frame_timed(c,c->append_json,&admitted_at))return false;
    pace_sent_audio(next_audio,admitted_at,n);
    /* The pacing above never sends faster than real time and forgives only 5 ms
     * of lateness per frame, so on a loop that is routinely later than that the
     * microphone falls steadily behind until its buffer overflows (every build
     * 10 call ended itself that way after 31-35 s). A conversation therefore
     * catches up: while a further full frame is already waiting, the next one
     * may start after 1/BV_SEND_CATCHUP of this frame's duration, measured from
     * this frame's own start, so lateness cannot accumulate. Send starts are
     * thus never closer than that: over any span the host receives at most
     * BV_SEND_CATCHUP times real time, and exactly real time once nothing is
     * waiting. Every byte is still sent once, in order, in the same frames. */
    if(atomic_load(&c->converse)&&xStreamBufferBytesAvailable(c->capture)>=BV_SEND_WAITING){
        int64_t early=admitted_at+(int64_t)n*1000000/48000/BV_SEND_CATCHUP;
        if(early<*next_audio)*next_audio=early;
    }
    return true;
}
static bool finish_capture(bridge_voice_t *c,int64_t *next_audio){
    if(!take()){failure(c,"reply_unavailable");return false;}
    atomic_store(&c->capture_gate,false);give();
    int64_t left=c->work-esp_timer_get_time();
    unsigned wait_ms=left>0?(unsigned)(left/1000):0;if(wait_ms>500)wait_ms=500;
    EventBits_t bits=xEventGroupWaitBits(c->events,BV_IDLE,pdFALSE,pdTRUE,pdMS_TO_TICKS(wait_ms));
    if(!(bits&BV_IDLE)){failure(c,"reply_unavailable");return false;}
    while(xStreamBufferBytesAvailable(c->capture)&&!stopping(c)&&!atomic_load(&c->failed)&&esp_timer_get_time()<c->work){
        size_t n=xStreamBufferReceive(c->capture,c->append_pcm,BV_APPEND,0);
        while(esp_timer_get_time()<*next_audio&&!stopping(c)&&!atomic_load(&c->failed)&&esp_timer_get_time()<c->work)vTaskDelay(pdMS_TO_TICKS(5));
        if(esp_timer_get_time()>=c->work||stopping(c)||atomic_load(&c->failed)){failure(c,"deadline");return false;}
        if(!append_audio(c,n,next_audio)){failure(c,"reply_unavailable");return false;}
    }
    /* Finish waits for the shared sample timeline. Relative to the last local
     * admission, that timeline permits at most the 5ms pacing jitter credit. */
    while(esp_timer_get_time()<*next_audio&&!stopping(c)&&!atomic_load(&c->failed)&&esp_timer_get_time()<c->work)
        vTaskDelay(pdMS_TO_TICKS(5));
    if(atomic_load(&c->failed)||stopping(c)||esp_timer_get_time()>=c->work||
       xStreamBufferBytesAvailable(c->capture)||!send_action(c,BR_ACT_FINISH_CAPTURE)){
        failure(c,"reply_unavailable");return false;
    }
    return true;
}
static void finalize_model(bridge_voice_t *c){
    if(!take()){atomic_store(&restart_required,true);return;}
    /* Provider EOS is not speaker completion. Cleanup before the drain
     * acknowledgement must not turn a cut-off reply into "Reply finished". */
    if(c->model.draining){
        c->model.reply=atomic_load(&c->stop)?BR_REPLY_STOPPED:BR_REPLY_UNAVAILABLE;
        c->model.draining=false;
    }
    if(atomic_load(&c->stop))bridge_command(&c->model,BR_CMD_STOP,0);
    if(c->model.confirmed&&c->model.send==BR_SEND_NONE)c->model.send=BR_SEND_UNKNOWN;
    if(c->model.send==BR_SEND_ACCEPTED&&c->model.reply==BR_REPLY_NONE)c->model.reply=BR_REPLY_UNAVAILABLE;
    give();
}
static void publish(bridge_voice_t *c){
    bridge_view_t view;
    if(!take())return;
    if(c->model.notice_visible&&esp_timer_get_time()>=c->notice_until)bridge_notice_expire(&c->model);
    bridge_view(&c->model,&view);give();
    live_state_t state=view.phase==BR_CAPTURING?LIVE_LISTENING:view.phase==BR_REPLYING?LIVE_SPEAKING:
        view.phase==BR_CONNECTING||view.phase==BR_BEGINNING?LIVE_CONNECTING:view.phase>=BR_CLOSING?LIVE_CLOSING:LIVE_THINKING;
    if(view.phase==BR_CONVERSING)
        state=view.talk==BR_TALK_ASKING?LIVE_THINKING:view.talk==BR_TALK_ANSWERING?LIVE_SPEAKING:LIVE_LISTENING;
    live_voice_bridge_status(state,view.status); /* only fixed metadata, NOT target/transcript */
}
void bridge_voice_init(void){guard=xSemaphoreCreateMutex();assert(guard);}
bool bridge_voice_restart_required(void){return atomic_load(&restart_required);}
void bridge_voice_stop(void){
    if(!take())return;
    bridge_voice_t *c=current;
    if(c&&atomic_load(&c->active)){
        note_exit(c,BV_EXIT_USER);
        atomic_store(&c->stop,true);atomic_store(&c->command,0);
        if(c->model.draining)c->model.reply=BR_REPLY_STOPPED;
        bridge_command(&c->model,BR_CMD_STOP,0);quiet(c);
        /* One-turn Stop fences the socket at once. In a conversation the
         * microphone gate and amplifier are already off here and no new audio
         * is admitted; its owner sees this latch within one bounded step, says
         * pet.stop/session.close, then cancels. Unified work is not cancelled. */
        if(!c->model.converse)bridge_transport_cancel(c->transport);
    }
    give();
}
void bridge_voice_finish_capture(void){if(!take())return;if(current&&atomic_load(&current->active)){atomic_store(&current->capture_gate,false);atomic_store(&current->command,1);}give();}
void bridge_voice_confirm(uint32_t revision){if(!take())return;if(current&&atomic_load(&current->active)){atomic_store(&current->confirm_revision,revision);atomic_store(&current->command,2);}give();}
bool bridge_voice_snapshot(bridge_view_t *view){
    if(!view||!take())return false;
    bool exists=current!=NULL||terminal_valid;
    if(current)bridge_view(&current->model,view);
    else if(terminal_valid)*view=terminal_view;
    give();return exists;
}
bool bridge_voice_copy_preview(char *out,size_t cap,uint32_t *revision){
    if(!out||!cap||!revision||!take())return false;
    bool ok=current&&current->model.phase==BR_PREVIEW&&strlen(current->model.display)<cap;
    if(ok){memcpy(out,current->model.display,strlen(current->model.display)+1);*revision=current->model.revision;}else{out[0]=0;*revision=0;}give();return ok;
}
static bool audio_exited(const bridge_voice_t *c){
    portENTER_CRITICAL(&completion_mux);
    bool exited=(!(c->audio_tasks&BV_CAPTURE_DONE)||atomic_load_explicit(&c->capture_exited,memory_order_acquire))&&
                (!(c->audio_tasks&BV_PLAY_DONE)||atomic_load_explicit(&c->play_exited,memory_order_acquire));
    portEXIT_CRITICAL(&completion_mux);
    return exited;
}
/* Guard held. One honest line for how a conversation ended, from its FIRST
 * cause; returns whether that is shown as an error. A normal mic-off, the
 * host's own close and either time limit are ordinary ends, not errors. */
static bool converse_end(bridge_voice_t *c,char *out,size_t cap){
    unsigned reason=atomic_load(&c->exit_reason);
    const char *s=NULL;bool error=true;
    /* What the host itself said about the ending (its error, its quiet/time
     * limit or its close) is the status whenever the host or the connection
     * ended the call, even if the socket then failed before the last frames
     * were handled in order. bridge_view already holds that wording. */
    bool host_said=c->model.code[0]||c->model.end_notice!=BR_NOTICE_NONE||c->model.wire_closed;
    bool link=reason==BV_EXIT_HOST||reason==BV_EXIT_DISCONNECTED||reason==BV_EXIT_CALLBACK||reason==BV_EXIT_SEND;
    if(reason==BV_EXIT_USER){s="Mic off";error=false;}
    else if(link&&host_said)error=c->model.code[0]!=0;
    else if(reason==BV_EXIT_DEADLINE){s="Mic off (time limit)";error=false;}
    else s=exit_status(c);
    if(s)snprintf(out,cap,"%s",s);
    return error;
}
/* The call is over with host frames still queued (the socket failed, or the
 * loop ended first). Read them only for the host's stated ending: no audio is
 * played, nothing is sent and nothing here can start or repeat a request. */
static void converse_tail(bridge_voice_t *c){
    char *message=NULL;
    while(xQueueReceive(c->frames,&message,0)==pdTRUE){
        bridge_event_t event;
        if(bridge_parse(message,strlen(message),&event)){
            if(event.kind!=BR_EV_AUDIO&&take()){(void)bridge_step(&c->model,&event);give();}
            bridge_event_clear(&event);
        }
        setup_bridge_wipe(message,strlen(message));free(message);
    }
}
static void cleanup(bridge_voice_t *c,bool wait){
    quiet(c);
    if(wait&&atomic_load(&c->converse)){
        if(c->frames)converse_tail(c);
        live_voice_bridge_status(LIVE_CLOSING,"Mic off"); /* audio is already off; the release follows */
    }
    finalize_model(c);
    if(!c->disposal_started&&c->transport&&bridge_transport_started(c->transport)){
        if(atomic_load(&c->connected)&&esp_timer_get_time()<c->hard){
            /* Close the pet only, never cancel or replay Unified input. */
            (void)send_frame(c,"{\"type\":\"pet.stop\"}");
            bool closed=esp_timer_get_time()<c->hard&&send_frame(c,"{\"type\":\"session.close\"}");
            if(closed&&atomic_load(&c->converse)){
                /* A conversation's socket is still open here. Give the host a
                 * bounded moment to close its side, so our stop is not lost to
                 * an abrupt reset. The fence below follows either way. */
                int64_t linger=esp_timer_get_time()+BV_LINGER_US;
                if(linger>c->hard)linger=c->hard;
                while(atomic_load(&c->connected)&&!bridge_transport_finished(c->transport)&&esp_timer_get_time()<linger)
                    vTaskDelay(pdMS_TO_TICKS(10));
            }
        }
    }
    if(c->transport)bridge_transport_cancel(c->transport);
    atomic_store(&c->active,false);atomic_store(&c->accepting,false);
    c->disposal_started=true; /* even a later proof check must never use client APIs */
    int64_t until=wait?c->hard:esp_timer_get_time();
    /* A conversation deadline is minutes away. Stopping earlier only retains
     * unproven storage sooner; the zero-wait reaper still releases it later. */
    if(wait&&until-esp_timer_get_time()>BV_CLEANUP_US)until=esp_timer_get_time()+BV_CLEANUP_US;
    int64_t audio_until=esp_timer_get_time()+500000;
    if(audio_until>until)audio_until=until;
    bool audio_settled;
    for(;;){
        /* SetBits can still access the group AFTER waking a waiter. These
         * flags are the workers' last context access after SetBits returns. */
        audio_settled=audio_exited(c);
        if(audio_settled||esp_timer_get_time()>=audio_until)break;
        vTaskDelay(1);
    }
    if(!audio_settled)atomic_store(&restart_required,true);
    /* No client API use after this call, including on false. Proof waits use
     * the original hard deadline, never now+another cleanup allowance. */
    bool transport_settled=bridge_transport_dispose(c->transport,until);
    if(transport_settled){c->transport=NULL;c->ws=NULL;}
    else atomic_store(&restart_required,true);
    if(!audio_settled||!transport_settled){
        c->retained=true;
        if(take()){
            setup_bridge_wipe(c->model.text,sizeof(c->model.text));setup_bridge_wipe(c->model.display,sizeof(c->model.display));give();
        }
        live_voice_bridge_status(LIVE_CLOSING,"Releasing voice connection...");
        return; /* exceptional unproven ownership: never timeout-and-free */
    }
    if(c->audio){audio_stream_close(c->audio);c->audio=NULL;}
    if(!take()){
        c->retained=true;
        atomic_store(&restart_required,true);live_voice_bridge_status(LIVE_ERROR,"Voice cleanup incomplete. Connection held.");return;
    }
    /* A prior guard timeout must not leave possibly-sent input looking unsent. */
    if(c->model.confirmed&&c->model.send==BR_SEND_NONE)c->model.send=BR_SEND_UNKNOWN;
    if(c->model.send==BR_SEND_ACCEPTED&&c->model.reply==BR_REPLY_NONE)c->model.reply=BR_REPLY_UNAVAILABLE;
    if(c->model.draining){c->model.reply=atomic_load(&c->stop)?BR_REPLY_STOPPED:BR_REPLY_UNAVAILABLE;c->model.draining=false;}
    bridge_view(&c->model,&terminal_view);
    terminal_view.phase=BR_FINISHED;
    terminal_view.can_finish_capture=false;terminal_view.can_send=false;terminal_view.can_stop=false;
    if(!c->model.converse&&terminal_view.send!=BR_SEND_UNKNOWN&&terminal_view.reply!=BR_REPLY_UNAVAILABLE&&
       terminal_view.reply!=BR_REPLY_STOPPED&&terminal_view.send!=BR_SEND_REJECTED&&c->model.code[0]==0){
        snprintf(terminal_view.status,sizeof(terminal_view.status),"%s",
            terminal_view.reply==BR_REPLY_RECEIVED?"Reply finished.":exit_status(c));
    }
    bool converse_error=c->model.converse&&converse_end(c,terminal_view.status,sizeof(terminal_view.status));
    terminal_valid=true;
    bridge_view_t final=terminal_view;
    bool final_error=c->model.converse?converse_error:
        (atomic_load(&c->failed)||final.send==BR_SEND_UNKNOWN||final.reply==BR_REPLY_UNAVAILABLE);
    if(current==c)current=NULL;
    atomic_store(&restart_required,false);
    give(); /* unpublish before freeing anything the UI could read */
    if(c->capture)vStreamBufferDelete(c->capture);
    if(c->playback)vStreamBufferDelete(c->playback);
    if(c->frames){
        char *pending=NULL;
        while(xQueueReceive(c->frames,&pending,0)==pdTRUE){
            setup_bridge_wipe(pending,strlen(pending));free(pending);
        }
        vQueueDelete(c->frames);
    }
    if(c->events)vEventGroupDelete(c->events);
    if(c->capture_memory){setup_bridge_wipe(c->capture_memory,BV_MIC+1);free(c->capture_memory);}
    if(c->playback_memory){setup_bridge_wipe(c->playback_memory,BV_SPEAKER+1);free(c->playback_memory);}
    if(c->assembly_memory){setup_bridge_wipe(c->assembly_memory,BV_FRAME);free(c->assembly_memory);}
    if(c->append_pcm){setup_bridge_wipe(c->append_pcm,BV_APPEND);free(c->append_pcm);}
    if(c->append_json){setup_bridge_wipe(c->append_json,7000);free(c->append_json);}
    if(c->decoded){setup_bridge_wipe(c->decoded,BV_APPEND);free(c->decoded);}
    setup_bridge_wipe(c,sizeof(*c));free(c);
    live_voice_bridge_status(final_error?LIVE_ERROR:LIVE_OFF,final.status);
}
void bridge_voice_reap_finished(void){
    /* Called only after the outer owner worker is suspended. A late task/timer
     * completion can release held storage without reboot or a new connection.
     * This is a zero-wait proof check, not a retry of audio, input or client I/O. */
    if(!take())return;
    bridge_voice_t *c=current;
    bool ready=c&&c->retained&&!atomic_load(&c->active)&&audio_exited(c);
    give();
    if(ready)cleanup(c,false);
}
void bridge_voice_run(const setup_bridge_t *cfg,int64_t deadline,bool (*parent_stopped)(void)){
    if(atomic_load(&restart_required)){live_voice_bridge_status(LIVE_ERROR,"Voice cleanup incomplete. Connection held.");return;}
    bridge_voice_t *c=heap_caps_calloc(1,sizeof(*c),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if(!c){live_voice_bridge_status(LIVE_ERROR,"Bridge memory unavailable.");return;}
    c->hard=deadline;c->work=deadline-2000000;c->start=esp_timer_get_time();
    c->parent_stopped=parent_stopped;
    /* The pet's one voice control is a running conversation: mic on listens and
     * talks back, mic off ends the call. The one-turn preview/Send model stays
     * in this file for its lifecycle tests but is not started from here. */
    atomic_store(&c->converse,true);atomic_store(&c->speaker_until,(uint32_t)(c->start/1000));
    bridge_init(&c->model);bridge_command(&c->model,BR_CMD_CONVERSE,0);
    c->frames=xQueueCreate(BV_FRAMES,sizeof(char*));c->events=xEventGroupCreate();
    c->capture_memory=heap_caps_malloc(BV_MIC+1,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->playback_memory=heap_caps_malloc(BV_SPEAKER+1,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->assembly_memory=heap_caps_malloc(BV_FRAME,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->append_pcm=heap_caps_malloc(BV_APPEND,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->append_json=heap_caps_malloc(7000,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->decoded=heap_caps_malloc(BV_APPEND,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if(!c->frames||!c->events||!c->capture_memory||!c->playback_memory||!c->assembly_memory||!c->append_pcm||!c->append_json||!c->decoded)goto setup_failed;
    c->capture=xStreamBufferCreateStatic(BV_MIC+1,1,c->capture_memory,&c->capture_meta);
    c->playback=xStreamBufferCreateStatic(BV_SPEAKER+1,1,c->playback_memory,&c->playback_meta);
    if(!c->capture||!c->playback)goto setup_failed;
    char token[44]={0};
    esp_err_t loaded=network_copy_bridge_token(token,sizeof(token));
    if(loaded==ESP_OK)c->transport=bridge_transport_new(cfg,token,c->hard);
    setup_bridge_wipe(token,sizeof(token));if(!c->transport)goto setup_failed;
    c->ws=bridge_transport_client(c->transport);
    if(!take())goto setup_failed;
    current=c;terminal_valid=false;setup_bridge_wipe(&terminal_view,sizeof(terminal_view));give();
    if(stopping(c))goto cleanup;
    atomic_store(&c->active,true);atomic_store(&c->accepting,true);
    if(esp_websocket_register_events(c->ws,WEBSOCKET_EVENT_ANY,ws_event,c)!=ESP_OK)goto setup_failed;
    if(bridge_transport_start(c->transport)!=ESP_OK)goto setup_failed;
    publish(c);
    int64_t startup=c->start+25000000;
    if(startup>c->work)startup=c->work;
    while(!atomic_load(&c->connected)&&!atomic_load(&c->failed)&&!bridge_transport_finished(c->transport)&&!stopping(c)&&esp_timer_get_time()<startup)vTaskDelay(pdMS_TO_TICKS(10));
    if(!atomic_load(&c->connected)||stopping(c)){
        /* Before the first connection every failure is "could not reach". */
        if(!stopping(c)){atomic_store(&c->exit_reason,BV_EXIT_CONNECT);atomic_store(&c->failed,true);}
        goto cleanup;
    }
    if(!send_frame(c,PET_LIVE_START)){ /* host discards device reasoning fields */
        if(!stopping(c)){note_exit(c,BV_EXIT_SEND);atomic_store(&c->failed,true);}
        goto cleanup;
    }
    int64_t next_audio=0,begin_by=esp_timer_get_time()+BV_BEGIN_US;
    while(esp_timer_get_time()<c->work&&!stopping(c)&&!atomic_load(&c->failed)){
        char *message=NULL;
        if(xQueueReceive(c->frames,&message,pdMS_TO_TICKS(5))==pdTRUE){
            bridge_event_t event;
            if(!bridge_parse(message,strlen(message),&event)){setup_bridge_wipe(message,strlen(message));free(message);note_exit(c,BV_EXIT_PROTOCOL);failure(c,"invalid_preview");break;}
            if(!take()){note_exit(c,BV_EXIT_GUARD);bridge_event_clear(&event);setup_bridge_wipe(message,strlen(message));free(message);break;}
            unsigned a=bridge_step(&c->model,&event);
            if(c->model.reply==BR_REPLY_RECEIVED&&c->received_pcm==0){
                c->model.reply=BR_REPLY_UNAVAILABLE;c->model.draining=false;
                a=BR_ACT_MUTE|BR_ACT_CLOSE;
            }
            c->actions|=a;
            if(a&BR_ACT_CLOSE)note_exit(c,BV_EXIT_HOST); /* the host's error or close ended it */
            if(a&BR_ACT_NOTICE)c->notice_until=esp_timer_get_time()+BV_NOTICE_US;
            if(a&BR_ACT_MUTE)quiet(c);
            if(event.kind==BR_EV_REPLY_STARTED&&c->model.phase==BR_REPLYING&&!atomic_load(&c->stop)&&!atomic_load(&c->failed)){atomic_store(&c->play_gate,true);atomic_store(&c->audio_epoch,c->model.audio_epoch);}
            if(a&BR_ACT_DRAIN)atomic_store(&c->drain,true);
            if(a&BR_ACT_PLAY){size_t bytes=0;int e=mbedtls_base64_decode(c->decoded,BV_APPEND,&bytes,(const unsigned char*)event.audio,strlen(event.audio));
                if(e||!atomic_load(&c->play_gate)||atomic_load(&c->stop)||xStreamBufferSend(c->playback,c->decoded,bytes,0)!=bytes){note_exit(c,BV_EXIT_SPEAKER);atomic_store(&c->failed,true);}
                else c->received_pcm+=(unsigned)bytes;}
            give();bridge_event_clear(&event);setup_bridge_wipe(message,strlen(message));free(message);
        }
        if(transport_lost(c)){note_exit(c,BV_EXIT_DISCONNECTED);failure(c,"reply_unavailable");break;}
        unsigned command=atomic_exchange(&c->command,0);
        if(!take()){note_exit(c,BV_EXIT_GUARD);break;}
        if(!atomic_load(&c->stop)){
            if(command==1)c->actions|=bridge_command(&c->model,BR_CMD_FINISH_CAPTURE,0);
            else if(command==2)c->actions|=bridge_command(&c->model,BR_CMD_CONFIRM,atomic_load(&c->confirm_revision));
        }
        unsigned actions=c->actions;c->actions=0;bridge_phase_t phase=c->model.phase;give();
        if((phase==BR_CONNECTING||phase==BR_BEGINNING)&&esp_timer_get_time()>=begin_by){note_exit(c,BV_EXIT_BEGIN);failure(c,"reply_unavailable");break;}
        if(actions&BR_ACT_BEGIN){if(!send_action(c,BR_ACT_BEGIN)){note_exit(c,BV_EXIT_SEND);failure(c,"reply_unavailable");break;}}
        if(actions&BR_ACT_CAPTURE){
            if(stopping(c))break;
            if(audio_stream_open(&c->audio)!=ESP_OK){note_exit(c,BV_EXIT_AUDIO);failure(c,"reply_unavailable");break;}
            if(atomic_load(&c->stop)){quiet(c);break;}
            if(xTaskCreate(capture_task,"pet_bridge_mic",4096,c,5,NULL)!=pdPASS){note_exit(c,BV_EXIT_AUDIO);failure(c,"reply_unavailable");break;}
            c->audio_tasks|=BV_CAPTURE_DONE;
            if(xTaskCreate(play_task,"pet_bridge_out",4096,c,5,NULL)!=pdPASS){note_exit(c,BV_EXIT_AUDIO);failure(c,"reply_unavailable");break;}
            c->audio_tasks|=BV_PLAY_DONE;atomic_store(&c->capture_gate,true);
            /* A conversation plays host audio whenever it arrives, from the start. */
            if(phase==BR_CONVERSING&&!atomic_load(&c->stop)&&!atomic_load(&c->failed))atomic_store(&c->play_gate,true);
        }
        if((actions&BR_ACT_FINISH_CAPTURE)&&!finish_capture(c,&next_audio))break;
        if(actions&BR_ACT_CONFIRM){if(!send_action(c,BR_ACT_CONFIRM)){failure(c,"reply_unavailable");break;}}
        if(actions&BR_ACT_CLOSE)break;
        if(atomic_load(&c->drain)&&(xEventGroupGetBits(c->events)&BV_DRAINED)){
            if(take()){c->actions|=bridge_playback_drained(&c->model);give();}break;
        }
        if((phase==BR_CAPTURING||phase==BR_CONVERSING)&&esp_timer_get_time()>=next_audio){
            size_t n=xStreamBufferReceive(c->capture,c->append_pcm,BV_APPEND,0);
            /* Refused at the local time limit is the time limit, not a send failure. */
            if(n){if(!append_audio(c,n,&next_audio)){note_exit(c,esp_timer_get_time()>=c->work?BV_EXIT_DEADLINE:BV_EXIT_SEND);failure(c,"reply_unavailable");break;}}
        }
        publish(c);
    }
    if(esp_timer_get_time()>=c->work)failure(c,"deadline");
    goto cleanup;
setup_failed:
    note_exit(c,BV_EXIT_SETUP);atomic_store(&c->failed,true);
cleanup:
    cleanup(c,true);
}
