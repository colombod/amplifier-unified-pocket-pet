#pragma once
/* Host TCP hardware seam. Public config/status and production ml_tcp.c stay real. */
#include "tailtiny.h"
#include "ml_policy.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdlib.h>
#include <unistd.h>
#ifdef TCP_DIAG_TEST
#include <sys/socket.h>
#include <sys/select.h>
#define ESP_LOG_INFO 3
void esp_log_write(int level,const char *tag,const char *format,...);
int test_socket(int,int,int);
int test_bind(int,const struct sockaddr *,socklen_t);
int test_connect(int,const struct sockaddr *,socklen_t);
int test_fcntl(int,int,...);
int test_close(int);
int test_select(int,fd_set *,fd_set *,fd_set *,struct timeval *);
int test_getsockopt(int,int,int,void *,socklen_t *);
void *test_calloc(size_t,size_t);
#endif
#define ML_EVT_SHUTDOWN_REQUEST 64
struct tailtiny_s {
    tailtiny_state_t state;
    uint32_t vpn_ip,events;
    ml_policy_t policy;
    uint32_t app_connect_ip,wg_ready_ip;
};
static inline uint32_t xEventGroupGetBits(uint32_t bits) {return bits;}