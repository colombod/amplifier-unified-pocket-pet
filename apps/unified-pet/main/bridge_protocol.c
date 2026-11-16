#include "bridge_protocol.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "mbedtls/sha256.h"
static void wipe(void *p,size_t n){volatile unsigned char *b=p;while(n--)*b++=0;}
static bool hex(const char *s,size_t n){if(!s||strlen(s)!=n)return false;for(size_t i=0;i<n;i++)if(!((s[i]>='0'&&s[i]<='9')||(s[i]>='a'&&s[i]<='f')))return false;return true;}
bool bridge_uuid_valid(const char *s){
    if(!s||strlen(s)!=36||s[14]!='4'||!strchr("89ab",s[19]))return false;
    for(unsigned i=0;i<36;i++)if(i==8||i==13||i==18||i==23){if(s[i]!='-')return false;}else if(!((s[i]>='0'&&s[i]<='9')||(s[i]>='a'&&s[i]<='f')))return false;
    return true;
}
static bool point(const unsigned char *s,size_t n,size_t *used,uint32_t *value){
    if(!n||s[0]==0)return false;
    unsigned k;uint32_t c,min;
    if(s[0]<0x80){*used=1;*value=s[0];return true;}
    if(s[0]>=0xc2&&s[0]<=0xdf){k=2;c=s[0]&31;min=0x80;}
    else if(s[0]>=0xe0&&s[0]<=0xef){k=3;c=s[0]&15;min=0x800;}
    else if(s[0]>=0xf0&&s[0]<=0xf4){k=4;c=s[0]&7;min=0x10000;}else return false;
    if(n<k)return false;
    for(unsigned i=1;i<k;i++){if((s[i]&0xc0)!=0x80)return false;c=(c<<6)|(s[i]&63);}
    if(c<min||c>0x10ffff||(c>=0xd800&&c<=0xdfff))return false;
    *used=k;*value=c;return true;
}
bool bridge_utf8(const char *s,size_t bytes,size_t *points){
    if(!s)return false;
    size_t at=0,count=0,k;uint32_t c;
    while(at<bytes){if(!point((const unsigned char*)s+at,bytes-at,&k,&c))return false;at+=k;count++;}
    if(points)*points=count;
    return true;
}
static bool display(const char *s,char *out,size_t cap){
    size_t n=strlen(s),at=0,w=0,k;uint32_t c;
    while(at<n){
        if(!point((const unsigned char*)s+at,n-at,&k,&c))return false;
        char escaped[11];const char *src=s+at;size_t length=k;
        if(!((c>=32&&c<=126&&c!='<')||c=='\n')){int z=snprintf(escaped,sizeof(escaped),"<U+%04X>",(unsigned)c);if(z<0)return false;src=escaped;length=(size_t)z;}
        if(w+length>=cap)return false;
        memcpy(out+w,src,length);w+=length;at+=k;
    }
    out[w]=0;return true;
}
static void whitespace(const char *s,size_t n,size_t *i){
    while(*i<n&&(s[*i]==' '||s[*i]=='\t'||s[*i]=='\r'||s[*i]=='\n'))(*i)++;
}
static bool json_string(const char *s,size_t n,size_t *i){
    if(*i>=n||s[(*i)++]!='"')return false;
    while(*i<n){
        unsigned char c=(unsigned char)s[(*i)++];
        if(c=='"')return true;
        if(c<32)return false;
        if(c=='\\'){
            if(*i>=n)return false;
            char escape=s[(*i)++];
            if(escape=='u'){
                if(n-*i<4||!memcmp(s+*i,"0000",4))return false;
                for(unsigned k=0;k<4;k++)if(!strchr("0123456789abcdefABCDEF",s[*i+k]))return false;
                *i+=4;
            }else if(!strchr("\"\\/bfnrt",escape))return false;
        }
    }
    return false;
}
/* This protocol has only flat objects of strings and unsigned integer revision.
 * Enforce the grammar BEFORE cJSON (which accepts broader whitespace/numbers).
 * No nested containers means bounded parser depth and no permissive-number CAS. */
