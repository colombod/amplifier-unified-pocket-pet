#include "tailnet_tls.h"
#include "esp_timer.h"
#include "esp_crt_bundle.h"
#include "mbedtls/x509_crt.h"
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <string.h>
static const char *ca_path;
static mbedtls_x509_crt ca;
esp_err_t esp_crt_bundle_attach(mbedtls_ssl_config *cfg) {
    int e=mbedtls_x509_crt_parse_file(&ca,ca_path);
    if(e)return ESP_FAIL;
    mbedtls_ssl_conf_ca_chain(cfg,&ca,NULL);return ESP_OK;
}
static bool cancel(void *arg) {
    int64_t *after=arg;return *after&&esp_timer_get_time()>=*after;
}
int main(int argc,char **argv) {
    if(argc!=6 && argc!=8)return 2;
    int fd=atoi(argv[1]);ca_path=argv[2];
    int flags=fcntl(fd,F_GETFL,0);if(flags<0||fcntl(fd,F_SETFL,flags|O_NONBLOCK)<0)return 3;
    mbedtls_x509_crt_init(&ca);
    int64_t end=esp_timer_get_time()+(int64_t)atoi(argv[4])*1000;
    int64_t cancel_at=atoi(argv[5])?esp_timer_get_time()+(int64_t)atoi(argv[5])*1000:0;
    tailnet_tls_result_t s;
    unsigned char request[256],original[256];
    memset(request,'Q',sizeof(request));memcpy(original,request,sizeof(request));
    int64_t started=esp_timer_get_time();
    int e;
    if(argc==8) {
        if(strcmp(argv[6],"exchange"))return 4;
        int length=atoi(argv[7]);
        e=pet_tls_exchange_http(fd,argv[3],8941,
            length<0?NULL:(const char *)request,length<0?1:(size_t)length,
            (uint64_t)end/1000,cancel,&cancel_at,NULL,&s);
    } else e=tailnet_tls_probe(fd,argv[3],8941,(uint64_t)end/1000,cancel,&cancel_at,NULL,&s);
    int64_t elapsed=esp_timer_get_time()-started;
    printf("{\"result\":%d,\"verified\":%d,\"http\":%d,\"status\":%d,\"tls_error\":%d,\"verify_flags\":%u,\"bytes\":%u,"
           "\"elapsed_us\":%lld,\"fd_open\":%d,\"request_unchanged\":%d}\n",
        e,s.verified,s.http_complete,s.http_status,s.tls_error,s.verify_flags,s.response_bytes,
        (long long)elapsed,fcntl(fd,F_GETFD)>=0,!memcmp(request,original,sizeof(request)));
    mbedtls_x509_crt_free(&ca);return 0;
}