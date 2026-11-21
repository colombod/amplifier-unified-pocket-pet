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
#if defined(PET_PRIVATE_MIC_DIAG)
#include "driver/usb_serial_jtag.h"
#include "esp_wifi.h"
#endif
#define BV_MIC 96000
#define BV_SPEAKER (256*1024)
#define BV_FRAME 96000
#define BV_CHUNK 960
#define BV_APPEND 4800
#define BV_PACING_JITTER_US 5000
/* Conversation mode: a continuous host stream for up to ten minutes. */
/* The microphone and speaker tasks only move 20 ms chunks between the codec and
 * the two buffers (about 1% of a core each), but the codec's DMA holds just
 * 60 ms: a task that is scheduled later than that leaves a hole in the sound
 * (crackling) and loses microphone samples. At the socket reader's priority (5)
 * on core 0 that happened in 7 of 8 bench calls: 10-15 seconds per 25 s call
 * with holes, writes up to 117 ms late, microphone at 95%. Above the voice owner
 * (6), on the core the owner is not on, no write in 19 calls was later than
 * 28 ms; what gaps remained (2 calls) were network underruns of the playback
 * buffer (working-files/voice-bench runs 15-17, 20 November 2026). */
#define BV_AUDIO_PRIORITY 7
#define BV_AUDIO_CORE 0
#define BV_FRAMES 32             /* queued host messages; a short network burst must not end the call */
#define BV_PREBUFFER 15360       /* 320 ms jitter buffer before (re)starting playback */
#define BV_PRIME_WAIT_US 400000  /* a short burst before a gap is played, not held for the next one */
#define BV_CATCHUP 24000         /* above 500 ms queued, inaudible chunks are skipped to shed lag */
#define BV_SPEAKER_HOLD_MS 300   /* microphone is sent as silence this long after audible output */
#define BV_NOTICE_US 4000000     /* how long a host notice replaces the status line */
#define BV_CLEANUP_US 15000000   /* never wait a whole conversation deadline for release proof */
#define BV_BEGIN_US 30000000     /* connected, but the host never began the conversation */
#define BV_LINGER_US 1500000     /* after our close, let the host close its side before the fence */
#define BV_SEND_MS 1000          /* how long one frame may wait for the socket to take it */
#define BV_STALL_MS 5000         /* a conversation's frame may wait this long for a stalled link before the call is given up */
#define BV_FRAME_WAIT_MS 3000    /* a conversation's reader waits this long for the owner before a full message queue ends the call */
#define BV_SEND_WAITING 4800      /* a further full 100 ms frame of microphone audio is already waiting */
#define BV_SEND_CATCHUP 2         /* then the next frame may start early: at most this many times real time */
/* Reconnecting while the mic is on (conversation mode only). After a call ends
 * for a connection reason the next attempt waits BV_RETRY_DELAY_S[failures].
 * Six attempts over 82 s of waiting plus connect time: a host restart of up to
 * about 70 s that refuses connections at once is still outlasted, and each
 * attempt lands just after a likely recovery (the host's 15 s slot release,
 * then 30 s, 50 s and 80 s restarts). When attempts fail slowly instead (no
 * answer at all, up to 25 s each), BV_RETRY_WINDOW_US ends it sooner. */
#define BV_RETRIES 6             /* attempts in a row that did not give a healthy call, then give up */
#define BV_HEALTHY_US 30000000   /* a call that conversed this long resets that count */
#define BV_RECONNECT_MAX 20      /* reconnect attempts per mic-on, whatever happens: a flapping link ends */
#define BV_RETRY_WINDOW_US 120000000 /* no attempt starts later than this after the connection was lost */
#define BV_RETRY_POLL_MS 20      /* how often a waiting retry looks for mic off */
static const unsigned char BV_RETRY_DELAY_S[BV_RETRIES]={2,5,10,15,20,30};
#define BV_IDLE BIT0
#define BV_CAPTURE_DONE BIT1
#define BV_PLAY_DONE BIT2
#define BV_DRAINED BIT3
/* The FIRST recorded cause names how a call ended; later consequences of the
 * same ending (socket errors after the user's own mic-off, say) never rename it. */
enum { BV_EXIT_NONE, BV_EXIT_MIC, BV_EXIT_QUEUE, BV_EXIT_GUARD,
       BV_EXIT_CALLBACK, BV_EXIT_SEND, BV_EXIT_DISCONNECTED, BV_EXIT_DEADLINE,
       BV_EXIT_USER, BV_EXIT_HOST, BV_EXIT_CONNECT, BV_EXIT_SETUP,
       BV_EXIT_AUDIO, BV_EXIT_SPEAKER, BV_EXIT_PROTOCOL, BV_EXIT_BEGIN, BV_EXIT_RETRY_WINDOW };
/* How a conversation call ended: an ordinary end, an error that another call
 * cannot fix, or a lost connection that another call may. */
enum { BV_END_OFF, BV_END_ERROR, BV_END_RETRY };
#if defined(PET_PRIVATE_MIC_DIAG)
#define BV_DIAG_BINS 32u
#define BV_DIAG_BIN_US 1000000
/* Fixed window relative to capture-task creation, not wall clock. One writer
 * per array; cleanup may read them only after the capture last-access fence.
 * No audio, absolute times, identifiers, logging or synchronization here. */
typedef struct {
    unsigned read,read_errors,read_max_us,startup,gated,admitted,queue_peak;
} bv_diag_capture_t;
typedef struct {
    unsigned popped,completed,send_count,send_us_max,frame_count,frame_us_max;
    uint64_t send_us_total,frame_us_total;
    unsigned loop_us_max,frames_waiting_peak,playback_peak;
} bv_diag_owner_t;
typedef struct {unsigned play_bytes,play_skipped,play_write_us_max;} bv_diag_play_t;
/* Whole-call counters, independent of the older 32-second bin window.
 * Exactly one writer per group: play task / owner task respectively. */
typedef struct {
    unsigned underruns,total_ms,max_ms,hist[6];
    uint32_t opened_ms;
    bool open;
} bv_diag_gaps_t;
typedef struct {
    unsigned count,max_ms,hist[6];
    int64_t last;
} bv_diag_arrivals_t;
#define BV_CALL_PREBUFFER(c,conv) ((conv)?(c)->prebuffer_ms*48u:BV_PREBUFFER)
#define BV_CALL_PRIME_WAIT(c,conv) ((conv)?((c)->prebuffer_ms+80u)*1000u:BV_PRIME_WAIT_US)
#define BV_DIAG_TASKS 96u
static int bv_diag_bin(int64_t start,int64_t stamp,bool *clipped){
    int64_t age=stamp-start;
    if(age<0)return -1;
    if(age>=BV_DIAG_BINS*(int64_t)BV_DIAG_BIN_US){*clipped=true;return -1;}
    return (int)(age/BV_DIAG_BIN_US);
}
#else
#define BV_CALL_PREBUFFER(c,conv) BV_PREBUFFER
#define BV_CALL_PRIME_WAIT(c,conv) BV_PRIME_WAIT_US
#endif
typedef struct {char *text;int64_t received_at;bool after_local_control;} bv_frame_t;
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
    atomic_bool active,accepting,connected,connected_seen,failed,stop,muted,capture_gate,play_gate,drain,invalid_protocol,ingress_incomplete;
    atomic_bool capture_exited,play_exited;
    atomic_uint command,confirm_revision,audio_epoch,exit_reason;
    /* Conversation mode. speaker_until is a millisecond stamp, as live_voice.c:
     * only AUDIBLE output mutes the microphone, never packet arrival. */
    atomic_bool converse;
    atomic_uint speaker_until;
    int64_t notice_until;
    /* Reconnecting. failures/reconnects/reached are this mic-on's counts when
     * the call started; began_at is when this call started conversing. */
    unsigned failures,reconnects;
    bool reached;
    int http_status;
    bridge_transport_terminal_t transport_terminal;
    char terminal_code[32]; /* first definite host error; owner only under guard */
    bool send_uncertain_seen; /* no wire event proves its resolution during this call */
    bool send_pending; /* observed asking, not yet followed by ordered answering; per call */
    bool cleanup_stop_sent,cleanup_close_unconfirmed; /* owner only; unconfirmed close vetoes retry */
    int64_t cleanup_stop_started_at; /* valid only after a full successful pet.stop */
    bool tail_incomplete;
    bool assembly_after_local_control,peer_closed_before_control; /* receipt ordering only, never cause */
    int64_t assembly_received_at;
    int64_t began_at;
    int64_t ended_at;bool ended_recorded; /* completion_mux; may predate began_at */
    unsigned actions,received_pcm;
    int64_t hard,work,start;
    bool (*parent_stopped)(void);
    bool disposal_started,retained;
    uint8_t *capture_memory,*playback_memory,*append_pcm,*decoded;
    char *assembly_memory,*append_json;
    live_message_t assembly;
