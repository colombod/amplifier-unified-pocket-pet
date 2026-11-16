#pragma once
#include "pet_tls.h"
esp_err_t tailnet_tls_probe(int borrowed_fd,const char *host,uint16_t port,uint64_t deadline_ms,
    tailtiny_cancel_fn cancel,void *arg,void (*http_stage)(void *),tailnet_tls_result_t *out);