#pragma once
#include "esp_websocket_client.h"
#include "nvs.h"
#include "cJSON.h"
#include "setup_bridge.h"
typedef struct bridge_transport bridge_transport_t;
/* One active chain. Reuse only after successful disposal's final-access proof.
 * All APIs are inert until start. token is copied by pinned transport_ws. */
bridge_transport_t *bridge_transport_new(const setup_bridge_t *cfg,const char *token,int64_t absolute_deadline_us);
esp_websocket_client_handle_t bridge_transport_client(bridge_transport_t *t);
esp_err_t bridge_transport_start(bridge_transport_t *t);
bool bridge_transport_upgraded(bridge_transport_t *t);
/* Pre-start constructor-unwind helper. */
void bridge_transport_dispose_unstarted(bridge_transport_t *t);
/* Single owner after its LAST client API use. False retains storage/admission.
 * True invalidates t/client. Waits share this absolute monotonic deadline; no
 * reconnect, send, timeout-and-free or fresh cleanup time allowance is added. */
bool bridge_transport_dispose(bridge_transport_t *t,int64_t absolute_deadline_us);
bool bridge_transport_started(const bridge_transport_t *t);
bool bridge_transport_finished(const bridge_transport_t *t);
/* Atomically inhibits NEW I/O admissions, with zero-wait shutdown if io is free;
 * otherwise its owner services deferred shutdown on the still-owned socket.
 * Poll/lock slices are <=50ms at the pinned 1000Hz, charged to a fixed caller
 * deadline (also capped by the hard deadline; sub-ms wait residue rounds down).
 * A zero timeout permits one nonblocking probe, not a wait/retry.
 * Admitted blocking SDK TLS I/O
 * cannot be preempted here: this is NOT a 50ms completion bound, task join or
 * vendor-task exit proof. Only successful dispose permits storage release. */
void bridge_transport_cancel(bridge_transport_t *t);
