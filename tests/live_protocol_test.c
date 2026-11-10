#include <assert.h>
#include <stdio.h>
#include "live_protocol.h"
#include "assistant_config.h"
int main(void) {
 char out[32];live_message_t m={0};
 assert(live_message_feed(&m,out,sizeof(out),1,true,0,7,"{\"a",3)==0);
 assert(live_message_feed(&m,out,sizeof(out),1,true,3,7,"\":1}",4)==1);
 assert(!strcmp(out,"{\"a\":1}"));
 /* A fragmented text message, TCP splits, and an interleaved ping. */
 assert(live_message_feed(&m,out,sizeof(out),1,false,0,3,"abc",3)==0);
 assert(live_message_feed(&m,out,sizeof(out),9,true,0,1,"!",1)==0);
 assert(live_message_feed(&m,out,sizeof(out),0,true,0,3,"d",1)==0);
 assert(live_message_feed(&m,out,sizeof(out),0,true,1,3,"ef",2)==1);
 assert(!strcmp(out,"abcdef"));
 assert(live_message_feed(&m,out,sizeof(out),0,true,0,1,"x",1)==-1);
 m=(live_message_t){0};assert(live_message_feed(&m,out,4,1,true,0,4,"1234",4)==-1);
 m=(live_message_t){0};assert(live_message_feed(&m,out,sizeof(out),1,true,0,7,"abc",3)==0);
 assert(live_message_feed(&m,out,sizeof(out),1,true,4,7,"def",3)==-1);
 m=(live_message_t){0};assert(live_message_feed(&m,out,sizeof(out),2,true,0,1,"x",1)==-1);
 puts(PET_LIVE_START);
 return 0;
}
