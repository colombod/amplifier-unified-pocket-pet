#include "tailnet_tls.h"
#include <stdio.h>
#include <string.h>
esp_err_t tailnet_tls_probe(int fd,const char *host,uint16_t port,uint64_t until,
    tailtiny_cancel_fn cancel,void *arg,void (*http_stage)(void *),tailnet_tls_result_t *out) {
    if(!out)return ESP_ERR_INVALID_ARG;
    memset(out,0,sizeof(*out));
    if(!host)return ESP_ERR_INVALID_ARG;
    char request[256];
    int size=snprintf(request,sizeof(request),"GET /api/connections/v1 HTTP/1.1\r\nHost: %s:%u\r\nConnection: close\r\nAccept: application/json\r\n\r\n",host,port);
    if(size<0||(size_t)size>=sizeof(request))return ESP_ERR_INVALID_SIZE;
    return pet_tls_exchange_http(fd,host,port,request,(size_t)size,until,cancel,arg,http_stage,out);
}