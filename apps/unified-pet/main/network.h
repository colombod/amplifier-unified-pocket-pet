#pragma once
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
#include "nvs.h"
#include "cJSON.h"
#include "setup_unified.h"
#include "setup_bridge.h"
typedef struct {
    bool setup, connected, joining, key_saved, settings_saved, tailscale_key_saved, setup_paused;
    bool bridge_enabled, bridge_token_saved, bridge_valid;
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
/* After network_init; same caller-owned/wiped buffer lifecycle as the OpenAI key. */
esp_err_t network_copy_tailscale_auth_key(char *out, size_t capacity);
esp_err_t network_copy_unified_target(setup_unified_target_t *out);
/* Own namespace only; malformed/failed reads never select the direct provider path. */
esp_err_t network_copy_bridge(setup_bridge_t *out);
esp_err_t network_copy_bridge_token(char *out,size_t capacity);
/* Network worker owns actual HTTP/DNS listener stop and AP mode transitions.
 * Timeout is NOT an acknowledgment; caller must not activate a tunnel then. */
esp_err_t network_pause_setup(uint32_t timeout_ms);
esp_err_t network_resume_setup(uint32_t timeout_ms);