#if defined(PET_PRIVATE_MIC_DIAG)
    int64_t mic_start;
    bool mic_started,mic_clipped,owner_clipped,play_clipped;
    bv_diag_capture_t mic_bins[BV_DIAG_BINS];
    bv_diag_owner_t owner_bins[BV_DIAG_BINS];
    bv_diag_play_t play_bins[BV_DIAG_BINS];
    int64_t loop_start,tasks_start;
    TaskStatus_t *task_scratch;
    pet_bench_task_t *tasks_before,*tasks_after;
    unsigned tasks_before_n;
    pet_bench_task_t mic_task,play_task;
    unsigned prebuffer_ms;
    bv_diag_gaps_t gaps;
    bv_diag_arrivals_t arrivals;
    /* End stamp + 1 (0 = not ended), modulo 32-bit milliseconds. Published
     * under the existing completion lock; no 64-bit atomic or new hot lock.
     * The play task alone closes gaps, including an open terminal gap. */
    atomic_uint diag_end_ms;
    unsigned rssi_start,rssi_min;
    int64_t wifi_next;
#endif
} bridge_voice_t;
static SemaphoreHandle_t guard;
static bridge_voice_t *current;
static bridge_view_t terminal_view;
static bool terminal_valid;
static portMUX_TYPE completion_mux=portMUX_INITIALIZER_UNLOCKED;
static atomic_bool restart_required;
/* Written by the owner's cleanup of a conversation call, read by the same
 * thread's reconnect loop: whether another call follows, and the counts. */
static struct { bool again,reached; unsigned failures; int64_t ended_at; } call_report;
#if defined(PET_PRIVATE_MIC_DIAG)
static atomic_uint bench_prebuffer_ms=320;
/* Bench only: the reports of the last BV_DIAG_KEEP calls stay in memory, so a
 * call made with nobody reading the console can be read out afterwards
 * ("voice last <n>"). Counts and timings only, exactly the console report. */
#define BV_DIAG_KEEP 3u
#define BV_DIAG_KEEP_BYTES 12288u
static SemaphoreHandle_t bv_keep_lock;
static char *bv_keep_text[BV_DIAG_KEEP],*bv_keep_cur;
static unsigned bv_keep_len[BV_DIAG_KEEP],bv_keep_start_ms[BV_DIAG_KEEP],bv_keep_count,*bv_keep_cur_len;
static bool bv_keep_usb;
/* Bench only: priority and core of the next call's two audio tasks.
 * Consumed once at call entry; the normal defaults apply again afterwards. */
static atomic_uint bench_audio_priority=BV_AUDIO_PRIORITY;
static atomic_int bench_audio_core=BV_AUDIO_CORE;
void bridge_voice_bench_audio(unsigned priority,int core){
    atomic_store(&bench_audio_priority,priority<5?5:priority>7?7:priority);
    atomic_store(&bench_audio_core,core<0?-1:core?1:0);
}
bool bridge_voice_bench_jitter(unsigned ms){
    if(ms!=320&&ms!=480&&ms!=640&&ms!=800&&ms!=960&&ms!=1280)return false;
    atomic_store(&bench_prebuffer_ms,ms);return true;
}
static void bv_diag_gap_close(bridge_voice_t *c){
    bv_diag_gaps_t *g=&c->gaps;
    if(!g->open)return;
    uint32_t end=atomic_load(&c->diag_end_ms);
    uint32_t stamp=end?end-1:(uint32_t)(esp_timer_get_time()/1000);
    int32_t delta=(int32_t)(stamp-g->opened_ms);
    unsigned ms=delta>0?(unsigned)delta:0;
    g->total_ms+=ms;if(ms>g->max_ms)g->max_ms=ms;
    unsigned i=ms<100?0:ms<200?1:ms<400?2:ms<800?3:ms<1600?4:5;
    g->hist[i]++;g->open=false;
}
static void bv_diag_arrival(bridge_voice_t *c){
    bv_diag_arrivals_t *a=&c->arrivals;
    int64_t now=esp_timer_get_time();
    if(a->count){
        unsigned ms=(unsigned)((now-a->last)/1000);
        if(ms>a->max_ms)a->max_ms=ms;
        unsigned i=ms<150?0:ms<250?1:ms<400?2:ms<800?3:ms<1600?4:5;
        a->hist[i]++;
    }
    a->count++;a->last=now;
}
/* Only RSSI is retained, never the AP's identifiers. 0 means unavailable.
 * Magnitude grows as signal weakens, so the dBm minimum is the largest value. */
static unsigned bv_diag_rssi(void){
    int rssi=0;
    return esp_wifi_sta_get_rssi(&rssi)==ESP_OK&&rssi<0?(unsigned)-rssi:0;
}
static void bv_diag_wifi_sample(bridge_voice_t *c,int64_t now){
    if(!c->mic_started||now<c->wifi_next)return;
    unsigned rssi=bv_diag_rssi();
    if(!c->wifi_next)c->rssi_start=rssi;
    if(rssi>c->rssi_min)c->rssi_min=rssi;
    c->wifi_next=now+1000000; /* no catch-up queries after a stalled owner */
}
static inline bv_diag_owner_t *bv_diag_owner_bin(bridge_voice_t *c){
    if(!c->mic_started)return NULL;
    int i=bv_diag_bin(c->mic_start,esp_timer_get_time(),&c->owner_clipped);
    return i<0?NULL:&c->owner_bins[i];
}
static void bv_diag_peaks(bridge_voice_t *c,bv_diag_owner_t *b){
    unsigned n=uxQueueMessagesWaiting(c->frames);
    if(n>b->frames_waiting_peak)b->frames_waiting_peak=n;
    n=xStreamBufferBytesAvailable(c->playback);
    if(n>b->playback_peak)b->playback_peak=n;
}
static void bv_diag_loop(bridge_voice_t *c){
    int64_t now=esp_timer_get_time();
    bv_diag_wifi_sample(c,now);
    bv_diag_owner_t *b=bv_diag_owner_bin(c);
    if(b){
        if(c->loop_start&&(uint64_t)(now-c->loop_start)>b->loop_us_max)b->loop_us_max=now-c->loop_start;
        bv_diag_peaks(c,b);
    }
    c->loop_start=now;
}
static void bv_diag_frame(bridge_voice_t *c,int64_t begin){
    bv_diag_owner_t *b=bv_diag_owner_bin(c);
    if(b){
        uint64_t total=b->frame_us_total,max=b->frame_us_max;
        pet_bench_time(&total,&max,esp_timer_get_time()-begin);
        b->frame_us_total=total;b->frame_us_max=max;b->frame_count++;
        bv_diag_peaks(c,b);
    }
}
static unsigned bv_diag_snapshot(bridge_voice_t *c,pet_bench_task_t *out){
    if(!c->task_scratch||!out)return 0;
    UBaseType_t n=uxTaskGetSystemState(c->task_scratch,BV_DIAG_TASKS,NULL);
    for(unsigned i=0;i<n;i++)pet_bench_task_copy(&out[i],&c->task_scratch[i]);
    return n;
}
#endif
static bool take(void){return guard&&xSemaphoreTake(guard,pdMS_TO_TICKS(100))==pdTRUE;}
static void give(void){xSemaphoreGive(guard);}
static void quiet(bridge_voice_t *c){
    atomic_store(&c->capture_gate,false);atomic_store(&c->play_gate,false);
    atomic_fetch_add(&c->audio_epoch,1);audio_stream_inhibit();
}
static void health_end(bridge_voice_t *c,int64_t at){
    portENTER_CRITICAL(&completion_mux);
    if(!c->ended_recorded||at<c->ended_at){c->ended_at=at;c->ended_recorded=true;}
#if defined(PET_PRIVATE_MIC_DIAG)
    atomic_store(&c->diag_end_ms,(uint32_t)(c->ended_at/1000)+1u);
#endif
    portEXIT_CRITICAL(&completion_mux);
}
static void note_exit(bridge_voice_t *c,unsigned reason){
    health_end(c,esp_timer_get_time());
    unsigned empty=BV_EXIT_NONE;
    atomic_compare_exchange_strong(&c->exit_reason,&empty,reason);
}
static bool stopping(bridge_voice_t *c){
    if(c->parent_stopped&&c->parent_stopped()){
        /* The parent stops for the user's tap, or at its own session deadline. */
        bool user=esp_timer_get_time()<c->work;
        if(user)atomic_store(&c->muted,true);
        note_exit(c,user?BV_EXIT_USER:BV_EXIT_DEADLINE);
        atomic_store(&c->stop,true);
    }
    return atomic_load(&c->stop);
}
static bool transport_stopping(void *arg){return stopping(arg);}
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
    case BV_EXIT_RETRY_WINDOW:return c->reached?"Connection lost - mic off":"Could not reach the host - mic off";
    default:return atomic_load(&c->stop)?"Stopped. Unified work is not cancelled.":"Voice disconnected.";
    }
}
static void callback_failed(bridge_voice_t *c){
    note_exit(c,BV_EXIT_CALLBACK);atomic_store(&c->failed,true);
}
/* The host sent something that is not a message of this protocol. Unlike a
 * lost connection, calling again cannot fix that. */
