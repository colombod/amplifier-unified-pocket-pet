#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "bridge_protocol.h"
#define TURN "123e4567-e89b-42d3-a456-426614174000"
#define HASH "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
static bridge_model_t m;
static unsigned feed(const char *json) {
    bridge_event_t e;assert(bridge_parse(json,strlen(json),&e));
    unsigned a=bridge_step(&m,&e);bridge_event_clear(&e);return a;
}
static void capture(void) {
    bridge_init(&m);assert(bridge_command(&m,BR_CMD_CONNECT,0)==0);
    assert(feed("{\"type\":\"session.started\"}")==BR_ACT_BEGIN);
    assert(feed("{\"type\":\"pet.turn.began\",\"turn_id\":\"" TURN "\",\"safe_session_label\":\"Existing\",\"session_id_short\":\"23133f96\"}")==BR_ACT_CAPTURE);
    assert(bridge_command(&m,BR_CMD_FINISH_CAPTURE,0)==BR_ACT_FINISH_CAPTURE);
}
static void preview(void) {
    capture();assert(feed("{\"type\":\"pet.turn.preview\",\"turn_id\":\"" TURN "\",\"preview_revision\":1,\"text\":\"hello\",\"text_sha256\":\"" HASH "\"}")==0);
    bridge_view_t v;bridge_view(&m,&v);assert(v.can_send&&v.revision==1);
    assert(!strcmp(m.text,"hello")&&!strcmp(m.display,"hello"));
}
/* Conversation mode. Golden frames are the exact byte strings of the binding
 * design (conversation-mode.md, "Golden frames"); the host tests the same bytes. */
