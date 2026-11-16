#pragma once
#include "esp_websocket_client.h"
#include "nvs.h"
#include "cJSON.h"
#include "setup_bridge.h"
typedef struct bridge_transport bridge_transport_t;
typedef enum {
    BR_TRANSPORT_NONE,
    BR_TRANSPORT_PROTOCOL,
    BR_TRANSPORT_POLICY,
    BR_TRANSPORT_EVENT_LOSS,
    BR_TRANSPORT_PEER_NORMAL, /* complete received WS1000; no claim about close cause */
    BR_TRANSPORT_UNSUPPORTED,
    BR_TRANSPORT_PAYLOAD_REJECTED
} bridge_transport_terminal_t;
/* One active chain. Reuse only after successful disposal's final-access proof.
 * All APIs are inert until start. token is copied by pinned transport_ws. */
bridge_transport_t *bridge_transport_new(const setup_bridge_t *cfg,const char *token,int64_t absolute_deadline_us);
esp_websocket_client_handle_t bridge_transport_client(bridge_transport_t *t);
/* Install once before start. The callback and its context must remain valid
 * until successful disposal proves that the SDK task has exited. It arbitrates
 * startup and new connect admission with the caller's Stop latch. */
bool bridge_transport_set_stop_check(bridge_transport_t *t,bool (*stopped)(void *),void *context);
esp_err_t bridge_transport_start(bridge_transport_t *t);
/* Optional absolute cutoff for starting a reconnect; zero means no retry
 * cutoff. It does not shorten the new call's own hard lifetime. */
esp_err_t bridge_transport_start_before(bridge_transport_t *t,int64_t start_by);
bool bridge_transport_upgraded(bridge_transport_t *t);
/* HTTP status of the host's answer to the one upgrade request, or 0 when no
 * answer was read (unreachable, TLS failure, timeout). A number only: it lets
 * the caller tell "busy, try again" (409) from "refused" (401/403). */
int bridge_transport_http_status(const bridge_transport_t *t);
/* Acknowledge exactly one SDK DATA callback corresponding to a completed
 * transport read. Missing callbacks make the delivery outcome uncertain. */
void bridge_transport_data_delivered(bridge_transport_t *t);
/* A DATA ticket carries the control-admission state at its first ingress.
 * That orders receipt only, never the peer's generation time or close cause.
 * False means missing/ambiguous provenance and permanently marks event loss. */
bool bridge_transport_take_data(bridge_transport_t *t,bool *after_local_control,int64_t *received_at);
/* Admit one cleanup control in the same ordering domain as plaintext reads.
 * The caller's absolute deadline includes both admission and SDK send time. */
bool bridge_transport_admit_control(bridge_transport_t *t,int64_t deadline);
/* Conservative earliest time of a failed parser read, or 0 if unobserved. */
int64_t bridge_transport_end_at(bridge_transport_t *t);
/* Pre-start constructor-unwind helper. */
void bridge_transport_dispose_unstarted(bridge_transport_t *t);
/* Single owner after its LAST client API use. False retains storage/admission.
 * True invalidates t/client. Waits share this absolute monotonic deadline; no
 * reconnect, send, timeout-and-free or fresh cleanup time allowance is added. */
bool bridge_transport_dispose(bridge_transport_t *t,int64_t absolute_deadline_us);
/* Like dispose, but reports a verified terminal wire condition only after the
 * transport task's final-access proof. False retains t and leaves out unchanged. */
bool bridge_transport_dispose_result(bridge_transport_t *t,int64_t absolute_deadline_us,
                                     bridge_transport_terminal_t *out);
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