static void protocol_failed(bridge_voice_t *c){
    atomic_store(&c->invalid_protocol,true);note_exit(c,BV_EXIT_PROTOCOL);atomic_store(&c->failed,true);
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
        /* Stop and opening the capture gate share the model guard. Check the
         * parent latch here too, even if its bridge Stop is waiting for it. */
        if(stopping(c)||atomic_load(&c->failed)||esp_timer_get_time()>=c->work){give();break;}
        bool enabled=atomic_load(&c->capture_gate);
        if(!enabled){xEventGroupSetBits(c->events,BV_IDLE);give();was_enabled=false;vTaskDelay(pdMS_TO_TICKS(10));continue;}
        xEventGroupClearBits(c->events,BV_IDLE);give();
        if(stopping(c)||atomic_load(&c->failed)||esp_timer_get_time()>=c->work)break;
        if(!was_enabled){discard=9600;was_enabled=true;} /* first 4800 samples of stale DMA */
#if defined(PET_PRIVATE_MIC_DIAG)
        int64_t read_begin=esp_timer_get_time();
#endif
        esp_err_t e=audio_stream_read(c->audio,pcm,sizeof(pcm));
#if defined(PET_PRIVATE_MIC_DIAG)
        int64_t read_end=esp_timer_get_time();
        int i=bv_diag_bin(c->mic_start,read_end,&c->mic_clipped);
        if(i>=0){
            bv_diag_capture_t *b=&c->mic_bins[i];
            unsigned us=(unsigned)(read_end-read_begin);
            if(us>b->read_max_us)b->read_max_us=us;
            /* The codec wrapper reports status, not a lower-layer byte count. */
            if(e==ESP_OK)b->read+=sizeof(pcm);
            else b->read_errors++;
        }
#endif
        if(e!=ESP_OK){note_exit(c,BV_EXIT_MIC);failure(c,"reply_unavailable");break;}
        if(!atomic_load(&c->capture_gate)||atomic_load(&c->stop)){
#if defined(PET_PRIVATE_MIC_DIAG)
            if(i>=0)c->mic_bins[i].gated+=sizeof(pcm);
#endif
            continue;
        }
        if(discard){
#if defined(PET_PRIVATE_MIC_DIAG)
            if(i>=0)c->mic_bins[i].startup+=sizeof(pcm);
#endif
            discard-=sizeof(pcm);continue;
        }
        /* A conversation keeps streaming while the pet speaks, but never sends
         * its own audible speech back as the user's words (as live_voice.c). */
        if(atomic_load(&c->converse)&&speaker_active(c))memset(pcm,0,sizeof(pcm));
        /* A conversation outlives a stalled link. One lost packet can hold the
         * socket for seconds until TCP sends it again; the buffer then fills.
         * The newest audio is dropped while it is full and the call continues
         * with a gap. What is already buffered is still sent once, in order;
         * whole chunks only, so samples stay aligned. */
        if(atomic_load(&c->converse)&&xStreamBufferSpacesAvailable(c->capture)<sizeof(pcm))continue;
        size_t admitted=xStreamBufferSend(c->capture,pcm,sizeof(pcm),0);
#if defined(PET_PRIVATE_MIC_DIAG)
        i=bv_diag_bin(c->mic_start,esp_timer_get_time(),&c->mic_clipped);
        if(i>=0){
            bv_diag_capture_t *b=&c->mic_bins[i];
            b->admitted+=admitted; /* include a partial enqueue on overflow */
            /* Only the consumer may advance between enqueue and this sample.
             * This is a sampled peak, not an exact historical high-water. */
            unsigned depth=(unsigned)xStreamBufferBytesAvailable(c->capture);
            if(depth>b->queue_peak)b->queue_peak=depth;
        }
#endif
        if(admitted!=sizeof(pcm)){failure(c,"overflow");break;}
    }
#if defined(PET_PRIVATE_MIC_DIAG)
    pet_bench_task_final(&c->mic_task);
#endif
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
        if((stopping(c)||esp_timer_get_time()>=c->work)&&
           (atomic_load(&c->capture_gate)||atomic_load(&c->play_gate)))quiet(c);
        unsigned next=atomic_load(&c->audio_epoch);
        if(!atomic_load(&c->play_gate)||atomic_load(&c->stop)||atomic_load(&c->failed)){
#if defined(PET_PRIVATE_MIC_DIAG)
            bv_diag_gap_close(c);
#endif
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
            if(converse){
#if defined(PET_PRIVATE_MIC_DIAG)
                if(primed&&!c->gaps.open&&!atomic_load(&c->diag_end_ms)){
                    c->gaps.underruns++;c->gaps.open=true;
                    c->gaps.opened_ms=(uint32_t)(esp_timer_get_time()/1000);
                }
#endif
                primed=false;waiting=0;
            }
            vTaskDelay(pdMS_TO_TICKS(5));continue;
        }
        /* Finite-tail drain bypasses the old 320ms prebuffer. A packet gap never
         * seals the queue or declares completed speech. */
        if(!primed&&available<BV_CALL_PREBUFFER(c,converse)&&!draining){
            int64_t now=esp_timer_get_time();
            if(!waiting)waiting=now;
            if(!converse||now-waiting<BV_CALL_PRIME_WAIT(c,converse)){vTaskDelay(pdMS_TO_TICKS(5));continue;}
        }
        primed=true;waiting=0;
        size_t n=xStreamBufferReceive(c->playback,pcm,sizeof(pcm),0);
        if(!n)continue;
        if(epoch!=atomic_load(&c->audio_epoch)||!atomic_load(&c->play_gate)||stopping(c)||
           atomic_load(&c->failed)||esp_timer_get_time()>=c->work)continue;
        if(converse){
            if(live_pcm_audible(pcm,n))
                atomic_store(&c->speaker_until,(uint32_t)(esp_timer_get_time()/1000)+BV_SPEAKER_HOLD_MS);
            /* Only digital silence is ever skipped, and only while behind. */
            else if(xStreamBufferBytesAvailable(c->playback)>BV_CATCHUP){
#if defined(PET_PRIVATE_MIC_DIAG)
                int i=bv_diag_bin(c->mic_start,esp_timer_get_time(),&c->play_clipped);
                if(i>=0)c->play_bins[i].play_skipped+=n;
#endif
                continue;
            }
        }
#if defined(PET_PRIVATE_MIC_DIAG)
        bv_diag_gap_close(c); /* skipped silence above does not resume playback */
        int64_t write_begin=esp_timer_get_time();
        esp_err_t write_result=audio_stream_write(c->audio,pcm,n);
        int i=bv_diag_bin(c->mic_start,esp_timer_get_time(),&c->play_clipped);
        if(i>=0){
            bv_diag_play_t *b=&c->play_bins[i];b->play_bytes+=n;
            unsigned us=esp_timer_get_time()-write_begin;
            if(us>b->play_write_us_max)b->play_write_us_max=us;
        }
        if(write_result!=ESP_OK){note_exit(c,BV_EXIT_SPEAKER);failure(c,"reply_unavailable");break;}
#else
        if(audio_stream_write(c->audio,pcm,n)!=ESP_OK){note_exit(c,BV_EXIT_SPEAKER);failure(c,"reply_unavailable");break;}
#endif
    }
#if defined(PET_PRIVATE_MIC_DIAG)
    bv_diag_gap_close(c);
    pet_bench_task_final(&c->play_task);