static bool raw_strings(const char *s,size_t n){
    size_t i=0;unsigned fields=0;
    whitespace(s,n,&i);if(i>=n||s[i++]!='{')return false;
    whitespace(s,n,&i);
    if(i<n&&s[i]=='}'){i++;whitespace(s,n,&i);return i==n;}
    while(i<n){
        if(++fields>8||!json_string(s,n,&i))return false;
        whitespace(s,n,&i);if(i>=n||s[i++]!=':')return false;
        whitespace(s,n,&i);if(i>=n)return false;
        if(s[i]=='"'){if(!json_string(s,n,&i))return false;}
        else{
            if(s[i]<'0'||s[i]>'9')return false;
            bool zero=s[i]=='0';size_t start=i;
            while(i<n&&s[i]>='0'&&s[i]<='9')i++;
            if((zero&&i-start>1)||i-start>10)return false;
        }
        whitespace(s,n,&i);if(i>=n)return false;
        if(s[i]=='}'){i++;whitespace(s,n,&i);return i==n;}
        if(s[i++]!=',')return false;
        whitespace(s,n,&i);
    }
    return false;
}
static const char *str(const cJSON *j,const char *key){const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);return cJSON_IsString(v)?v->valuestring:NULL;}
static bool shape(const cJSON *j,const char *keys){
    if(!cJSON_IsObject(j))return false;
    for(const cJSON *a=j->child;a;a=a->next){
        if(!a->string)return false;
        bool found=false;const char *p=keys;
        while(*p){const char *end=strchr(p,'|');size_t n=end?(size_t)(end-p):strlen(p);if(strlen(a->string)==n&&!memcmp(a->string,p,n)){found=true;break;}if(!end)break;p=end+1;}
        if(!found)return false;
        for(const cJSON *b=a->next;b;b=b->next)if(b->string&&!strcmp(a->string,b->string))return false;
    }
    return true;
}
static bool oneof(const char *s,const char *list){
    if(!s)return false;
    while(*list){const char *e=strchr(list,'|');size_t n=e?(size_t)(e-list):strlen(list);if(strlen(s)==n&&!memcmp(s,list,n))return true;if(!e)return false;list=e+1;}
    return false;
}
static int b64(char c){static const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";const char *p=strchr(alphabet,c);return c&&p?(int)(p-alphabet):-1;}
static bool audio(const char *s){
    if(!s)return false;
    size_t n=strlen(s);if(!n||n>6400||n%4)return false;
    size_t pads=(s[n-1]=='=')+(s[n-2]=='=');
    for(size_t i=0;i<n-pads;i++)if(b64(s[i])<0)return false;
    for(size_t i=n-pads;i<n;i++)if(s[i]!='=')return false;
    size_t bytes=n/4*3-pads;
    return bytes&&bytes%2==0&&(pads!=1||(b64(s[n-2])&3)==0)&&(pads!=2||(b64(s[n-3])&15)==0);
}
static bool target(const cJSON *j,bridge_event_t *e){
    e->label=str(j,"safe_session_label");e->short_id=str(j,"session_id_short");size_t points=0;
    bool valid=e->label&&bridge_utf8(e->label,strlen(e->label),&points)&&points>0&&points<=40&&hex(e->short_id,8);
    if(valid)for(const unsigned char *p=(const unsigned char*)e->label;*p;p++)if(*p<32||*p==127)valid=false;
    return valid;
}
static void clear_strings(cJSON *j){for(cJSON *v=j;v;v=v->next){if(v->child)clear_strings(v->child);if(cJSON_IsString(v)&&v->valuestring)wipe(v->valuestring,strlen(v->valuestring));if(v->string)wipe(v->string,strlen(v->string));}}
void bridge_event_clear(bridge_event_t *e){if(!e)return;if(e->document){clear_strings(e->document);cJSON_Delete(e->document);}memset(e,0,sizeof(*e));}
bool bridge_parse(const char *json,size_t n,bridge_event_t *e){
    if(!e)return false;
    memset(e,0,sizeof(*e));
    if(!json||!n||n>BR_FRAME_BYTES||!raw_strings(json,n)||!bridge_utf8(json,n,NULL))return false;
    char *copy=malloc(n+1);if(!copy)return false;memcpy(copy,json,n);copy[n]=0;
    const char *end=NULL;cJSON *j=cJSON_ParseWithLengthOpts(copy,n+1,&end,true);
    wipe(copy,n);free(copy);e->document=j;
    const char *type=str(j,"type");const char *keys=NULL;bool valid=false;
    if(!type)goto done;
    if(!strcmp(type,"session.started")){e->kind=BR_EV_STARTED;keys="type";valid=true;}
    else if(!strcmp(type,"session.closed")){e->kind=BR_EV_CLOSED;keys="type";valid=true;}
    else if(!strcmp(type,"pet.turn.began")){
        e->kind=BR_EV_BEGAN;keys="type|turn_id|safe_session_label|session_id_short";
        valid=target(j,e);
    }else if(!strcmp(type,"pet.converse.began")){
        /* Same target value rules as pet.turn.began; a conversation has no turn_id. */
        e->kind=BR_EV_CONVERSE_BEGAN;keys="type|safe_session_label|session_id_short";
        valid=target(j,e);
    }else if(!strcmp(type,"pet.converse.state")){
        /* Any string is well formed: an unknown value is ignored by the model,
         * never a reason to close. A missing or non-string field is malformed. */
        e->kind=BR_EV_CONVERSE_STATE;keys="type|state";e->state=str(j,"state");valid=e->state!=NULL;
    }else if(!strcmp(type,"pet.converse.notice")){
        e->kind=BR_EV_CONVERSE_NOTICE;keys="type|code";e->code=str(j,"code");valid=e->code!=NULL;
    }else if(!strcmp(type,"pet.turn.preview")){
        e->kind=BR_EV_PREVIEW;keys="type|turn_id|preview_revision|text|text_sha256";
        e->text=str(j,"text");e->hash=str(j,"text_sha256");const cJSON *r=cJSON_GetObjectItemCaseSensitive(j,"preview_revision");size_t points=0;
        valid=e->text&&strlen(e->text)>0&&strlen(e->text)<=BR_TEXT_BYTES&&bridge_utf8(e->text,strlen(e->text),&points)&&points<=BR_TEXT_POINTS&&hex(e->hash,64)&&cJSON_IsNumber(r)&&isfinite(r->valuedouble)&&r->valuedouble>=1&&r->valuedouble<=2147483647&&r->valuedouble==(uint32_t)r->valuedouble;
        if(valid){unsigned char digest[32];char h[65];valid=mbedtls_sha256((const unsigned char*)e->text,strlen(e->text),digest,0)==0;for(unsigned i=0;i<32;i++)snprintf(h+i*2,3,"%02x",digest[i]);wipe(digest,sizeof(digest));valid=valid&&!strcmp(h,e->hash);e->revision=(uint32_t)r->valuedouble;}
    }else if(!strcmp(type,"pet.turn.preview_invalidated")){
        e->kind=BR_EV_INVALIDATED;keys="type|turn_id|reason";const char *reason=str(j,"reason");valid=reason&&strlen(reason)>0&&strlen(reason)<=64;if(valid)for(const char *p=reason;*p;p++)if((unsigned char)*p<32||(unsigned char)*p>126)valid=false;
    }else if(!strcmp(type,"pet.turn.send_result")){
        e->kind=BR_EV_SEND_RESULT;keys="type|turn_id|state";e->state=str(j,"state");valid=oneof(e->state,"accepted|rejected|uncertain");
    }else if(!strcmp(type,"pet.reply.started")){
        e->kind=BR_EV_REPLY_STARTED;keys="type|turn_id|rendering";valid=oneof(str(j,"rendering"),"spoken_rendering_not_verbatim");
    }else if(!strcmp(type,"pet.reply.done")){
        e->kind=BR_EV_REPLY_DONE;keys="type|turn_id|state";e->state=str(j,"state");valid=oneof(e->state,"completed|unavailable|stopped");
    }else if(!strcmp(type,"session.output_audio.delta")){
        e->kind=BR_EV_AUDIO;keys="type|delta|turn_id";e->audio=str(j,"delta");valid=audio(e->audio);
    }else if(!strcmp(type,"pet.error")){
        e->kind=BR_EV_ERROR;keys="type|code";e->code=str(j,"code");valid=oneof(e->code,"busy|unknown_command|model_mismatch|no_speech|unstable_transcript|expired|no_delegation|reply_unavailable|deadline|overflow|close_unconfirmed|invalid_preview");
    }
    if(!valid||!keys||!shape(j,keys)){valid=false;goto done;}
    e->turn=str(j,"turn_id");
    bool needs_turn=e->kind!=BR_EV_STARTED&&e->kind!=BR_EV_CLOSED&&e->kind!=BR_EV_ERROR&&e->kind!=BR_EV_AUDIO&&
        e->kind!=BR_EV_CONVERSE_BEGAN&&e->kind!=BR_EV_CONVERSE_STATE&&e->kind!=BR_EV_CONVERSE_NOTICE;
    if((needs_turn&&!e->turn)||(e->turn&&!bridge_uuid_valid(e->turn)))valid=false;
    /* Optional does not mean a wrong-typed supplied turn is accepted. */
    if(cJSON_GetObjectItemCaseSensitive(j,"turn_id")&&!e->turn)valid=false;
done:
    if(!valid){bridge_event_clear(e);return false;}return true;
}
void bridge_init(bridge_model_t *m){if(m){wipe(m,sizeof(*m));m->phase=BR_IDLE;}}
static void clear_preview(bridge_model_t *m){wipe(m->text,sizeof(m->text));wipe(m->display,sizeof(m->display));}
static unsigned close_model(bridge_model_t *m){
    if(m->confirmed&&m->send==BR_SEND_NONE)m->send=BR_SEND_UNKNOWN;
    clear_preview(m);m->draining=false;m->audio_epoch++;m->phase=BR_CLOSING;return BR_ACT_MUTE|BR_ACT_CLOSE;
}
unsigned bridge_command(bridge_model_t *m,bridge_command_t cmd,uint32_t revision){
    if(!m)return 0;
    if(cmd==BR_CMD_CONNECT&&m->phase==BR_IDLE){m->phase=BR_CONNECTING;return 0;}
    if(cmd==BR_CMD_CONVERSE&&m->phase==BR_IDLE){m->converse=true;m->phase=BR_CONNECTING;return 0;}
    if(cmd==BR_CMD_FINISH_CAPTURE&&m->phase==BR_CAPTURING){m->phase=BR_SETTLING;return BR_ACT_FINISH_CAPTURE;}
    if(cmd==BR_CMD_CONFIRM&&m->phase==BR_PREVIEW&&!m->confirmed&&revision==m->revision&&m->text[0]&&hex(m->hash,64)){
        m->confirmed=true;m->phase=BR_SENDING;return BR_ACT_CONFIRM;
    }
    if(cmd==BR_CMD_STOP&&m->draining){
        m->draining=false;m->audio_epoch++;m->phase=m->wire_closed?BR_FINISHED:BR_CLOSING;
        return BR_ACT_MUTE; /* never relabel an already received terminal reply */
    }
    if(cmd==BR_CMD_STOP&&m->phase!=BR_IDLE&&m->phase!=BR_FINISHED&&m->phase!=BR_ERROR&&m->phase!=BR_CLOSING&&!m->stop_sent){
        m->stop_sent=true;if(m->send==BR_SEND_ACCEPTED&&m->reply==BR_REPLY_NONE)m->reply=BR_REPLY_STOPPED;
        if(m->phase==BR_SENDING)m->send=BR_SEND_UNKNOWN;
        clear_preview(m);m->audio_epoch++;m->phase=BR_CLOSING;return BR_ACT_STOP|BR_ACT_MUTE;
    }
    return 0;
}
unsigned bridge_playback_drained(bridge_model_t *m){
    if(!m||!m->draining)return 0;
    m->draining=false;m->audio_epoch++;m->phase=m->wire_closed?BR_FINISHED:BR_CLOSING;
    return BR_ACT_MUTE|BR_ACT_CLOSE;
}
void bridge_notice_expire(bridge_model_t *m){if(m)m->notice_visible=false;}
static bridge_notice_t notice_code(const char *s){
    static const char *const names[]={"busy","not_sent","send_uncertain","reply_unavailable","idle_timeout","time_limit"};
    for(unsigned i=0;i<sizeof(names)/sizeof(*names);i++)if(!strcmp(s,names[i]))return (bridge_notice_t)(i+1);
    return BR_NOTICE_NONE;
}
/* Conversation mode. The host owns recognition, sending and replies; the device
 * only streams audio and shows what it is told. There is no preview, confirm or
 * reply bookkeeping here, so nothing in this path can authorize or repeat a send. */
static unsigned converse_step(bridge_model_t *m,const bridge_event_t *e){
    if(e->kind==BR_EV_CLOSED){
        if(m->wire_closed)return 0;
        /* The host closing (its quiet/time limits included) is a normal end. */
        m->wire_closed=true;m->audio_epoch++;m->phase=BR_FINISHED;return BR_ACT_MUTE|BR_ACT_CLOSE;
    }
    if(e->kind==BR_EV_ERROR){snprintf(m->code,sizeof(m->code),"%s",e->code);return close_model(m);}
    if(e->kind==BR_EV_CONVERSE_NOTICE&&(m->phase==BR_CONVERSING||m->phase==BR_CLOSING)){
        bridge_notice_t notice=notice_code(e->code);
        if(notice==BR_NOTICE_NONE){m->ignored++;return 0;} /* unknown code: ignored, stay open */
        if(notice==BR_NOTICE_IDLE_TIMEOUT||notice==BR_NOTICE_TIME_LIMIT)m->end_notice=notice;
        if(m->phase!=BR_CONVERSING)return 0; /* already ending: remember why, show nothing new */
        m->notice=notice;m->notice_visible=true;return BR_ACT_NOTICE;
    }
    if(m->phase==BR_CLOSING||m->phase==BR_FINISHED||m->phase==BR_ERROR){m->ignored++;return 0;}
    if(e->kind==BR_EV_STARTED&&m->phase==BR_CONNECTING){m->phase=BR_BEGINNING;return BR_ACT_BEGIN;}
    if(e->kind==BR_EV_CONVERSE_BEGAN&&m->phase==BR_BEGINNING){
        memcpy(m->short_id,e->short_id,9);
        if(!display(e->label,m->label,sizeof(m->label)))return close_model(m);
        m->talk=BR_TALK_LISTENING;m->audio_epoch++;m->phase=BR_CONVERSING;return BR_ACT_CAPTURE;
    }
    if(e->kind==BR_EV_CONVERSE_STATE&&m->phase==BR_CONVERSING){
        if(!strcmp(e->state,"listening"))m->talk=BR_TALK_LISTENING;
        else if(!strcmp(e->state,"asking"))m->talk=BR_TALK_ASKING;
        else if(!strcmp(e->state,"answering"))m->talk=BR_TALK_ANSWERING;
        else m->ignored++; /* unknown value: keep the last known state, stay open */
        return 0;
    }
    if(e->kind==BR_EV_AUDIO&&m->phase==BR_CONVERSING)return BR_ACT_PLAY;
    /* Audio before the conversation began, a repeated start/began, or a state
     * or notice out of place carries no authority and is dropped. */
    if(e->kind==BR_EV_AUDIO||e->kind==BR_EV_STARTED||e->kind==BR_EV_CONVERSE_BEGAN||
       e->kind==BR_EV_CONVERSE_STATE||e->kind==BR_EV_CONVERSE_NOTICE){m->ignored++;return 0;}
    /* One-turn frames (began/preview/send result/reply) do not belong to a
     * conversation: never capture, preview or play on them. */
    snprintf(m->code,sizeof(m->code),"protocol");return close_model(m);
}
unsigned bridge_step(bridge_model_t *m,const bridge_event_t *e){
    if(!m||!e)return 0;
    if(m->converse)return converse_step(m,e);
    if(e->turn&&m->turn[0]&&strcmp(e->turn,m->turn)){m->ignored++;return 0;}
    if(e->kind==BR_EV_CLOSED){
        if(m->wire_closed)return 0;
        m->wire_closed=true;
        if(m->draining)return BR_ACT_DRAIN; /* socket closure is not speaker drain */
        if(m->phase==BR_SENDING)m->send=BR_SEND_UNKNOWN;
        if(m->send==BR_SEND_ACCEPTED&&m->reply==BR_REPLY_NONE)m->reply=BR_REPLY_UNAVAILABLE;
        clear_preview(m);m->audio_epoch++;m->phase=BR_FINISHED;return BR_ACT_MUTE;
    }
    if(e->kind==BR_EV_ERROR){
        snprintf(m->code,sizeof(m->code),"%s",e->code);
        if(m->phase==BR_SENDING)m->send=BR_SEND_UNKNOWN;
        if(m->send==BR_SEND_ACCEPTED&&m->reply==BR_REPLY_NONE)m->reply=BR_REPLY_UNAVAILABLE;
        return close_model(m);
    }
    if(m->phase==BR_CLOSING||m->phase==BR_FINISHED||m->phase==BR_ERROR){m->ignored++;return 0;}
    if(e->kind==BR_EV_STARTED&&m->phase==BR_CONNECTING){m->phase=BR_BEGINNING;return BR_ACT_BEGIN;}
    if(e->kind==BR_EV_BEGAN&&m->phase==BR_BEGINNING){
        memcpy(m->turn,e->turn,37);memcpy(m->short_id,e->short_id,9);
        if(!display(e->label,m->label,sizeof(m->label)))return close_model(m);
        m->phase=BR_CAPTURING;return BR_ACT_CAPTURE;
    }
    if(e->kind==BR_EV_PREVIEW&&m->phase==BR_SETTLING&&!m->preview_seen){
        m->preview_seen=true;m->revision=e->revision;memcpy(m->hash,e->hash,65);
        memcpy(m->text,e->text,strlen(e->text)+1);
        if(!display(e->text,m->display,sizeof(m->display)))return close_model(m);
        m->phase=BR_PREVIEW;return 0;
    }
    if(e->kind==BR_EV_PREVIEW&&m->preview_seen)return close_model(m);
    if(e->kind==BR_EV_INVALIDATED){snprintf(m->code,sizeof(m->code),"invalid_preview");return close_model(m);}
    if(e->kind==BR_EV_SEND_RESULT&&m->phase==BR_SENDING){
        clear_preview(m);
        if(!strcmp(e->state,"accepted")){m->send=BR_SEND_ACCEPTED;m->phase=BR_AWAIT_REPLY;return 0;}
        m->send=!strcmp(e->state,"uncertain")?BR_SEND_UNKNOWN:BR_SEND_REJECTED;
        return close_model(m);
    }
    if(e->kind==BR_EV_REPLY_STARTED&&m->phase==BR_AWAIT_REPLY&&m->send==BR_SEND_ACCEPTED){m->audio_epoch++;m->phase=BR_REPLYING;return 0;}
    if(e->kind==BR_EV_AUDIO&&m->phase==BR_REPLYING)return BR_ACT_PLAY;
    if(e->kind==BR_EV_REPLY_DONE&&m->send==BR_SEND_ACCEPTED&&(m->phase==BR_REPLYING||m->phase==BR_AWAIT_REPLY)){
        m->reply=!strcmp(e->state,"completed")?BR_REPLY_RECEIVED:!strcmp(e->state,"stopped")?BR_REPLY_STOPPED:BR_REPLY_UNAVAILABLE;
        if(m->reply==BR_REPLY_RECEIVED){clear_preview(m);m->draining=true;m->phase=BR_CLOSING;return BR_ACT_DRAIN;}
        return close_model(m);
    }
    m->ignored++;return 0;
}
const char *bridge_notice_text(bridge_notice_t notice){
    switch(notice){
    case BR_NOTICE_BUSY:return "That conversation is busy";
    case BR_NOTICE_NOT_SENT:return "Not sent";
    case BR_NOTICE_SEND_UNCERTAIN:return "Could not confirm it was sent";
    case BR_NOTICE_REPLY_UNAVAILABLE:return "No reply yet";
    case BR_NOTICE_IDLE_TIMEOUT:return "Mic off (quiet)";
    case BR_NOTICE_TIME_LIMIT:return "Mic off (time limit)";
    default:return "Mic off";
    }
}
static void converse_status(const bridge_model_t *m,char *out,size_t cap){
    const char *s="Mic off";
    if(m->code[0]){
        /* Fixed host codes only (validated list or a local constant). */
        if(!strcmp(m->code,"busy"))s="That conversation is busy";
        else if(!strcmp(m->code,"deadline"))s="Mic off (time limit)";
        else if(!strcmp(m->code,"protocol"))s="Host sent an unexpected message";
        else{snprintf(out,cap,"Host error: %s",m->code);return;}
    }else if(m->stop_sent)s="Mic off";
    else if(m->phase==BR_CONVERSING){
        if(m->notice_visible)s=bridge_notice_text(m->notice);
        else s=m->talk==BR_TALK_ASKING?"Asking Unified...":m->talk==BR_TALK_ANSWERING?"Speaking":"Listening";
    }else if(m->phase==BR_CONNECTING||m->phase==BR_BEGINNING)s="Connecting...";
    else if(m->end_notice!=BR_NOTICE_NONE)s=bridge_notice_text(m->end_notice);
    else if(m->wire_closed)s="Mic off (host ended the call)";
    else if(m->phase==BR_CLOSING)s="Closing...";
    snprintf(out,cap,"%s",s);
}
void bridge_view(const bridge_model_t *m,bridge_view_t *v){
    if(!m||!v)return;
    memset(v,0,sizeof(*v));v->phase=m->phase;v->send=m->send;v->reply=m->reply;v->revision=m->revision;v->audio_epoch=m->audio_epoch;
    v->can_finish_capture=m->phase==BR_CAPTURING;v->can_send=m->phase==BR_PREVIEW&&!m->confirmed;v->can_stop=(m->phase>BR_IDLE&&m->phase<BR_CLOSING)||m->draining;
    v->converse=m->converse;v->talk=m->talk;
    snprintf(v->target,sizeof(v->target),"To %s #%s",m->label,m->short_id);
    if(m->converse){converse_status(m,v->status,sizeof(v->status));return;}
    const char *s="Ready";
    if(m->send==BR_SEND_UNKNOWN)s="Unknown; check Unified. No retry.";
    else if(m->reply==BR_REPLY_UNAVAILABLE)s="Reply unavailable; delivery not verified.";
    else if(m->reply==BR_REPLY_STOPPED)s="Stopped. Unified work is not cancelled.";
    else if(m->reply==BR_REPLY_RECEIVED)s=m->draining?"Reply received; draining speaker.":"Reply ended.";
    else if(m->send==BR_SEND_REJECTED)s="Send rejected.";
    else if(m->code[0])s=m->code;
    else if(m->phase==BR_PREVIEW)s="Check exact preview, then tap Send.";
    else if(m->phase==BR_CAPTURING)s="Listening. Tap Done talking.";
    else if(m->phase==BR_REPLYING)s="Spoken rendering, not verbatim. Stop to end.";
    else if(m->phase==BR_SENDING)s="Sending once...";
    else if(m->send==BR_SEND_ACCEPTED)s="Accepted. Waiting for Unified reply.";
    else if(m->phase==BR_SETTLING)s="Preparing preview...";
    else if(m->phase==BR_CLOSING)s="Closing...";
    else if(m->phase==BR_CONNECTING||m->phase==BR_BEGINNING)s="Connecting to host bridge...";
    snprintf(v->status,sizeof(v->status),"%s",s);
}
int bridge_encode(const bridge_model_t *m,unsigned a,char *out,size_t cap){
    if(!m||!out||!cap)return -1;
    int n=-1;
    if(a==BR_ACT_BEGIN)n=snprintf(out,cap,m->converse?"{\"type\":\"pet.converse.begin\"}":"{\"type\":\"pet.turn.begin\"}");
    else if(a==BR_ACT_STOP)n=snprintf(out,cap,"{\"type\":\"pet.stop\"}");
    else if(a==BR_ACT_CLOSE)n=snprintf(out,cap,"{\"type\":\"session.close\"}");
    else if(a==BR_ACT_FINISH_CAPTURE&&bridge_uuid_valid(m->turn))n=snprintf(out,cap,"{\"type\":\"pet.turn.capture_stop\",\"turn_id\":\"%s\"}",m->turn);
    else if(a==BR_ACT_CONFIRM&&m->confirmed&&m->phase==BR_SENDING&&bridge_uuid_valid(m->turn)&&hex(m->hash,64)&&m->revision)n=snprintf(out,cap,"{\"type\":\"pet.turn.send_confirm\",\"turn_id\":\"%s\",\"text_sha256\":\"%s\",\"preview_revision\":%u}",m->turn,m->hash,(unsigned)m->revision);
    if(n<0||(size_t)n>=cap){out[0]=0;return -1;}return n;
}
