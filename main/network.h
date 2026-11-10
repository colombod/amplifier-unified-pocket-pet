#pragma once
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
typedef struct {
    bool setup, connected, joining, key_saved, settings_saved;
    unsigned configure_generation;
    int rssi;
    char ap_ssid[32], ap_password[17], message[96], ssid[33];
} network_status_t;
void network_init(void);
void network_open_setup(void);
void network_snapshot(network_status_t *out);

void network_close_setup(void);
/* Device-only credential access. Caller wipes the buffer; never expose via HTTP. */
esp_err_t network_copy_api_key(char *out, size_t capacity);