#define G_BEGIN "{\"type\":\"pet.converse.begin\"}"
#define G_BEGAN "{\"type\":\"pet.converse.began\",\"safe_session_label\":\"Example conversation\",\"session_id_short\":\"a1b2c3d4\"}"
#define G_STATE(x) "{\"type\":\"pet.converse.state\",\"state\":\"" x "\"}"
#define G_NOTICE(x) "{\"type\":\"pet.converse.notice\",\"code\":\"" x "\"}"
#define STARTED "{\"type\":\"session.started\"}"
#define CLOSED "{\"type\":\"session.closed\"}"
#define AUDIO "{\"type\":\"session.output_audio.delta\",\"delta\":\"AQI=\"}"
static const char *status(void){static bridge_view_t view;bridge_view(&m,&view);return view.status;}
static void conversing(void){
    char wire[64];
    bridge_init(&m);assert(bridge_command(&m,BR_CMD_CONVERSE,0)==0&&m.phase==BR_CONNECTING&&m.converse);
    assert(!strcmp(status(),"Connecting..."));
    assert(feed(AUDIO)==0&&m.ignored==1);                 /* no audio before the conversation began */
    assert(feed(STARTED)==BR_ACT_BEGIN&&m.phase==BR_BEGINNING);
    assert(bridge_encode(&m,BR_ACT_BEGIN,wire,sizeof(wire))==(int)strlen(G_BEGIN)&&!strcmp(wire,G_BEGIN));
    assert(feed(G_STATE("listening"))==0&&m.phase==BR_BEGINNING); /* not before began */
    assert(feed(G_BEGAN)==BR_ACT_CAPTURE&&m.phase==BR_CONVERSING);
    assert(!strcmp(m.label,"Example conversation")&&!strcmp(m.short_id,"a1b2c3d4")&&!m.turn[0]);
}
static void rejects(const char *json){bridge_event_t e;assert(!bridge_parse(json,strlen(json),&e));}
static void conversation_cases(void){
    char wire[64];bridge_event_t e;bridge_view_t v;
    /* Every gateway->device golden frame parses, with the documented meaning. */
    const char *golden[]={G_BEGAN,G_STATE("listening"),G_STATE("asking"),G_STATE("working"),G_STATE("answering"),
        G_NOTICE("busy"),G_NOTICE("not_sent"),G_NOTICE("send_uncertain"),G_NOTICE("reply_unavailable"),
        G_NOTICE("idle_timeout"),G_NOTICE("time_limit"),G_NOTICE("no_words_yet")};
    for(size_t i=0;i<sizeof(golden)/sizeof(*golden);i++){
        assert(bridge_parse(golden[i],strlen(golden[i]),&e));
        assert(e.kind==(i==0?BR_EV_CONVERSE_BEGAN:i<5?BR_EV_CONVERSE_STATE:BR_EV_CONVERSE_NOTICE));
        bridge_event_clear(&e);
    }
    /* A missing, wrongly typed or extra field is a protocol error, never ignored. */
    rejects("{\"type\":\"pet.converse.state\"}");
    rejects("{\"type\":\"pet.converse.state\",\"state\":5}");
    rejects("{\"type\":\"pet.converse.state\",\"state\":\"listening\",\"extra\":\"x\"}");
    rejects("{\"type\":\"pet.converse.state\",\"state\":\"listening\",\"turn_id\":\"" TURN "\"}");
    rejects("{\"type\":\"pet.converse.notice\"}");
    rejects("{\"type\":\"pet.converse.notice\",\"code\":7}");
    rejects("{\"type\":\"pet.converse.notice\",\"code\":\"busy\",\"state\":\"listening\"}");
    rejects("{\"type\":\"pet.converse.began\",\"safe_session_label\":\"Example conversation\"}");
    rejects("{\"type\":\"pet.converse.began\",\"safe_session_label\":\"Example conversation\",\"session_id_short\":\"A1B2C3D4\"}");
    rejects("{\"type\":\"pet.converse.began\",\"safe_session_label\":\"\",\"session_id_short\":\"a1b2c3d4\"}");
    rejects("{\"type\":\"pet.converse.began\",\"safe_session_label\":7,\"session_id_short\":\"a1b2c3d4\"}");
    rejects("{\"type\":\"pet.converse.began\",\"turn_id\":\"" TURN "\",\"safe_session_label\":\"Example conversation\",\"session_id_short\":\"a1b2c3d4\"}");
    rejects("{\"type\":\"pet.converse.begin\"}"); /* device->gateway only */
    /* The one-turn start still emits its own frame: the old flow's bytes are unchanged. */
    bridge_init(&m);bridge_command(&m,BR_CMD_CONNECT,0);feed(STARTED);
    assert(bridge_encode(&m,BR_ACT_BEGIN,wire,sizeof(wire))>0&&!strcmp(wire,"{\"type\":\"pet.turn.begin\"}"));

    /* Normal conversation: several state changes, audio at any time, notices. */
    conversing();
    bridge_view(&m,&v);assert(v.converse&&v.can_stop&&!v.can_send&&!v.can_finish_capture);
    assert(!strcmp(status(),"Listening"));
    assert(feed(AUDIO)==BR_ACT_PLAY);
    assert(feed(G_STATE("listening"))==0&&m.talk==BR_TALK_LISTENING&&!strcmp(status(),"Listening"));
    assert(feed(G_NOTICE("no_words_yet"))==BR_ACT_NOTICE);
    bridge_notice_expire(&m);
    assert(!strcmp(status(),"Listening: no speech recognized"));
    assert(feed(G_STATE("listening"))==0&&!strcmp(status(),"Listening"));
    assert(feed(G_STATE("asking"))==0&&m.talk==BR_TALK_ASKING&&!strcmp(status(),"Asking Unified..."));
    assert(feed(G_STATE("working"))==0&&m.talk==BR_TALK_WORKING&&!strcmp(status(),"Accepted. Waiting for reply..."));
    bridge_view(&m,&v);assert(v.can_stop&&!v.can_send&&!v.can_finish_capture);
    assert(feed(AUDIO)==BR_ACT_PLAY);
    assert(feed(G_STATE("answering"))==0&&m.talk==BR_TALK_ANSWERING&&!strcmp(status(),"Speaking"));
    assert(feed(AUDIO)==BR_ACT_PLAY);
    assert(feed(G_STATE("listening"))==0&&!strcmp(status(),"Listening"));
    assert(feed(G_STATE("asking"))==0&&feed(G_STATE("answering"))==0&&feed(G_STATE("listening"))==0);
    static const struct {const char *frame,*text;} notices[]={
        {G_NOTICE("busy"),"That conversation is busy"},{G_NOTICE("not_sent"),"Not sent"},
        {G_NOTICE("send_uncertain"),"Could not confirm it was sent"},{G_NOTICE("reply_unavailable"),"No reply yet"},
        {G_NOTICE("idle_timeout"),"Mic off (quiet)"},{G_NOTICE("time_limit"),"Mic off (time limit)"}};
    for(size_t i=0;i<sizeof(notices)/sizeof(*notices);i++){
        conversing();feed(G_STATE("asking"));
        assert(feed(notices[i].frame)==BR_ACT_NOTICE&&m.phase==BR_CONVERSING); /* non-fatal */
        assert(!strcmp(status(),notices[i].text));
        bridge_notice_expire(&m);assert(!strcmp(status(),"Asking Unified...")); /* shown briefly */
        assert(feed(AUDIO)==BR_ACT_PLAY);
    }
    /* Unknown state/code values: ignored, conversation stays open and unchanged. */
    conversing();feed(G_STATE("answering"));unsigned ignored=m.ignored;
    assert(feed(G_STATE("dreaming"))==0&&m.phase==BR_CONVERSING&&m.talk==BR_TALK_ANSWERING&&m.ignored==ignored+1);
    assert(feed(G_NOTICE("something_new"))==0&&m.phase==BR_CONVERSING&&!m.notice_visible&&m.ignored==ignored+2);
    assert(feed(G_STATE(""))==0&&feed(G_NOTICE(""))==0&&m.phase==BR_CONVERSING);
    assert(!strcmp(status(),"Speaking")&&feed(AUDIO)==BR_ACT_PLAY);
    /* A repeated start/began carries nothing and is dropped. */
    assert(feed(STARTED)==0&&feed(G_BEGAN)==0&&m.phase==BR_CONVERSING);

    /* Mic off at each stage: one stop, audio muted, nothing else to send. */
    for(unsigned stage=0;stage<6;stage++){
        if(stage==0){bridge_init(&m);bridge_command(&m,BR_CMD_CONVERSE,0);}
        else if(stage==1){bridge_init(&m);bridge_command(&m,BR_CMD_CONVERSE,0);feed(STARTED);}
        else{conversing();if(stage>=4)feed(G_STATE("asking"));if(stage==5)feed(G_STATE("answering"));
             if(stage==3)feed(G_NOTICE("busy"));}
        unsigned epoch=m.audio_epoch;
        assert(bridge_command(&m,BR_CMD_STOP,0)==(BR_ACT_STOP|BR_ACT_MUTE));
        assert(m.phase==BR_CLOSING&&m.audio_epoch>epoch&&!strcmp(status(),"Mic off"));
        assert(bridge_encode(&m,BR_ACT_STOP,wire,sizeof(wire))>0&&!strcmp(wire,"{\"type\":\"pet.stop\"}"));
        assert(bridge_command(&m,BR_CMD_STOP,0)==0);                  /* once */
        assert(feed(AUDIO)==0&&feed(G_STATE("asking"))==0&&feed(G_BEGAN)==0&&feed(STARTED)==0);
        assert(bridge_command(&m,BR_CMD_CONVERSE,0)==0&&bridge_command(&m,BR_CMD_CONNECT,0)==0); /* never restarts itself */
        assert(feed(CLOSED)==(BR_ACT_MUTE|BR_ACT_CLOSE)&&m.phase==BR_FINISHED&&!strcmp(status(),"Mic off"));
        bridge_view(&m,&v);assert(!v.can_stop&&!v.can_send&&!v.can_finish_capture);
    }
    /* Gateway close: a normal end, with the host's stated reason when it gave one. */
    conversing();assert(feed(CLOSED)==(BR_ACT_MUTE|BR_ACT_CLOSE)&&m.phase==BR_FINISHED&&!m.code[0]);
    assert(!strcmp(status(),"Mic off (host ended the call)")&&feed(CLOSED)==0&&feed(AUDIO)==0);
    conversing();feed(G_NOTICE("idle_timeout"));feed(CLOSED);assert(!strcmp(status(),"Mic off (quiet)"));
    conversing();feed(G_NOTICE("busy"));feed(G_NOTICE("time_limit"));feed(CLOSED);
    assert(!strcmp(status(),"Mic off (time limit)"));
    conversing();feed(G_NOTICE("not_sent"));feed(CLOSED);assert(!strcmp(status(),"Mic off (host ended the call)"));
    /* pet.error: existing fixed handling, at any stage; the call ends. */
    conversing();feed(G_STATE("asking"));
    assert(feed("{\"type\":\"pet.error\",\"code\":\"busy\"}")==(BR_ACT_MUTE|BR_ACT_CLOSE)&&m.phase==BR_CLOSING);
    assert(!strcmp(status(),"That conversation is busy")&&feed(AUDIO)==0);
    conversing();feed("{\"type\":\"pet.error\",\"code\":\"reply_unavailable\"}");
    assert(!strcmp(status(),"Host error: reply_unavailable"));
    bridge_init(&m);bridge_command(&m,BR_CMD_CONVERSE,0);
    assert(feed("{\"type\":\"pet.error\",\"code\":\"model_mismatch\"}")==(BR_ACT_MUTE|BR_ACT_CLOSE));
    rejects("{\"type\":\"pet.error\",\"code\":\"anything_else\"}"); /* unchanged strict list */
    /* The host's reason for ending is kept even when it arrives while closing. */
    conversing();bridge_command(&m,BR_CMD_STOP,0);
    assert(feed(G_NOTICE("idle_timeout"))==0&&m.end_notice==BR_NOTICE_IDLE_TIMEOUT&&!m.notice_visible);
    /* One-turn frames do not belong to a conversation, and never start a
     * capture, a preview, a confirmation or playback there. */
    static const char *const turn_frames[]={
        "{\"type\":\"pet.turn.began\",\"turn_id\":\"" TURN "\",\"safe_session_label\":\"Existing\",\"session_id_short\":\"23133f96\"}",
        "{\"type\":\"pet.turn.preview\",\"turn_id\":\"" TURN "\",\"preview_revision\":1,\"text\":\"hello\",\"text_sha256\":\"" HASH "\"}",
        "{\"type\":\"pet.turn.send_result\",\"turn_id\":\"" TURN "\",\"state\":\"accepted\"}",
        "{\"type\":\"pet.reply.started\",\"turn_id\":\"" TURN "\",\"rendering\":\"spoken_rendering_not_verbatim\"}",
        "{\"type\":\"pet.reply.done\",\"turn_id\":\"" TURN "\",\"state\":\"completed\"}"};
    for(size_t i=0;i<sizeof(turn_frames)/sizeof(*turn_frames);i++){
        conversing();
        assert(feed(turn_frames[i])==(BR_ACT_MUTE|BR_ACT_CLOSE)&&m.phase==BR_CLOSING);
        assert(!m.confirmed&&!m.preview_seen&&!m.text[0]&&m.send==BR_SEND_NONE&&m.reply==BR_REPLY_NONE);
        assert(bridge_command(&m,BR_CMD_CONFIRM,1)==0&&bridge_command(&m,BR_CMD_FINISH_CAPTURE,0)==0);
        assert(bridge_encode(&m,BR_ACT_CONFIRM,wire,sizeof(wire))<0&&bridge_encode(&m,BR_ACT_FINISH_CAPTURE,wire,sizeof(wire))<0);
        assert(!strcmp(status(),"Host sent an unexpected message"));
    }
    /* No conversation phase accepts the one-turn commands or their frames. */
    conversing();
    assert(bridge_command(&m,BR_CMD_CONFIRM,0)==0&&bridge_command(&m,BR_CMD_FINISH_CAPTURE,0)==0&&m.phase==BR_CONVERSING);
    assert(bridge_encode(&m,BR_ACT_CONFIRM,wire,sizeof(wire))<0&&bridge_encode(&m,BR_ACT_FINISH_CAPTURE,wire,sizeof(wire))<0);
    /* Conversation frames mean nothing to a one-turn model. */
    capture();ignored=m.ignored;
    assert(feed(G_BEGAN)==0&&feed(G_STATE("asking"))==0&&feed(G_NOTICE("busy"))==0&&m.ignored==ignored+3&&m.phase==BR_SETTLING);
    /* A second conversation after the first ended: a fresh model, a fresh begin. */
    conversing();feed(G_STATE("answering"));feed(G_NOTICE("time_limit"));feed(CLOSED);assert(m.phase==BR_FINISHED);
    conversing();assert(m.talk==BR_TALK_LISTENING&&m.end_notice==BR_NOTICE_NONE&&!m.wire_closed&&!m.code[0]);
    assert(!strcmp(status(),"Listening")&&feed(AUDIO)==BR_ACT_PLAY&&feed(G_STATE("asking"))==0);
    puts("PASS: conversation mode golden frames (10 parsed, pet.converse.begin emitted byte-exact), states, notices, unknown values ignored, malformed refused, mic off at each stage, gateway close, pet.error, one-turn frames refused, second conversation");
}
int main(void) {
    bridge_event_t e;size_t points;bridge_view_t v;
    assert(bridge_uuid_valid(TURN));assert(!bridge_uuid_valid("123e4567-e89b-12d3-a456-426614174000"));
    assert(bridge_utf8("\xF0\x9F\x90\x88",4,&points)&&points==1);
    assert(!bridge_utf8("\xC0\x80",2,&points));assert(!bridge_utf8("\xED\xA0\x80",3,&points));
    const char *bad[]={"{}","[]","\v{\"type\":\"session.started\"}","\f{\"type\":\"session.started\"}",
        "{\"type\":\"session.started\",}","{\"type\":\"session.started\",\"type\":\"session.closed\"}",
        "{\"type\":\"session.started\",\"session\":{}}","{\"type\":\"session.started\"} trailing",
        "{\"type\":\"pet.turn.preview\",\"turn_id\":\"" TURN "\",\"preview_revision\":true,\"text\":\"hello\",\"text_sha256\":\"" HASH "\"}",
        "{\"type\":\"pet.turn.preview\",\"turn_id\":\"" TURN "\",\"preview_revision\":01,\"text\":\"hello\",\"text_sha256\":\"" HASH "\"}",
        "{\"type\":\"pet.turn.preview\",\"turn_id\":\"" TURN "\",\"preview_revision\":1.0,\"text\":\"hello\",\"text_sha256\":\"" HASH "\"}",
        "{\"type\":\"pet.turn.preview\",\"turn_id\":\"" TURN "\",\"preview_revision\":1,\"text\":\"wrong\",\"text_sha256\":\"" HASH "\"}",
        "{\"type\":\"pet.turn.preview\",\"turn_id\":\"" TURN "\",\"preview_revision\":1,\"text\":\"hello\\u0000hidden\",\"text_sha256\":\"" HASH "\"}",
        "{\"type\":\"session.output_audio.delta\",\"delta\":\"AR==\"}","{\"type\":\"session.output_audio.delta\",\"delta\":\"AQ==\"}"};
    for(size_t i=0;i<sizeof(bad)/sizeof(*bad);i++)assert(!bridge_parse(bad[i],strlen(bad[i]),&e));
    const char *valid_audio="{\"type\":\"session.output_audio.delta\",\"delta\":\"AQI=\"}";
    assert(bridge_parse(valid_audio,strlen(valid_audio),&e));
    bridge_event_clear(&e);
    preview();char command[256];
    assert(bridge_command(&m,BR_CMD_CONFIRM,2)==0&&m.phase==BR_PREVIEW);
    assert(bridge_command(&m,BR_CMD_CONFIRM,1)==BR_ACT_CONFIRM);
    assert(bridge_encode(&m,BR_ACT_CONFIRM,command,sizeof(command))>0&&strstr(command,HASH));
    assert(bridge_command(&m,BR_CMD_CONFIRM,1)==0);
    preview();bridge_command(&m,BR_CMD_CONFIRM,1);
    feed("{\"type\":\"pet.turn.preview_invalidated\",\"turn_id\":\"" TURN "\",\"reason\":\"late_transcript\"}");
    assert(m.send==BR_SEND_UNKNOWN);
    feed("{\"type\":\"pet.turn.send_result\",\"turn_id\":\"" TURN "\",\"state\":\"accepted\"}");
    feed("{\"type\":\"session.closed\"}");
    bridge_view(&m,&v);assert(v.send==BR_SEND_UNKNOWN&&!v.can_send);
    preview();bridge_command(&m,BR_CMD_CONFIRM,1);
    feed("{\"type\":\"pet.turn.preview\",\"turn_id\":\"" TURN "\",\"preview_revision\":2,\"text\":\"hello\",\"text_sha256\":\"" HASH "\"}");
    feed("{\"type\":\"session.closed\"}");
    assert(m.send==BR_SEND_UNKNOWN&&!m.text[0]);
    preview();bridge_command(&m,BR_CMD_CONFIRM,1);
    feed("{\"type\":\"pet.turn.send_result\",\"turn_id\":\"" TURN "\",\"state\":\"accepted\"}");
    assert(m.send==BR_SEND_ACCEPTED&&!m.text[0]&&!m.display[0]);
    assert(feed("{\"type\":\"session.output_audio.delta\",\"delta\":\"AQI=\"}")==0);
    assert(feed("{\"type\":\"pet.reply.started\",\"turn_id\":\"" TURN "\",\"rendering\":\"spoken_rendering_not_verbatim\"}")==0);
    assert(feed("{\"type\":\"session.output_audio.delta\",\"delta\":\"AQI=\"}")==BR_ACT_PLAY);
    unsigned epoch=m.audio_epoch;
    assert(bridge_command(&m,BR_CMD_STOP,0)==(BR_ACT_STOP|BR_ACT_MUTE));
    assert(m.audio_epoch>epoch&&m.reply==BR_REPLY_STOPPED);
    assert(feed("{\"type\":\"session.output_audio.delta\",\"delta\":\"AQI=\"}")==0);
    assert(bridge_command(&m,BR_CMD_STOP,0)==0);
    feed("{\"type\":\"session.closed\"}");assert(m.phase==BR_FINISHED&&m.reply==BR_REPLY_STOPPED);
    assert(bridge_command(&m,BR_CMD_STOP,0)==0);
    preview();
    assert(feed("{\"type\":\"pet.turn.preview_invalidated\",\"turn_id\":\"" TURN "\",\"reason\":\"late_transcript\"}")&(BR_ACT_MUTE|BR_ACT_CLOSE));
    feed("{\"type\":\"pet.turn.preview\",\"turn_id\":\"" TURN "\",\"preview_revision\":2,\"text\":\"hello\",\"text_sha256\":\"" HASH "\"}");
    assert(bridge_command(&m,BR_CMD_CONFIRM,2)==0&&!m.text[0]);
    preview();bridge_command(&m,BR_CMD_CONFIRM,1);
    feed("{\"type\":\"pet.turn.send_result\",\"turn_id\":\"" TURN "\",\"state\":\"uncertain\"}");
    bridge_view(&m,&v);assert(v.send==BR_SEND_UNKNOWN&&!strcmp(v.status,"Unknown; check Unified. No retry."));
    assert(bridge_command(&m,BR_CMD_CONFIRM,1)==0);
    preview();bridge_command(&m,BR_CMD_CONFIRM,1);
    feed("{\"type\":\"pet.turn.send_result\",\"turn_id\":\"" TURN "\",\"state\":\"accepted\"}");
    feed("{\"type\":\"pet.reply.started\",\"turn_id\":\"" TURN "\",\"rendering\":\"spoken_rendering_not_verbatim\"}");
    epoch=m.audio_epoch;
    assert(feed("{\"type\":\"pet.reply.done\",\"turn_id\":\"" TURN "\",\"state\":\"completed\"}")==BR_ACT_DRAIN);
    assert(m.audio_epoch==epoch&&m.draining);
    assert(feed("{\"type\":\"session.closed\"}")==BR_ACT_DRAIN&&m.audio_epoch==epoch);
    assert(bridge_playback_drained(&m)==(BR_ACT_MUTE|BR_ACT_CLOSE));
    assert(m.phase==BR_FINISHED&&m.reply==BR_REPLY_RECEIVED&&!m.draining);
    assert(bridge_command(&m,BR_CMD_STOP,0)==0&&m.reply==BR_REPLY_RECEIVED);
    puts("PASS: production bridge strict wire, UTF8/hash, one sealed preview, once-only confirm, audio epochs, Stop races, no false Done/unknown retry");
    conversation_cases();
}
