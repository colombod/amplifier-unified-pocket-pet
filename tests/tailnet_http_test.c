#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "tailnet_http.h"
int main(void) {
    const char *good="HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}";
    tailnet_http_result_t r;
    char partial[256]={0};
    for(size_t i=0;i<=strlen(good);i++) {
        memcpy(partial,good,i);partial[i]=0;
        assert(tailnet_http_parse(partial,i,false,&r));
        assert(r.complete==(i==strlen(good)));
    }
    assert(r.status==401&&r.body==2); /* reachability, NOT authorization */
    assert(!tailnet_http_parse(good,strlen(good)-1,true,&r));
    const char *bad[]={
        "HTTP/1.1 302 Found\r\nContent-Length: 0\r\n\r\n",
        "HTTP/1.1 2000 Bad\r\n\r\n",
        "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
        "HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n",
        "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        "HTTP/1.1 200 OK\r\nContent-Length: 4097\r\n\r\n",
        "HTTP/1.1 200 OK\r\nbad header\r\n\r\n",
    };
    for(unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);i++)assert(!tailnet_http_parse(bad[i],strlen(bad[i]),true,&r));
    puts("PASS production HTTP framing: partial IO, 401-not-auth, truncation, redirect/duplicate/chunked/oversize rejection");
}