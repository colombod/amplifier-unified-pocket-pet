#include "pet_tls.h"
#include "tailnet_http.h"
#include "esp_crt_bundle.h"
#include "esp_timer.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "mbedtls/ssl.h"
#include "mbedtls/net_sockets.h"
#include "mbedtls/platform_util.h"
#if !defined(MBEDTLS_HAVE_TIME_DATE) || !defined(MBEDTLS_SSL_SERVER_NAME_INDICATION)
#error "The native spike requires real certificate expiry and SNI support"
#endif
#include <errno.h>
#include <time.h>
#include <stdlib.h>
static int rng(void *ctx,unsigned char *out,size_t n) {(void)ctx;esp_fill_random(out,n);return 0;}
static int tx(void *ctx,const unsigned char *b,size_t n) {
    int r=send(*(int *)ctx,b,n,0);
    if(r>=0)return r;
    return errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR?MBEDTLS_ERR_SSL_WANT_WRITE:MBEDTLS_ERR_NET_SEND_FAILED;
}
static int rx(void *ctx,unsigned char *b,size_t n) {
    int r=recv(*(int *)ctx,b,n,0);
    if(r>=0)return r;
    return errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR?MBEDTLS_ERR_SSL_WANT_READ:MBEDTLS_ERR_NET_RECV_FAILED;
}
static bool stopped(uint64_t until,tailtiny_cancel_fn cancel,void *arg) {
    return (uint64_t)esp_timer_get_time()/1000>=until||(cancel&&cancel(arg));
}
static bool want(int r) {return r==MBEDTLS_ERR_SSL_WANT_READ||r==MBEDTLS_ERR_SSL_WANT_WRITE;}
esp_err_t pet_tls_exchange_http(int fd,const char *host,uint16_t port,
    const char *request,size_t request_bytes,uint64_t until,
    tailtiny_cancel_fn cancel,void *arg,void (*http_stage)(void *),tailnet_tls_result_t *out) {
    if(!out)return ESP_ERR_INVALID_ARG;
    memset(out,0,sizeof(*out));
    if(fd<0||!host||!host[0]||!port||!request||!request_bytes||request_bytes>255||time(NULL)<1760000000)return ESP_ERR_INVALID_ARG;
    struct tls_context {mbedtls_ssl_context ssl;mbedtls_ssl_config cfg;char response[4097];} *c=calloc(1,sizeof(*c));
    if(!c)return ESP_ERR_NO_MEM;
    mbedtls_ssl_init(&c->ssl);mbedtls_ssl_config_init(&c->cfg);
    esp_err_t result=ESP_FAIL;int rc;
    rc=mbedtls_ssl_config_defaults(&c->cfg,MBEDTLS_SSL_IS_CLIENT,MBEDTLS_SSL_TRANSPORT_STREAM,MBEDTLS_SSL_PRESET_DEFAULT);
    if(rc)goto cleanup;
    mbedtls_ssl_conf_authmode(&c->cfg,MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_rng(&c->cfg,rng,NULL);
    if(esp_crt_bundle_attach(&c->cfg)!=ESP_OK)goto cleanup;
    rc=mbedtls_ssl_setup(&c->ssl,&c->cfg);if(rc)goto cleanup;
    rc=mbedtls_ssl_set_hostname(&c->ssl,host);if(rc)goto cleanup;
    mbedtls_ssl_set_bio(&c->ssl,&fd,tx,rx,NULL);
    do {
        if(stopped(until,cancel,arg)){result=ESP_ERR_TIMEOUT;goto cleanup;}
        rc=mbedtls_ssl_handshake(&c->ssl);
        if(want(rc))vTaskDelay(pdMS_TO_TICKS(10));
    }while(want(rc));
    out->verify_flags=mbedtls_ssl_get_verify_result(&c->ssl);
    if(rc||out->verify_flags)goto cleanup;
    out->verified=true;if(http_stage)http_stage(arg);
    for(size_t sent=0;sent<request_bytes;) {
        if(stopped(until,cancel,arg)){result=ESP_ERR_TIMEOUT;goto cleanup;}
        rc=mbedtls_ssl_write(&c->ssl,(const unsigned char *)request+sent,request_bytes-sent);
        if(rc>0)sent+=rc;
        else if(want(rc))vTaskDelay(pdMS_TO_TICKS(10));
        else goto cleanup;
    }
    size_t n=0;
    for(;;) {
        if(stopped(until,cancel,arg)){result=ESP_ERR_TIMEOUT;goto cleanup;}
        if(n==4096){result=ESP_ERR_INVALID_SIZE;goto cleanup;}
        rc=mbedtls_ssl_read(&c->ssl,(unsigned char *)c->response+n,4096-n);
        if(want(rc)){vTaskDelay(pdMS_TO_TICKS(10));continue;}
        bool eof=rc==0||rc==MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY;
        if(rc<0&&!eof)goto cleanup;
        if(rc>0)n+=(size_t)rc;
        c->response[n]=0;out->response_bytes=n;
        tailnet_http_result_t h;
        if(!tailnet_http_parse(c->response,n,eof,&h)){result=ESP_ERR_INVALID_RESPONSE;goto cleanup;}
        out->http_status=h.status;
        if(h.complete){out->http_complete=true;result=ESP_OK;break;}
        if(eof){result=ESP_ERR_INVALID_RESPONSE;break;}
    }
cleanup:
    out->tls_error=rc<0&&!want(rc)?rc:0;
    /* No blocking close_notify. Owner closes its borrowed TCP fd next. */
    mbedtls_ssl_free(&c->ssl);mbedtls_ssl_config_free(&c->cfg);
    mbedtls_platform_zeroize(c,sizeof(*c));free(c);return result;
}