#include <assert.h>
#include "network_retry.h"
int main(void) {
 network_retry_t r={0};network_retry_failed(&r,1000);
 assert(r.delay_ms==5000 && !network_retry_due(&r,5999,false,false,false,false,0));
 assert(network_retry_due(&r,6000,false,false,false,false,0));
 assert(!network_retry_due(&r,6000,true,false,false,false,0));
 assert(!network_retry_due(&r,6000,false,true,false,false,0));
 assert(!network_retry_due(&r,6000,false,false,true,false,0));
 assert(!network_retry_due(&r,6000,false,false,false,true,5900));
 assert(network_retry_due(&r,65900,false,false,false,true,5900));
 for(int i=0;i<8;i++)network_retry_failed(&r,70000);
 assert(r.delay_ms==60000 && r.deadline==130000);
 network_retry_reset(&r);assert(!r.scheduled);network_retry_failed(&r,UINT32_MAX-2000);
 assert(!network_retry_due(&r,1000,false,false,false,false,0));
 assert(network_retry_due(&r,3000,false,false,false,false,0));
 network_retry_reset(&r);assert(!network_retry_due(&r,9000,false,false,false,false,0));
 return 0;
}
