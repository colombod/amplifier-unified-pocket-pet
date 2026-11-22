#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "cJSON.h"
#define BR_TEXT_BYTES 16000
#define BR_TEXT_POINTS 4000
#define BR_DISPLAY_BYTES 40000
#define BR_FRAME_BYTES 96000
/* Host-testable, single-owner model. Caller serializes UI commands and network events.
 * No logging, transport, microphone or automatic confirmation occurs here. */
typedef enum { BR_EV_STARTED,BR_EV_BEGAN,BR_EV_PREVIEW,BR_EV_INVALIDATED,BR_EV_SEND_RESULT,
    BR_EV_REPLY_STARTED,BR_EV_REPLY_DONE,BR_EV_AUDIO,BR_EV_ERROR,BR_EV_CLOSED,
    BR_EV_CONVERSE_BEGAN,BR_EV_CONVERSE_STATE,BR_EV_CONVERSE_NOTICE } bridge_event_kind_t;
typedef struct {
    bridge_event_kind_t kind;
    cJSON *document; /* owned; bridge_event_clear wipes all parsed string values */
    const char *turn,*text,*hash,*label,*short_id,*state,*code,*audio;
    uint32_t revision;
} bridge_event_t;
/* BR_CONVERSING is the running spoken conversation: microphone streamed and
 * host audio played for its whole length. It never passes through the one-turn
 * capture/preview/send phases, so no device confirmation can be produced. */
typedef enum { BR_IDLE,BR_CONNECTING,BR_BEGINNING,BR_CAPTURING,BR_SETTLING,BR_PREVIEW,
    BR_SENDING,BR_AWAIT_REPLY,BR_REPLYING,BR_CONVERSING,BR_CLOSING,BR_FINISHED,BR_ERROR } bridge_phase_t;
typedef enum { BR_SEND_NONE,BR_SEND_ACCEPTED,BR_SEND_REJECTED,BR_SEND_UNKNOWN } bridge_send_t;
typedef enum { BR_REPLY_NONE,BR_REPLY_RECEIVED,BR_REPLY_UNAVAILABLE,BR_REPLY_STOPPED } bridge_reply_t;
/* BR_CMD_CONVERSE connects in conversation mode; BR_CMD_CONNECT is one-turn. */
typedef enum { BR_CMD_CONNECT,BR_CMD_FINISH_CAPTURE,BR_CMD_CONFIRM,BR_CMD_STOP,BR_CMD_CONVERSE } bridge_command_t;
enum { BR_ACT_NONE=0,BR_ACT_BEGIN=1,BR_ACT_CAPTURE=2,BR_ACT_FINISH_CAPTURE=4,
       BR_ACT_CONFIRM=8,BR_ACT_STOP=16,BR_ACT_MUTE=32,BR_ACT_CLOSE=64,
       BR_ACT_PLAY=128,BR_ACT_DRAIN=256,BR_ACT_NOTICE=512 };
/* Display only. The host decides and reports these; the device never infers them. */
typedef enum { BR_TALK_LISTENING,BR_TALK_ASKING,BR_TALK_ANSWERING,BR_TALK_WORKING } bridge_talk_t;
typedef enum { BR_NOTICE_NONE,BR_NOTICE_BUSY,BR_NOTICE_NOT_SENT,BR_NOTICE_SEND_UNCERTAIN,
    BR_NOTICE_REPLY_UNAVAILABLE,BR_NOTICE_IDLE_TIMEOUT,BR_NOTICE_TIME_LIMIT,BR_NOTICE_NO_WORDS_YET } bridge_notice_t;
typedef struct {
    bridge_phase_t phase;
    bridge_send_t send;
    bridge_reply_t reply;
    char turn[37],hash[65],label[401],short_id[9],code[32];
    char text[BR_TEXT_BYTES+1],display[BR_DISPLAY_BYTES+1];
    uint32_t revision,audio_epoch,ignored;
    bool preview_seen,confirmed,stop_sent,draining,wire_closed;
    /* Conversation mode. notice is the latest one; end_notice is the host's
     * stated reason for ending (quiet/time limit), kept for the final status. */
    bool converse,notice_visible;
    bridge_talk_t talk;
    bridge_notice_t notice,end_notice;
} bridge_model_t;
typedef struct bridge_view {
    bridge_phase_t phase;
    bridge_send_t send;
    bridge_reply_t reply;
    bool can_finish_capture,can_send,can_stop;
    uint32_t revision,audio_epoch;
    bool converse;
    bridge_talk_t talk;
    char target[432],status[112];
} bridge_view_t;
bool bridge_uuid_valid(const char *s);
bool bridge_utf8(const char *s,size_t bytes,size_t *points);
bool bridge_parse(const char *json,size_t length,bridge_event_t *out);
void bridge_event_clear(bridge_event_t *event);
void bridge_init(bridge_model_t *model);
unsigned bridge_step(bridge_model_t *model,const bridge_event_t *event);
unsigned bridge_command(bridge_model_t *model,bridge_command_t command,uint32_t revision);
/* Call only after the actual local speaker worker acknowledged its queue empty. */
unsigned bridge_playback_drained(bridge_model_t *model);
/* The caller owns the clock: ends the brief display of the latest notice. */
void bridge_notice_expire(bridge_model_t *model);
/* Fixed display text for a notice code; never host-supplied text. */
const char *bridge_notice_text(bridge_notice_t notice);
void bridge_view(const bridge_model_t *model,bridge_view_t *view);
/* Builder never accepts unchecked text or caller-selected target identities. */
int bridge_encode(const bridge_model_t *model,unsigned action,char *out,size_t capacity);
