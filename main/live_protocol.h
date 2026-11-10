#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <string.h>

/* Bounds both TCP-buffer splits and actual RFC6455 continuation frames.
 * Control frames may be interleaved without changing the JSON message. */
typedef struct { size_t used, frame_offset; bool active; } live_message_t;
static inline int live_message_feed(live_message_t *m,char *out,size_t capacity,
 unsigned opcode,bool fin,size_t offset,size_t total,const char *bytes,size_t length) {
 if(opcode>=8)return 0;
 if(opcode!=0 && opcode!=1)return -1;
 if(offset==0) {
  if(opcode==1){if(m->active)return -1;m->used=0;m->active=true;}
  else if(!m->active)return -1;
  m->frame_offset=0;
 }
 if(!m->active || offset!=m->frame_offset || offset>total || length>total-offset ||
    m->used>=capacity || length>=capacity-m->used)return -1;
 memcpy(out+m->used,bytes,length);m->used+=length;m->frame_offset+=length;
 if(offset+length==total && fin){out[m->used]=0;m->active=false;return 1;}
 return 0;
}