#endif
    setup_bridge_wipe(pcm,sizeof(pcm));xEventGroupSetBits(c->events,BV_PLAY_DONE);
    portENTER_CRITICAL(&completion_mux);
    atomic_store_explicit(&c->play_exited,true,memory_order_release);
    portEXIT_CRITICAL(&completion_mux); /* no c access after unlock */
    vTaskDelete(NULL);
}
static void ws_event(void *arg,esp_event_base_t base,int32_t id,void *data){
    (void)base;bridge_voice_t *c=arg;esp_websocket_event_data_t *e=data;
    /* The ticket records first parser ingress, not the potentially delayed
     * callback time. The first text chunk fixes origin through continuations. */
    bool after_local_control=false;
    int64_t received_at=0;
    if(id==WEBSOCKET_EVENT_DATA&&!bridge_transport_take_data(c->transport,&after_local_control,&received_at)){
        atomic_store(&c->ingress_incomplete,true);callback_failed(c);return;
    }
    if(!atomic_load(&c->accepting))return;
    if(id==WEBSOCKET_EVENT_CONNECTED){
        if(!bridge_transport_upgraded(c->transport)){callback_failed(c);return;}
        atomic_store(&c->connected_seen,true);atomic_store(&c->connected,true);
    }else if(id==WEBSOCKET_EVENT_DISCONNECTED||id==WEBSOCKET_EVENT_CLOSED){
        int64_t at=bridge_transport_end_at(c->transport);
        health_end(c,at?at:esp_timer_get_time());atomic_store(&c->connected,false);
    }
    else if(id==WEBSOCKET_EVENT_ERROR){
        int64_t at=bridge_transport_end_at(c->transport);
        if(at)health_end(c,at);
        callback_failed(c);
    }
    else if(id==WEBSOCKET_EVENT_DATA){
        if(!e||e->data_len<0||e->payload_len<0||e->payload_offset<0){protocol_failed(c);return;}
        /* The transport wrapper only delivers complete validated control
         * frames. A peer CLOSE ends active conversation at receipt, before the
         * SDK may spend time acknowledging it or waiting for TCP close. */
        if(e->op_code==8&&e->fin&&e->payload_offset==0&&e->data_len==e->payload_len)
            health_end(c,received_at);
        /* Preserve admission at the first piece of a message, including across
         * continuations and interleaved controls; queue consumption can be later. */
        if(e->op_code==1&&e->payload_offset==0&&!c->assembly.active){
            c->assembly_after_local_control=after_local_control;
            c->assembly_received_at=received_at;
        }
        int complete=live_message_feed(&c->assembly,c->assembly_memory,BV_FRAME,e->op_code,e->fin,
            e->payload_offset,e->payload_len,e->data_ptr,e->data_len);
        if(complete<0){protocol_failed(c);return;}
        if(complete==1){
            /* Queue strings only after rejecting embedded raw NUL. Otherwise
             * owner-side strlen could accept a valid prefix and hide a suffix. */
            if(memchr(c->assembly_memory,0,c->assembly.used)){protocol_failed(c);return;}
            char *message=heap_caps_malloc(c->assembly.used+1,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
            if(!message){atomic_store(&c->ingress_incomplete,true);callback_failed(c);return;}
            memcpy(message,c->assembly_memory,c->assembly.used);message[c->assembly.used]=0;
            bv_frame_t frame={.text=message,.received_at=c->assembly_received_at,.after_local_control=c->assembly_after_local_control};
            /* After a stalled link the host's backlog arrives faster than the
             * owner plays it. A conversation's reader then waits here, which
             * holds the socket back, instead of ending the call at once. Mic
             * off, a failure or the end of the call end the wait. */
            bool queued=xQueueSend(c->frames,&frame,0)==pdTRUE;
            int64_t until=esp_timer_get_time()+(int64_t)BV_FRAME_WAIT_MS*1000;
            /* No guard spans a queue wait. Cleanup retains frames, assembly
             * and c until this transport task's final-access proof. The parent
             * latch matters even if the UI's bridge Stop missed the guard. */
            while(!queued&&atomic_load(&c->converse)&&esp_timer_get_time()<until&&
                    atomic_load(&c->active)&&!stopping(c)&&!atomic_load(&c->failed))
                queued=xQueueSend(c->frames,&frame,pdMS_TO_TICKS(10))==pdTRUE;
            if(!queued){
                atomic_store(&c->ingress_incomplete,true);setup_bridge_wipe(message,c->assembly.used);free(message);callback_failed(c);
            }
        }
    }
}
static bool transport_lost(bridge_voice_t *c){
    /* DATA callbacks precede DISCONNECTED on the vendor event loop. Consume
     * their already-queued messages before handling clean transport EOF. */
    return !atomic_load(&c->connected)&&!atomic_load(&c->drain)&&uxQueueMessagesWaiting(c->frames)==0;
}
static bool send_frame_timed(bridge_voice_t *c,const char *json,unsigned limit_ms,int64_t *send_started){
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
    unsigned ms=left>0?(unsigned)(left/1000):0;if(ms>limit_ms)ms=limit_ms;
    give();
    if(!admitted||!ms)return false;
    /* No model lock spans blocking I/O. This stamp is the final local send
     * admission boundary, not a claim of peer receipt or TLS transfer timing. */
    int64_t send_until=started+(int64_t)ms*1000;
    if(closing&&!bridge_transport_admit_control(c->transport,send_until))return false;
    int64_t remaining=send_until-esp_timer_get_time();
    ms=remaining>0?(unsigned)(remaining/1000):0;
    if(!ms){
        /* Admission was published but no send can now occur. A close seen
         * after that boundary cannot safely be called a cleanup response. */
        if(closing)atomic_store(&c->ingress_incomplete,true);
        return false;
    }
    if(esp_websocket_client_send_text(c->ws,json,n,pdMS_TO_TICKS(ms))!=(int)n)return false;
    if(send_started)*send_started=started;
    return true;
}
static bool send_frame(bridge_voice_t *c,const char *json){
    return send_frame_timed(c,json,BV_SEND_MS,NULL);
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
    /* The SDK drops the connection when a send times out, and one lost packet
     * can hold the socket for seconds until TCP sends it again. A
     * conversation's frame may therefore wait BV_STALL_MS; the microphone
     * buffer drops its newest audio meanwhile (capture_task). Nothing is sent
     * twice; a longer stall ends this call as a lost connection. */
    unsigned limit=atomic_load(&c->converse)?BV_STALL_MS:BV_SEND_MS;
    int64_t admitted_at=0;
#if defined(PET_PRIVATE_MIC_DIAG)
    int64_t send_begin=esp_timer_get_time();
    bool sent=send_frame_timed(c,c->append_json,limit,&admitted_at);
    bv_diag_owner_t *b=bv_diag_owner_bin(c);
    if(b){
        uint64_t max=b->send_us_max;
        pet_bench_time(&b->send_us_total,&max,esp_timer_get_time()-send_begin);
        b->send_us_max=max;b->send_count++;
    }
    if(!sent)return false;
    if(b)b->completed+=n; /* raw PCM bytes in fully returned SDK sends, not peer receipts */
#else
    if(!send_frame_timed(c,c->append_json,limit,&admitted_at))return false;
#endif
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
#if defined(PET_PRIVATE_MIC_DIAG)
        bv_diag_owner_t *b=n?bv_diag_owner_bin(c):NULL;
        if(b)b->popped+=n;
#endif
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
    /* A call being re-established keeps the mic-on look until the host answers. */
    if(c->reconnects&&state==LIVE_CONNECTING){live_voice_bridge_status(LIVE_RECONNECTING,"Reconnecting...");return;}
    live_voice_bridge_status(state,view.status); /* only fixed metadata, NOT target/transcript */
}
void bridge_voice_init(void){
    guard=xSemaphoreCreateMutex();assert(guard);
#if defined(PET_PRIVATE_MIC_DIAG)
    bv_keep_lock=xSemaphoreCreateMutex();
#endif
}
bool bridge_voice_restart_required(void){return atomic_load(&restart_required);}
void bridge_voice_stop(void){
    /* This latch is independent of the model guard. If a contended guard
     * times out, the amplifier must still be inhibited immediately. */
    audio_stream_inhibit();
    if(!take())return;
    bridge_voice_t *c=current;
    if(c)atomic_store(&c->muted,true);
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
/* Guard held; parsed frames from this call's owner or cleanup tails only.
 * The gateway admits confirm before enqueueing asking. An observed asking
 * therefore fences replacement until a later ordered answering, even when
 * the frame is consumed after local failure/closing. Listening, unknown state
 * and cleanup do not settle it. Answering never clears sticky uncertainty.
 * This cannot detect an asking frame that never reached the device. */
static void observe_converse_send(bridge_voice_t *c,const bridge_event_t *event){
    if(!c->model.converse)return;
    if(event->kind==BR_EV_CONVERSE_NOTICE&&!strcmp(event->code,"send_uncertain"))
        c->send_uncertain_seen=true;
    if(event->kind==BR_EV_CONVERSE_STATE){
        if(!strcmp(event->state,"asking"))c->send_pending=true;
        else if(c->send_pending&&!strcmp(event->state,"answering"))c->send_pending=false;
    }
}
/* Guard held. Combine the first local exit with all settled closing evidence.
 * Receipt order relative to our cleanup cannot establish what caused the peer
 * to end. Any valid session.closed, complete WS1000 or unconfirmed close vetoes
 * automatic replacement; only a loss without that evidence may be retried. */
static unsigned converse_end(bridge_voice_t *c,char *out,size_t cap){
    unsigned reason=atomic_load(&c->exit_reason);
    /* What the host itself said about the ending (its quiet/time limit, its
     * error or its close) decides whenever the host or the connection ended
     * the call, even if the socket then failed before the last frames were
     * handled in order. bridge_view already holds the wording for its error. */
    bool link=reason==BV_EXIT_HOST||reason==BV_EXIT_DISCONNECTED||reason==BV_EXIT_CALLBACK||reason==BV_EXIT_SEND;
    const char *s=NULL;unsigned end=BV_END_ERROR;
    if(reason==BV_EXIT_USER||atomic_load(&c->muted)){s="Mic off";end=BV_END_OFF;}
    else if(atomic_load(&c->invalid_protocol))s="Host sent an invalid message.";
    else if(c->transport_terminal==BR_TRANSPORT_CERT_VERIFY)s="Host certificate verification failed.";
    else if(c->transport_terminal==BR_TRANSPORT_TLS_UNKNOWN)s="Secure connection failed.";
    else if(c->transport_terminal==BR_TRANSPORT_PROTOCOL)s="Voice connection protocol error.";
    else if(c->transport_terminal==BR_TRANSPORT_POLICY)s="Host refused the voice connection.";
    else if(c->transport_terminal==BR_TRANSPORT_UNSUPPORTED)s="Host rejected the voice format.";
    else if(c->transport_terminal==BR_TRANSPORT_PAYLOAD_REJECTED)s="Host rejected the voice payload.";
    else if(c->transport_terminal==BR_TRANSPORT_EVENT_LOSS)s="Voice connection interrupted.";
    else if(c->tail_incomplete||atomic_load(&c->ingress_incomplete))s="Voice connection interrupted.";
    else if(c->terminal_code[0]){
        if(!strcmp(c->terminal_code,"protocol"))s="Host sent an unexpected message";
        else{snprintf(out,cap,"Host error: %s%s",c->terminal_code,
            c->send_uncertain_seen?"; send unconfirmed":"");return BV_END_ERROR;}
    }
    else if(c->send_uncertain_seen)s="Could not confirm it was sent";
    else if(c->send_pending)s="Send unconfirmed";
    else if(c->cleanup_close_unconfirmed)s="Host error: close_unconfirmed";
    else if(link&&c->model.code[0]){
        snprintf(out,cap,"Host error: %s",c->model.code);return BV_END_ERROR;
    }
    else if(reason==BV_EXIT_DEADLINE){s="Mic off (time limit)";end=BV_END_OFF;}
    else if(reason==BV_EXIT_CONNECT){
        /* No answer, the host's one slot still busy (409) or a server-side
         * failure may pass. Any other answer is a refusal of this pet. */
        int http=c->http_status;
        if(http==0||http==409||http>=500){s=exit_status(c);end=BV_END_RETRY;}
        else if(http==401||http==403)s="Host refused this pet. Check its token.";
        else{snprintf(out,cap,"Host refused the connection (HTTP %d).",http);return BV_END_ERROR;}
    }else{
        s=exit_status(c);
        if(link||reason==BV_EXIT_QUEUE||reason==BV_EXIT_BEGIN)end=BV_END_RETRY;
    }
    /* Closing evidence vetoes every otherwise retryable exit, including local
     * queue/begin timeout, regardless of control-admission provenance. It must
     * not turn definite local/security/host errors into an ordinary ending.
     * Do not attribute a cause to a close that could race our own cleanup. */
    if(end==BV_END_RETRY&&c->model.end_notice!=BR_NOTICE_NONE){
        s=bridge_notice_text(c->model.end_notice);end=BV_END_OFF;
    }else if(end==BV_END_RETRY&&(c->model.wire_closed||c->transport_terminal==BR_TRANSPORT_PEER_NORMAL)){
        s="Mic off (call ended)";end=BV_END_OFF;
    }
    if(s)snprintf(out,cap,"%s",s);
    return end;
}
/* Whether a call that ended on a lost connection may be followed by another:
 * the count of attempts in a row without a healthy call (cleared by this call
 * if it conversed long enough), and the ceiling per mic-on. */
static bool retry_left(const bridge_voice_t *c,unsigned *failures){
    portENTER_CRITICAL(&completion_mux);
    int64_t ended=c->ended_at;bool recorded=c->ended_recorded;
    portEXIT_CRITICAL(&completion_mux);
    bool healthy=c->began_at&&recorded&&ended>=c->began_at&&ended-c->began_at>=BV_HEALTHY_US;
    *failures=healthy?0:c->failures;
    return *failures<BV_RETRIES&&c->reconnects<BV_RECONNECT_MAX;
}
/* Guard held, cleanup tail only. Keep this late ambiguous close separate from
 * the first semantic error and sticky send state, but latch it as a retry veto.
 * These checks describe receipt order, NOT a locally caused cleanup response.
 * Every other close_unconfirmed goes through the ordinary terminal-error path. */
static bool cleanup_unconfirmed(bridge_voice_t *c,const bv_frame_t *frame,const bridge_event_t *event){
    if(event->kind!=BR_EV_ERROR||strcmp(event->code,"close_unconfirmed")||
       !c->model.converse||c->model.phase!=BR_CLOSING||
       atomic_load(&c->exit_reason)!=BV_EXIT_QUEUE||!c->began_at||
       !c->cleanup_stop_sent||!frame->after_local_control||
       c->cleanup_stop_started_at<=0||frame->received_at<=c->cleanup_stop_started_at||
       frame->received_at>esp_timer_get_time())return false;
    c->cleanup_close_unconfirmed=true;
    return true;
}
/* The call is over with host frames still queued (the socket failed, or the
 * loop ended first). Read them only for the host's stated ending: no audio is
 * played, nothing is sent and nothing here can start or repeat a request. */
static void converse_tail(bridge_voice_t *c){
    bv_frame_t frame;
    while(xQueueReceive(c->frames,&frame,0)==pdTRUE){
        char *message=frame.text;
        bridge_event_t event;
        if(bridge_parse(message,strlen(message),&event)){
            if(event.kind!=BR_EV_AUDIO){
                if(take()){
                    observe_converse_send(c,&event);
                    bool unconfirmed=cleanup_unconfirmed(c,&frame,&event);
                    if(event.kind==BR_EV_ERROR&&!unconfirmed&&!c->terminal_code[0])
                        snprintf(c->terminal_code,sizeof(c->terminal_code),"%s",event.code);
                    if(event.kind==BR_EV_CLOSED&&!frame.after_local_control)
                        c->peer_closed_before_control=true;
                    /* Preserve semantic-error precedence for this ambiguous
                     * close; its sticky flag independently forbids retry. */
                    unsigned actions=unconfirmed?0:bridge_step(&c->model,&event);
                    if(actions&BR_ACT_CLOSE)health_end(c,frame.received_at);
                    if(!strcmp(c->model.code,"protocol")&&!c->terminal_code[0])
                        snprintf(c->terminal_code,sizeof(c->terminal_code),"protocol");
                    give();
                }else c->tail_incomplete=true;
            }
            bridge_event_clear(&event);
        }else protocol_failed(c);
        setup_bridge_wipe(message,strlen(message));free(message);
    }
}
#if defined(PET_PRIVATE_MIC_DIAG)
static bool bv_diag_write(const char *line,int n,size_t cap,int64_t until){
    if(n<=0||(size_t)n>=cap)return false;
    if(bv_keep_cur&&*bv_keep_cur_len+(unsigned)n<=BV_DIAG_KEEP_BYTES){
        memcpy(bv_keep_cur+*bv_keep_cur_len,line,(size_t)n);*bv_keep_cur_len+=(unsigned)n;
    }
    /* After the first console write that fails nothing more is written there;
     * the kept copy is still completed. */
    int64_t left=until-esp_timer_get_time();
    if(bv_keep_usb&&(left<=0||
        usb_serial_jtag_write_bytes(line,(size_t)n,pdMS_TO_TICKS((unsigned)(left/1000)))!=n))bv_keep_usb=false;
    return bv_keep_usb||bv_keep_cur!=NULL;
}
static void bv_diag_emit(bridge_voice_t *c){
    /* No model lock; both audio tasks and the transport have settled. One
     * bounded best-effort write window on the existing USB console. The
     * operator must discard a report without its trailer. */
    int64_t until=esp_timer_get_time()+2000000;
    unsigned count=c->tasks_before_n?bv_diag_snapshot(c,c->tasks_after):0,lost=0;
    int64_t elapsed=esp_timer_get_time()-c->tasks_start;
    int64_t terminal=c->ended_at-c->mic_start;
    unsigned ms=terminal>0?(unsigned)(terminal/1000):0;
    unsigned residual=(unsigned)xStreamBufferBytesAvailable(c->capture);
    char line[512];
    int n=snprintf(line,sizeof(line),"pet-mic-v1 begin %u %u %u %u %u\n",
        BV_DIAG_BINS,ms,(unsigned)(c->mic_clipped||c->owner_clipped),residual,atomic_load(&c->exit_reason));
    if(!bv_diag_write(line,n,sizeof(line),until))return;
    for(unsigned i=0;i<BV_DIAG_BINS;i++){
        const bv_diag_capture_t *m=&c->mic_bins[i];
        const bv_diag_owner_t *o=&c->owner_bins[i];
        n=snprintf(line,sizeof(line),"pet-mic-v1 bin %u %u %u %u %u %u %u %u %u %u\n",
            i,m->read,m->read_errors,m->read_max_us,m->startup,m->gated,m->admitted,m->queue_peak,o->popped,o->completed);
        if(!bv_diag_write(line,n,sizeof(line),until))return;
    }
    static const char end[]="pet-mic-v1 end\n";
    if(!bv_diag_write(end,sizeof(end)-1,sizeof(end),until))return;
    /* Positional, decimal, times in microseconds unless named ms. No content:
     * pet-bench-v1 begin <bins> <clipped>
     * pet-bench-v1 bin <i> <send_count> <send_us_total> <send_us_max>
     *   <frame_count> <frame_us_total> <frame_us_max> <loop_us_max>
     *   <frames_waiting_peak> <playback_peak> <play_bytes> <play_skipped> <play_write_us_max>
     * pet-bench-v1 transport <write_wait_us_total> <write_wait_us_max> <write_wait_count>
     *   <write_us_total> <write_us_max> <read_hold_us_total> <read_hold_us_max>
     *   <read_hold_count> <idle_read_polls>
     * pet-bench-v1 task <name> <core|-1> <priority> <delta_us>
     * pet-bench-v1 tasks_status <before_count> <after_count> <lost_count>
     * pet-bench-v1 tasks_total <elapsed_us>
     * pet-bench-v1 end
     * Final own-task records include tasks deleted before the end snapshot.
     * Other vanished tasks are counted as lost, never assigned a fake delta.
     * Modular uint32 runtime deltas are valid for this <10 minute window. */
    n=snprintf(line,sizeof(line),"pet-bench-v1 begin %u %u\n",BV_DIAG_BINS,
        (unsigned)(c->mic_clipped||c->owner_clipped||c->play_clipped));
    if(!bv_diag_write(line,n,sizeof(line),until))return;
    for(unsigned i=0;i<BV_DIAG_BINS;i++){
        const bv_diag_owner_t *o=&c->owner_bins[i];const bv_diag_play_t *p=&c->play_bins[i];
        n=snprintf(line,sizeof(line),"pet-bench-v1 bin %u %u %llu %u %u %llu %u %u %u %u %u %u %u\n",
            i,o->send_count,(unsigned long long)o->send_us_total,o->send_us_max,
            o->frame_count,(unsigned long long)o->frame_us_total,o->frame_us_max,o->loop_us_max,
            o->frames_waiting_peak,o->playback_peak,p->play_bytes,p->play_skipped,p->play_write_us_max);
        if(!bv_diag_write(line,n,sizeof(line),until))return;
    }
    pet_bench_transport_t t;bridge_transport_bench(&t);
    const pet_bench_io_t *s=&t.io;
    n=snprintf(line,sizeof(line),"pet-bench-v1 transport %llu %llu %llu %llu %llu %llu %llu %llu %llu\n",
        (unsigned long long)s->write_wait_us_total,(unsigned long long)s->write_wait_us_max,
        (unsigned long long)s->write_wait_count,(unsigned long long)s->write_us_total,
        (unsigned long long)s->write_us_max,(unsigned long long)s->read_hold_us_total,
        (unsigned long long)s->read_hold_us_max,(unsigned long long)s->read_hold_count,
        (unsigned long long)s->idle_read_polls);
    if(!bv_diag_write(line,n,sizeof(line),until))return;
    pet_bench_task_t retired[]={c->mic_task,c->play_task,t.task};
    unsigned after=count;
    for(unsigned i=0;i<3;i++){
        if(!retired[i].number||!c->tasks_after||!c->tasks_before_n||!after)continue;
        bool found=false;
        for(unsigned j=0;j<count;j++)if(c->tasks_after[j].number==retired[i].number)found=true;
        if(!found&&count<BV_DIAG_TASKS)c->tasks_after[count++]=retired[i];
    }
    for(unsigned i=0;i<c->tasks_before_n;i++){
        bool found=false;
        for(unsigned j=0;j<count;j++)if(c->tasks_before[i].number==c->tasks_after[j].number)found=true;
        if(!found)lost++;
    }
    for(unsigned i=0;i<count;i++){
        const pet_bench_task_t *task=&c->tasks_after[i];uint32_t base=0;
        for(unsigned j=0;j<c->tasks_before_n;j++)
            if(c->tasks_before[j].number==task->number)base=c->tasks_before[j].runtime;
        n=snprintf(line,sizeof(line),"pet-bench-v1 task %s %d %u %lu\n",
            task->name,task->core,task->priority,(unsigned long)(uint32_t)(task->runtime-base));
        if(!bv_diag_write(line,n,sizeof(line),until))return;
    }
    n=snprintf(line,sizeof(line),"pet-bench-v1 tasks_status %u %u %u\n",c->tasks_before_n,after,lost);
    if(!bv_diag_write(line,n,sizeof(line),until))return;
    n=snprintf(line,sizeof(line),"pet-bench-v1 tasks_total %llu\n",(unsigned long long)(elapsed>0?elapsed:0));
    if(!bv_diag_write(line,n,sizeof(line),until))return;
    const bv_diag_gaps_t *g=&c->gaps;const bv_diag_arrivals_t *a=&c->arrivals;
    n=snprintf(line,sizeof(line),"pet-bench-v1 playback %u %u %u %u %u %u %u %u %u %u %u %u %u %u %u %u %u %u\n",
        c->prebuffer_ms,g->underruns,g->total_ms,g->max_ms,
        g->hist[0],g->hist[1],g->hist[2],g->hist[3],g->hist[4],g->hist[5],a->count,a->max_ms,
        a->hist[0],a->hist[1],a->hist[2],a->hist[3],a->hist[4],a->hist[5]);
    if(!bv_diag_write(line,n,sizeof(line),until))return;
    unsigned rssi_end=bv_diag_rssi();
    wifi_ap_record_t ap={0};
    uint8_t channel=0;wifi_second_chan_t second=WIFI_SECOND_CHAN_NONE;
    wifi_bandwidth_t bandwidth=WIFI_BW_HT20;
    unsigned bw=0,is_11n=0;
    if(esp_wifi_sta_get_ap_info(&ap)==ESP_OK)is_11n=ap.phy_11n?1:0;
    if(esp_wifi_get_channel(&channel,&second)!=ESP_OK){channel=0;second=WIFI_SECOND_CHAN_NONE;}
    if(esp_wifi_get_bandwidth(WIFI_IF_STA,&bandwidth)==ESP_OK)bw=bandwidth==WIFI_BW_HT40?40:20;
    n=snprintf(line,sizeof(line),"pet-bench-v1 wifi %u %u %u %u %u %u %u\n",
        c->rssi_start,c->rssi_min,rssi_end,(unsigned)channel,(unsigned)second,bw,is_11n);
    if(!bv_diag_write(line,n,sizeof(line),until))return;
    static const char bench_end[]="pet-bench-v1 end\n";
    (void)bv_diag_write(bench_end,sizeof(bench_end)-1,sizeof(bench_end),until);
}
static void bv_diag_report(bridge_voice_t *c){
    if(!c->mic_started)return;
    bool locked=bv_keep_lock&&xSemaphoreTake(bv_keep_lock,pdMS_TO_TICKS(5000))==pdTRUE;
    if(locked){
        unsigned slot=bv_keep_count%BV_DIAG_KEEP;
        if(!bv_keep_text[slot])bv_keep_text[slot]=heap_caps_malloc(BV_DIAG_KEEP_BYTES,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
        bv_keep_len[slot]=0;bv_keep_start_ms[slot]=(unsigned)(c->mic_start/1000);
        bv_keep_cur=bv_keep_text[slot];bv_keep_cur_len=&bv_keep_len[slot];
    }
    bv_keep_usb=true;
    bv_diag_emit(c);
    if(locked){bv_keep_cur=NULL;bv_keep_count++;xSemaphoreGive(bv_keep_lock);}
}
/* pet-bench-v1 kept <back> <calls_since_boot> <bytes> <call_start_uptime_ms> <now_uptime_ms>
 * then the kept report unchanged, then "pet-bench-v1 kept-end". */
void bridge_voice_bench_last(unsigned back){
    if(!bv_keep_lock||xSemaphoreTake(bv_keep_lock,pdMS_TO_TICKS(3000))!=pdTRUE)return;
    unsigned have=bv_keep_count<BV_DIAG_KEEP?bv_keep_count:BV_DIAG_KEEP,len=0,start=0;
    const char *text=NULL;
    if(back<have){
        unsigned slot=(bv_keep_count-1-back)%BV_DIAG_KEEP;
        text=bv_keep_text[slot];len=text?bv_keep_len[slot]:0;start=bv_keep_start_ms[slot];
    }
    char line[96];
    int n=snprintf(line,sizeof(line),"pet-bench-v1 kept %u %u %u %u %u\n",back,bv_keep_count,len,start,
        (unsigned)(esp_timer_get_time()/1000));
    usb_serial_jtag_write_bytes(line,(size_t)n,pdMS_TO_TICKS(200));
    for(unsigned off=0;off<len;){
        unsigned chunk=len-off>256?256:len-off;
        if(usb_serial_jtag_write_bytes(text+off,chunk,pdMS_TO_TICKS(500))!=(int)chunk)break;
        off+=chunk;
    }
    static const char done[]="pet-bench-v1 kept-end\n";
    usb_serial_jtag_write_bytes(done,sizeof(done)-1,pdMS_TO_TICKS(200));
    xSemaphoreGive(bv_keep_lock);
}
#endif
static void cleanup(bridge_voice_t *c,bool wait){
    health_end(c,esp_timer_get_time()); /* before close, linger or proof waits */
    quiet(c);
    if(wait&&atomic_load(&c->converse)){
        call_report.again=false;
        if(c->frames)converse_tail(c);
        /* Audio is off, but the final tail and transport result are not proven.
         * Never promise a replacement while closing evidence can still arrive. */
        live_voice_bridge_status(LIVE_CLOSING,"Releasing voice connection...");
    }
    finalize_model(c);
    if(!c->disposal_started&&c->transport&&bridge_transport_started(c->transport)){
        if(atomic_load(&c->connected)&&esp_timer_get_time()<c->hard){
            /* Close the pet only, never cancel or replay Unified input. */
            bool stopped=send_frame_timed(c,"{\"type\":\"pet.stop\"}",BV_SEND_MS,&c->cleanup_stop_started_at);
            /* Record full local send completion, not peer receipt or cause.
             * Cancellation/finish while returning leaves completion unproven. */
            c->cleanup_stop_sent=stopped&&!bridge_transport_finished(c->transport)&&bridge_transport_upgraded(c->transport);
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
    /* In-flight SDK DATA may still complete across cancellation. Keep the
     * callback queue alive through actual vendor-task exit; audio is already
     * inhibited and no new client I/O is admitted. */
    atomic_store(&c->active,false);
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
    bool transport_settled=!c->transport||bridge_transport_dispose_result(c->transport,until,&c->transport_terminal);
    if(transport_settled){
        /* The callback task can still complete a fragmented message while
         * disposal is pending. Inspect assembly only after final-access proof. */
        if(c->assembly.active)atomic_store(&c->ingress_incomplete,true);
        c->transport=NULL;c->ws=NULL;
    }
    else atomic_store(&restart_required,true);
    if(!audio_settled||!transport_settled){
        c->retained=true;
        if(take()){
            setup_bridge_wipe(c->model.text,sizeof(c->model.text));setup_bridge_wipe(c->model.display,sizeof(c->model.display));give();
        }
        live_voice_bridge_status(LIVE_CLOSING,"Releasing voice connection...");
        return; /* exceptional unproven ownership: never timeout-and-free */
    }
    atomic_store(&c->accepting,false);
    /* Only now has the callback task actually finished. A notice can arrive
     * during close/linger, even after the first tail drain. Process its queued
     * frames before the final retry decision, never before ingress settles. */
    if(c->model.converse&&c->frames)converse_tail(c);
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
    bool converse_error=false,again=false;
    if(c->model.converse){
        unsigned end=converse_end(c,terminal_view.status,sizeof(terminal_view.status)),failures=c->failures;
        bool reached=c->reached||c->began_at;
        if(atomic_load(&c->muted)||(c->parent_stopped&&c->parent_stopped()&&esp_timer_get_time()<c->work)){
            /* A later explicit tap always wins over any previously recorded
             * disconnect or notice, including one arriving during cleanup. */
            snprintf(terminal_view.status,sizeof(terminal_view.status),"Mic off");end=BV_END_OFF;
        }
        converse_error=end!=BV_END_OFF;
        /* Only the owner's own cleanup (wait) may lead to another call; a
         * late release by the reaper never does. */
        if(end==BV_END_RETRY&&wait){
            if(stopping(c)){
                /* Mic off arrived while this call was being released. */
                snprintf(terminal_view.status,sizeof(terminal_view.status),"Mic off");converse_error=false;
            }else if(retry_left(c,&failures)){
                again=true;snprintf(terminal_view.status,sizeof(terminal_view.status),"Reconnecting...");
            }else snprintf(terminal_view.status,sizeof(terminal_view.status),"%s",
                reached?"Connection lost - mic off":"Could not reach the host - mic off");
        }
        if(wait){
            portENTER_CRITICAL(&completion_mux);
            call_report.ended_at=c->ended_at;
            portEXIT_CRITICAL(&completion_mux);
            call_report.again=again;call_report.failures=failures;call_report.reached=reached;
        }
    }
    terminal_valid=true;
    bridge_view_t final=terminal_view;
    bool final_error=c->model.converse?converse_error:
        (atomic_load(&c->failed)||final.send==BR_SEND_UNKNOWN||final.reply==BR_REPLY_UNAVAILABLE);
    if(current==c)current=NULL;
    atomic_store(&restart_required,false);
    give(); /* unpublish before freeing anything the UI could read */
#if defined(PET_PRIVATE_MIC_DIAG)
    bv_diag_report(c);
#endif
    if(c->capture)vStreamBufferDelete(c->capture);
    if(c->playback)vStreamBufferDelete(c->playback);
    if(c->frames){
        bv_frame_t pending;
        while(xQueueReceive(c->frames,&pending,0)==pdTRUE){
            setup_bridge_wipe(pending.text,strlen(pending.text));free(pending.text);
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
#if defined(PET_PRIVATE_MIC_DIAG)
    free(c->task_scratch);free(c->tasks_before);free(c->tasks_after);
#endif
    setup_bridge_wipe(c,sizeof(*c));free(c);
    live_voice_bridge_status(again?LIVE_RECONNECTING:final_error?LIVE_ERROR:LIVE_OFF,final.status);
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
/* One complete call: connect, converse, and release everything through
 * cleanup. Leaves in call_report whether another call is to follow. */
static void run_call(const setup_bridge_t *cfg,int64_t deadline,bool (*parent_stopped)(void),
                     unsigned failures,unsigned reconnects,bool reached,int64_t retry_until){
    call_report.again=false;
#if defined(PET_PRIVATE_MIC_DIAG)
    unsigned audio_priority=atomic_exchange(&bench_audio_priority,BV_AUDIO_PRIORITY);
    int requested_core=atomic_exchange(&bench_audio_core,BV_AUDIO_CORE);
    BaseType_t audio_core=requested_core<0?tskNO_AFFINITY:requested_core;
#else
    unsigned audio_priority=BV_AUDIO_PRIORITY;
    BaseType_t audio_core=BV_AUDIO_CORE<0?tskNO_AFFINITY:BV_AUDIO_CORE;
#endif
    if(atomic_load(&restart_required)){live_voice_bridge_status(LIVE_ERROR,"Voice cleanup incomplete. Connection held.");return;}
    bridge_voice_t *c=heap_caps_calloc(1,sizeof(*c),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if(!c){live_voice_bridge_status(LIVE_ERROR,"Bridge memory unavailable.");return;}
    c->hard=deadline;c->work=deadline-2000000;c->start=esp_timer_get_time();
    c->parent_stopped=parent_stopped;
    c->failures=failures;c->reconnects=reconnects;c->reached=reached;
    /* The pet's one voice control is a running conversation: mic on listens and
     * talks back, mic off ends the call. The one-turn preview/Send model stays
     * in this file for its lifecycle tests but is not started from here. */
    atomic_store(&c->converse,true);atomic_store(&c->speaker_until,(uint32_t)(c->start/1000));
    bridge_init(&c->model);bridge_command(&c->model,BR_CMD_CONVERSE,0);
    c->frames=xQueueCreate(BV_FRAMES,sizeof(bv_frame_t));c->events=xEventGroupCreate();
    c->capture_memory=heap_caps_malloc(BV_MIC+1,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->playback_memory=heap_caps_malloc(BV_SPEAKER+1,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->assembly_memory=heap_caps_malloc(BV_FRAME,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->append_pcm=heap_caps_malloc(BV_APPEND,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->append_json=heap_caps_malloc(7000,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->decoded=heap_caps_malloc(BV_APPEND,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
#if defined(PET_PRIVATE_MIC_DIAG)
    /* Optional diagnostic storage: allocation failure never changes call admission. */
    c->prebuffer_ms=atomic_load(&bench_prebuffer_ms);
    c->task_scratch=heap_caps_malloc(BV_DIAG_TASKS*sizeof(*c->task_scratch),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->tasks_before=heap_caps_malloc(BV_DIAG_TASKS*sizeof(*c->tasks_before),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    c->tasks_after=heap_caps_malloc(BV_DIAG_TASKS*sizeof(*c->tasks_after),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
#endif
    if(!c->frames||!c->events||!c->capture_memory||!c->playback_memory||!c->assembly_memory||!c->append_pcm||!c->append_json||!c->decoded)goto setup_failed;
    c->capture=xStreamBufferCreateStatic(BV_MIC+1,1,c->capture_memory,&c->capture_meta);
    c->playback=xStreamBufferCreateStatic(BV_SPEAKER+1,1,c->playback_memory,&c->playback_meta);
    if(!c->capture||!c->playback)goto setup_failed;
    char token[44]={0};
    esp_err_t loaded=network_copy_bridge_token(token,sizeof(token));
    if(loaded==ESP_OK)c->transport=bridge_transport_new(cfg,token,c->hard);
    setup_bridge_wipe(token,sizeof(token));if(!c->transport)goto setup_failed;
    if(!bridge_transport_set_stop_check(c->transport,transport_stopping,c))goto setup_failed;
    c->ws=bridge_transport_client(c->transport);
    if(!take())goto setup_failed;
    current=c;terminal_valid=false;setup_bridge_wipe(&terminal_view,sizeof(terminal_view));give();
    if(stopping(c))goto cleanup;
    atomic_store(&c->active,true);atomic_store(&c->accepting,true);
    if(esp_websocket_register_events(c->ws,WEBSOCKET_EVENT_ANY,ws_event,c)!=ESP_OK)goto setup_failed;
    if(retry_until&&esp_timer_get_time()>retry_until){note_exit(c,BV_EXIT_RETRY_WINDOW);goto cleanup;}
    if(bridge_transport_start_before(c->transport,retry_until)!=ESP_OK){
        if(stopping(c))goto cleanup;
        if(retry_until&&esp_timer_get_time()>retry_until){note_exit(c,BV_EXIT_RETRY_WINDOW);goto cleanup;}
        goto setup_failed;
    }
    publish(c);
    int64_t startup=c->start+25000000;
    if(startup>c->work)startup=c->work;
    while(!atomic_load(&c->connected)&&!atomic_load(&c->failed)&&!bridge_transport_finished(c->transport)&&!stopping(c)&&esp_timer_get_time()<startup)vTaskDelay(pdMS_TO_TICKS(10));
    if(!atomic_load(&c->connected)||stopping(c)){
        /* Before the connection is up every failure is "could not reach",
         * told apart only by what the host answered, if it answered. */
        if(!stopping(c)){
            c->http_status=bridge_transport_http_status(c->transport);
            if(atomic_load(&c->connected_seen))note_exit(c,BV_EXIT_DISCONNECTED);
            else atomic_store(&c->exit_reason,BV_EXIT_CONNECT);
            atomic_store(&c->failed,true);
        }
        goto cleanup;
    }
    if(!send_frame(c,PET_LIVE_START)){ /* host discards device reasoning fields */
        if(!stopping(c)){note_exit(c,BV_EXIT_SEND);atomic_store(&c->failed,true);}
        goto cleanup;
    }
    int64_t next_audio=0,begin_by=esp_timer_get_time()+BV_BEGIN_US;
    while(esp_timer_get_time()<c->work&&!stopping(c)&&!atomic_load(&c->failed)){
#if defined(PET_PRIVATE_MIC_DIAG)
        bv_diag_loop(c);
#endif
        bv_frame_t frame;
        if(xQueueReceive(c->frames,&frame,pdMS_TO_TICKS(5))==pdTRUE){
#if defined(PET_PRIVATE_MIC_DIAG)
            int64_t frame_begin=esp_timer_get_time();
#endif
            char *message=frame.text;
            bridge_event_t event;
            if(!bridge_parse(message,strlen(message),&event)){setup_bridge_wipe(message,strlen(message));free(message);
#if defined(PET_PRIVATE_MIC_DIAG)
                bv_diag_frame(c,frame_begin);
#endif
                protocol_failed(c);failure(c,"invalid_preview");break;}
            if(!take()){c->tail_incomplete=true;note_exit(c,BV_EXIT_GUARD);bridge_event_clear(&event);setup_bridge_wipe(message,strlen(message));free(message);
#if defined(PET_PRIVATE_MIC_DIAG)
                bv_diag_frame(c,frame_begin);
#endif
                break;}
            observe_converse_send(c,&event);
            if(event.kind==BR_EV_ERROR&&!c->terminal_code[0])
                snprintf(c->terminal_code,sizeof(c->terminal_code),"%s",event.code);
            if(event.kind==BR_EV_CLOSED&&!frame.after_local_control)
                c->peer_closed_before_control=true;
            unsigned a=bridge_step(&c->model,&event);
            if(!strcmp(c->model.code,"protocol")&&!c->terminal_code[0])
                snprintf(c->terminal_code,sizeof(c->terminal_code),"protocol");
            if(c->model.reply==BR_REPLY_RECEIVED&&c->received_pcm==0){
                c->model.reply=BR_REPLY_UNAVAILABLE;c->model.draining=false;
                a=BR_ACT_MUTE|BR_ACT_CLOSE;
            }
            c->actions|=a;
            if(a&BR_ACT_CLOSE){health_end(c,frame.received_at);note_exit(c,BV_EXIT_HOST);} /* host ending */
            if(a&BR_ACT_NOTICE)c->notice_until=esp_timer_get_time()+BV_NOTICE_US;
            if(a&BR_ACT_MUTE)quiet(c);
            if(event.kind==BR_EV_REPLY_STARTED&&c->model.phase==BR_REPLYING&&!atomic_load(&c->stop)&&!atomic_load(&c->failed)){atomic_store(&c->play_gate,true);atomic_store(&c->audio_epoch,c->model.audio_epoch);}
            if(a&BR_ACT_DRAIN)atomic_store(&c->drain,true);
            if(a&BR_ACT_PLAY){size_t bytes=0;int e=mbedtls_base64_decode(c->decoded,BV_APPEND,&bytes,(const unsigned char*)event.audio,strlen(event.audio));
                /* A conversation's backlog after a stalled link can exceed the
                 * speaker buffer: that reply audio is dropped whole (a gap),
                 * never a partial, misaligned write, and the call continues. */
                bool room=!c->model.converse||xStreamBufferSpacesAvailable(c->playback)>=bytes;
                if(e||!atomic_load(&c->play_gate)||atomic_load(&c->stop)||
                   (room&&xStreamBufferSend(c->playback,c->decoded,bytes,0)!=bytes)){note_exit(c,BV_EXIT_SPEAKER);atomic_store(&c->failed,true);}
                else if(room){
                    c->received_pcm+=(unsigned)bytes;
#if defined(PET_PRIVATE_MIC_DIAG)
                    bv_diag_arrival(c);
#endif
                }}
            give();bridge_event_clear(&event);setup_bridge_wipe(message,strlen(message));free(message);
#if defined(PET_PRIVATE_MIC_DIAG)
            bv_diag_frame(c,frame_begin);
#endif
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
            if(stopping(c)||esp_timer_get_time()>=c->work)break;
            if(audio_stream_open(&c->audio)!=ESP_OK){note_exit(c,BV_EXIT_AUDIO);failure(c,"reply_unavailable");break;}
            if(stopping(c)||esp_timer_get_time()>=c->work){quiet(c);break;}
#if defined(PET_PRIVATE_MIC_DIAG)
            c->mic_start=esp_timer_get_time();
            c->mic_started=true;
            bv_diag_wifi_sample(c,c->mic_start);
            c->tasks_start=esp_timer_get_time();
            c->tasks_before_n=bv_diag_snapshot(c,c->tasks_before);
            c->loop_start=c->mic_start;
#endif
            if(xTaskCreatePinnedToCore(capture_task,"pet_bridge_mic",4096,c,audio_priority,NULL,audio_core)!=pdPASS){note_exit(c,BV_EXIT_AUDIO);failure(c,"reply_unavailable");break;}
            c->audio_tasks|=BV_CAPTURE_DONE;
            if(xTaskCreatePinnedToCore(play_task,"pet_bridge_out",4096,c,audio_priority,NULL,audio_core)!=pdPASS){note_exit(c,BV_EXIT_AUDIO);failure(c,"reply_unavailable");break;}
            c->audio_tasks|=BV_PLAY_DONE;c->began_at=esp_timer_get_time();
            if(!take()){note_exit(c,BV_EXIT_GUARD);failure(c,"reply_unavailable");break;}
            if(stopping(c)||atomic_load(&c->failed)){give();quiet(c);break;}
            /* The parent latch and these gate stores share the very same
             * status lock. Stop before admission prevents either gate; a Stop
             * after admission closes them via bridge_voice_stop. No I/O or
             * task creation occurs while the status lock is held. */
            if(!live_voice_bridge_audio_begin()){give();stopping(c);quiet(c);break;}
            if(atomic_load(&c->stop)||atomic_load(&c->failed)||esp_timer_get_time()>=c->work){
                live_voice_bridge_audio_end();give();quiet(c);break;
            }
            atomic_store(&c->capture_gate,true);
            /* A conversation plays host audio whenever it arrives, from the start. */
            if(phase==BR_CONVERSING)atomic_store(&c->play_gate,true);
            live_voice_bridge_audio_end();
            give();
        }
        if((actions&BR_ACT_FINISH_CAPTURE)&&!finish_capture(c,&next_audio))break;
        if(actions&BR_ACT_CONFIRM){if(!send_action(c,BR_ACT_CONFIRM)){failure(c,"reply_unavailable");break;}}
        if(actions&BR_ACT_CLOSE)break;
        if(atomic_load(&c->drain)&&(xEventGroupGetBits(c->events)&BV_DRAINED)){
            if(take()){c->actions|=bridge_playback_drained(&c->model);give();}break;
        }
        if((phase==BR_CAPTURING||phase==BR_CONVERSING)&&esp_timer_get_time()>=next_audio){
            size_t n=xStreamBufferReceive(c->capture,c->append_pcm,BV_APPEND,0);
#if defined(PET_PRIVATE_MIC_DIAG)
            bv_diag_owner_t *b=n?bv_diag_owner_bin(c):NULL;
            if(b)b->popped+=n;
#endif
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
/* Guard taken here. The last word of a mic-on that ends between two calls. */
static void between_calls(live_state_t state,const char *text){
    if(take()){snprintf(terminal_view.status,sizeof(terminal_view.status),"%s",text);give();}
    live_voice_bridge_status(state,text);
}
void bridge_voice_run(const setup_bridge_t *cfg,int64_t first_deadline,int64_t call_limit,bool (*parent_stopped)(void)){
    unsigned failures=0,reconnects=0;bool reached=false;int64_t deadline=first_deadline,lost_at=0,retry_until=0;
    for(;;){
        run_call(cfg,deadline,parent_stopped,failures,reconnects,reached,retry_until);
        /* Every call has been released in full by its own cleanup before this
         * point: its codec, tasks, buffers (with any microphone audio not yet
         * sent) and transport are gone. Nothing of it is carried over, and
         * nothing is ever sent again. */
        if(!call_report.again)return;
        failures=call_report.failures;reached=call_report.reached;
        if(!failures)lost_at=call_report.ended_at; /* original loss, or loss after a healthy call */
        if(!lost_at){
            between_calls(LIVE_ERROR,reached?"Connection lost - mic off":"Could not reach the host - mic off");return;
        }
        retry_until=lost_at+BV_RETRY_WINDOW_US;
        int64_t until=esp_timer_get_time()+(int64_t)BV_RETRY_DELAY_S[failures]*1000000;
        if(until>retry_until){
            between_calls(LIVE_ERROR,reached?"Connection lost - mic off":"Could not reach the host - mic off");return;
        }
        failures++;reconnects++;
        /* No microphone, speaker or socket exists while waiting. Mic off ends
         * the wait at once. */
        for(;;){
            if(parent_stopped&&parent_stopped()){between_calls(LIVE_OFF,"Mic off");return;}
            if(esp_timer_get_time()>=until)break;
            vTaskDelay(pdMS_TO_TICKS(BV_RETRY_POLL_MS));
        }
        /* Ending the last call latched the amplifier off, as every end does.
         * The mic is still on, so re-arm it for the new call, then look for
         * mic off once more: a tap from here on finds either no call (and the
         * new one stops before it opens audio) or a current one, which it
         * mutes and latches off again exactly as on a first call. */
        audio_stream_prepare();
        if(parent_stopped&&parent_stopped()){audio_stream_inhibit();between_calls(LIVE_OFF,"Mic off");return;}
        if(esp_timer_get_time()>retry_until){
            audio_stream_inhibit();between_calls(LIVE_ERROR,reached?"Connection lost - mic off":"Could not reach the host - mic off");return;
        }
        deadline=esp_timer_get_time()+call_limit;
    }
}
