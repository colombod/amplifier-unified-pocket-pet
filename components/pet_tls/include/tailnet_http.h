#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <strings.h>
#include <stdlib.h>
#include <stdio.h>
/* Strict bounded HTTP/1 response framing, not an HTTP client or redirect engine.
 * Chunked/ambiguous framing is rejected rather than claimed as complete. */
typedef struct {int status;size_t headers,body;bool complete;} tailnet_http_result_t;
static inline bool tailnet_http_parse(const char *b,size_t n,bool eof,tailnet_http_result_t *out) {
    memset(out,0,sizeof(*out));
    if(n>4096||memchr(b,0,n))return false;
    const char *end=strstr(b,"\r\n\r\n");
    if(!end)return !eof;
    int status=0,consumed=0;
    if((strncmp(b,"HTTP/1.1 ",9)&&strncmp(b,"HTTP/1.0 ",9))||
       sscanf(b+9,"%3d%n",&status,&consumed)!=1||consumed!=3||b[12]!=' '||
       status<200||status>599||status/100==3)return false;
    out->status=status;out->headers=(size_t)(end+4-b);out->body=n-out->headers;
    const char *line=strstr(b,"\r\n");
    if(!line||line>end)return false;
    size_t length=0;bool has_length=false;
    for(line+=2;line<end;) {
        const char *next=strstr(line,"\r\n");
        if(!next||next>end||!memchr(line,':',(size_t)(next-line)))return false;
        if(!strncasecmp(line,"Transfer-Encoding:",18))return false;
        if(!strncasecmp(line,"Content-Length:",15)) {
            if(has_length)return false;
            const char *p=line+15;while(p<next&&(*p==' '||*p=='\t'))p++;
            if(p==next)return false;
            for(;p<next;p++) {
                if(*p<'0'||*p>'9')return false;
                length=length*10+(unsigned)(*p-'0');if(length>4096)return false;
            }
            has_length=true;
        }
        line=next+2;
    }
    if(has_length&&out->body>length)return false;
    if(eof&&has_length&&out->body!=length)return false;
    out->complete=has_length?out->body==length:eof;
    return true;
}