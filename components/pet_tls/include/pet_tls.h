#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
#include "tailtiny.h"
typedef struct {
    bool verified, http_complete;
    int tls_error,http_status;
    uint32_t verify_flags;
    unsigned response_bytes;
} tailnet_tls_result_t;
/* Caller supplies a nonblocking fd and keeps request bytes immutable until return.
 * Borrowed fd and request remain owned by the caller until return. Request is
 * 1..255 bytes; no logging or credential selection. Verified chain/host/clock,
 * bounded response, cancellation/deadline and nonblocking cleanup are mandatory. */
esp_err_t pet_tls_exchange_http(int borrowed_fd,const char *host,uint16_t port,
    const char *request,size_t request_bytes,uint64_t deadline_ms,
    tailtiny_cancel_fn cancel,void *arg,void (*http_stage)(void *),tailnet_tls_result_t *out);