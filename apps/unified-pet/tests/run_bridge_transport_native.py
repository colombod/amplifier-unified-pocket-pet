"""Compile pinned IDF transport/WS/SSL I/O and app adapter over synthetic TLS/RTOS.

Run ONLY inside the parent's authorized, offline pinned SDK container:
  python3 tests/run_bridge_transport_native.py /tmp/bridge-transport-red [CASE]
Default CASE=legacy retains the original 32-I/O + HTTP suite (150 HTTP cases). Named cases:
  tx-ready60, tx-payload-stall60, tx-partial-stall60, rx-header-gap60
    Positive contracts (old 50ms-clamped adapter was RED). Run EACH as a
    separate process; do not let one assertion hide another. After one compile:
      /tmp/bridge-transport-red/test tx-ready60
      /tmp/bridge-transport-red/test rx-header-gap60
    tx-partial-stall60 additionally pins partial-byte continuation: its immediate
    short-write RED is distinct from the poll-clamp defect, not its cause proof.
  {tx,rx}-{caller10,caller0,deadline20,cancel20}
    Expected operation failures with bounded fake time (test should PASS).
Named cases call actual SDK WS send_raw/read; admitted I/O uses the extracted
SSL bodies. tx-ready60 RED exits the SDK WS preflight before SSL write, whereas
tx-payload-stall60 reaches SSL write. Only readiness, clock and TLS byte I/O
are synthetic. No WS retry is in the fixture.
The byte oracle rejects duplicate/reordered masked frames after payload stalls.
Each base poll still has a <=50ms assertion; short poll slices are NOT assumed
to be the complete caller budget. Positive cases must finish at fake t=60ms.
Additional independently runnable cases cover total/zero/negative budgets,
contended mutexes, idle RX/Start TX scheduling, nested exclusion and deadline
restoration, post-readiness cancel/invalid FD, and known-byte-only continuation.
--tick-hz defaults to the app's pinned 1000; 100 additionally checks nonzero
tick rounding with the synthetic scheduler, NOT real FreeRTOS qualification.
Use a fresh output directory for each runner invocation. No vendor source is
modified or copied into the repository. SSL context lookup/read/write are extracted byte-for-byte
from the verified SDK; only context allocation, TLS/error I/O and polling are
synthetic. Not the complete esp_websocket_client task or real TLS qualification.
"""
import argparse,hashlib,json,os,subprocess,sys
from pathlib import Path
case_names=(
    'legacy','bench-counters','tx-ready60','tx-payload-stall60','tx-partial-stall60','rx-header-gap60',
    'tx-caller10','rx-caller10','tx-caller0','rx-caller0',
    'tx-deadline20','rx-deadline20','tx-cancel20','rx-cancel20',
    'idle-rx-start-tx','nested-tls-exclusion','nested-deadline-restore',
    'lock-wait60','lock-caller20','lock-caller0','lock-cancel20',
    'cancel-after-readiness','closed-fd','closed-after-readiness',
    'poll-negative-no-retry','timeout-total-budget','negative-budget-hard',
    'zero-probe','partial-error-no-retry','partial-fixed-deadline',
    'immediate-zero-progress','subtick-lock-budget',
    'zero-probe-clock-progress',
    'cancel-final-release','cancel-nested-release','cancel-release-invalidfd',
    'nested-short-request','ws-observation-isolation','cert-connect','lifecycle','receive',
)
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('output',type=Path)
parser.add_argument('case',nargs='?',default='legacy',choices=case_names)
parser.add_argument('--tick-hz',type=int,choices=(1000,100),default=1000)
parser.add_argument('--micdiag',action='store_true')
args=parser.parse_args()
sdk=Path(os.environ['IDF_PATH'])
ssl_source=sdk/'components/tcp_transport/transport_ssl.c'
ws_source=sdk/'components/tcp_transport/transport_ws.c'
pins={
    ssl_source:'1b9e42e556f8ef85d441950d0487eca77a8e9bde8293d7f0332f0826f08f6bde',
    ws_source:'281a84a82c1e00f0198204a0ea80b981090fc3ec00b480ede3d79c9c5fc5bbfd',
    sdk/'components/esp-tls/esp_tls_errors.h':'260744dfbbdba45a6100b03967341a064ac13e3c6c0bd3e127d62a04a6f16c8b',
    sdk/'components/esp-tls/esp_tls_error_capture.c':'b42a800f4de408677ca21cf1d074ef5cdd4a2e3db3ceaf0221b13f74bccf07db',
    sdk/'components/esp-tls/esp_tls_mbedtls.c':'76fdfeaa6788301e73e2b4a5dd58b5909818fb41f75c3d63e41dcafc566001ce',
    sdk/'components/mbedtls/mbedtls/library/ssl_tls.c':'0154e70f20b465213d3f97a9c2b75e686d79ee83592f90e1d7e7d995593ca7e1',
    sdk/'components/mbedtls/mbedtls/include/mbedtls/ssl.h':'038e560c60744a13a19d67f77194f7ad6213d988349519a234df55da7b0a3bb8',
    sdk/'components/mbedtls/mbedtls/include/mbedtls/x509.h':'aa138f6080869285173296afd08727e0ab0313cca959b830250b72a3c37a3bc4',
}
pinned_sources={path:path.read_bytes() for path in pins}
for path,digest in pins.items():
    if hashlib.sha256(pinned_sources[path]).hexdigest()!=digest:
        raise SystemExit(f'Pinned SDK source mismatch: {path}; no fallback SSL stub')
source=pinned_sources[ssl_source].decode('utf-8')
signatures=(
    'static inline transport_esp_tls_t *ssl_get_context_data(',
    'static int ssl_connect(',
    'static int ssl_write(',
    'static int ssl_read(',
)
extracted=[]
for signature in signatures:
    if source.count(signature)!=1:
        raise SystemExit(f'Expected exactly one SDK function: {signature}')
    start=source.index(signature)
    end=source.index('\n}\n',start)+3
    body=source[start:end]
    extracted.append((signature,source[:start].count('\n')+1,body))
out=args.output;out.mkdir(parents=True,exist_ok=False)
inc=out/'include';(inc/'freertos').mkdir(parents=True)
generated=inc/'pinned_transport_ssl_io.inc'
generated.write_text(
    '/* Generated from verified Espressif Apache-2.0 source; do not edit. */\n'
    '/* SPDX-FileCopyrightText: 2015-2025 Espressif Systems (Shanghai) CO LTD */\n'
    '/* SPDX-License-Identifier: Apache-2.0 */\n'
    'static const char *TAG = "transport_base";\n'+
    ''.join(f'#line {line} {json.dumps(str(ssl_source))}\n'+
            (body.replace('static int ssl_connect(', 'static int pinned_ssl_connect(', 1)
             if signature=='static int ssl_connect(' else body)
            for signature,line,body in extracted))
(out/'ssl-extraction.json').write_text(json.dumps({
    'source':str(ssl_source),'sha256':pins[ssl_source],'fixture_tick_hz':args.tick_hz,
    'functions':[{'signature':name,'line':line,
                  'sha256':hashlib.sha256(body.encode('utf-8')).hexdigest()}
                 for name,line,body in extracted],
    'generated_sha256':hashlib.sha256(generated.read_bytes()).hexdigest(),
},indent=2)+'\n')
headers={
'esp_err.h':'''#pragma once
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_INVALID_SIZE 0x104
#define ESP_ERR_NVS_NOT_FOUND 0x105
#define ESP_ERR_NVS_INVALID_LENGTH 0x106
#define ESP_ERR_TIMEOUT 0x107
''',
'esp_log.h':'''#pragma once
#include <stdarg.h>
#include <stdio.h>
#define ESP_LOG_NONE 0
void esp_log_level_set(const char*,int);
void fixture_log(const char*,const char*,...);
#define ESP_LOGE(tag,...) fixture_log(tag,__VA_ARGS__)
#define ESP_LOGW(tag,...) fixture_log(tag,__VA_ARGS__)
#define ESP_LOGD(tag,...) fixture_log(tag,__VA_ARGS__)
#define ESP_LOGI(tag,...) fixture_log(tag,__VA_ARGS__)
#define ESP_LOGV(tag,...) fixture_log(tag,__VA_ARGS__)
#define ESP_STATIC_ANALYZER_CHECK(test,ret) do {if(test)return ret;}while(0)
''',
'esp_tls.h':'''#pragma once
#include <stddef.h>
#include <sys/types.h>
#include "esp_err.h"
#include "esp_tls_errors.h"
typedef struct esp_tls {void *transport;esp_tls_last_error_t error;} esp_tls_t;
typedef struct {int timeout_ms;} esp_tls_cfg_t;
ssize_t esp_tls_conn_read(esp_tls_t*,void*,size_t);
ssize_t esp_tls_conn_write(esp_tls_t*,const void*,size_t);
esp_err_t esp_tls_get_error_handle(esp_tls_t*,esp_tls_error_handle_t*);
esp_tls_t *esp_tls_init(void);
int esp_tls_conn_new_sync(const char*,int,int,const esp_tls_cfg_t*,esp_tls_t*);
int esp_tls_conn_destroy(esp_tls_t*);
esp_err_t esp_tls_get_conn_sockfd(esp_tls_t*,int*);
static inline int esp_tls_get_and_clear_error_type(void*p,esp_tls_error_type_t t,int*out){(void)p;(void)t;*out=0;return 0;}
''',
'esp_tls_crypto.h':'''#pragma once
#include "mbedtls/base64.h"
#include "mbedtls/sha1.h"
#define esp_crypto_base64_encode mbedtls_base64_encode
#define esp_crypto_sha1 mbedtls_sha1
''',
'nvs.h':'''#pragma once
#include <stddef.h>
#include "esp_err.h"
typedef int nvs_handle_t;
esp_err_t nvs_get_blob(nvs_handle_t,const char*,void*,size_t*);
esp_err_t nvs_get_str(nvs_handle_t,const char*,char*,size_t*);
esp_err_t nvs_set_blob(nvs_handle_t,const char*,const void*,size_t);
esp_err_t nvs_set_str(nvs_handle_t,const char*,const char*);
esp_err_t nvs_erase_key(nvs_handle_t,const char*);
esp_err_t nvs_commit(nvs_handle_t);
''',
'esp_idf_version.h':'''#pragma once
#define ESP_IDF_VERSION_VAL(a,b,c) ((a)*10000+(b)*100+(c))
#define ESP_IDF_VERSION ESP_IDF_VERSION_VAL(5,5,4)
''',
'esp_crt_bundle.h':'''#pragma once
#include "esp_err.h"
esp_err_t esp_crt_bundle_attach(void*);
''',
'esp_timer.h':'''#pragma once
#include <stdint.h>
#include "esp_err.h"
typedef struct fixture_timer *esp_timer_handle_t;
typedef struct {void(*callback)(void*);void*arg;const char*name;} esp_timer_create_args_t;
int64_t esp_timer_get_time(void);
esp_err_t esp_timer_create(const esp_timer_create_args_t*,esp_timer_handle_t*);
esp_err_t esp_timer_start_once(esp_timer_handle_t,uint64_t);
esp_err_t esp_timer_stop(esp_timer_handle_t);
esp_err_t esp_timer_delete(esp_timer_handle_t);
''',
'freertos/FreeRTOS.h':'''#pragma once
#include <stdint.h>
#include <pthread.h>
typedef unsigned TickType_t;
typedef int BaseType_t;
typedef pthread_mutex_t portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED PTHREAD_MUTEX_INITIALIZER
#define portENTER_CRITICAL(m) ((void)pthread_mutex_lock(m))
#define portEXIT_CRITICAL(m) ((void)pthread_mutex_unlock(m))
#ifndef configTICK_RATE_HZ
#define configTICK_RATE_HZ 1000
#endif
#define pdTRUE 1
#define pdFALSE 0
#define configMAX_TASK_NAME_LEN 16
#define tskNO_AFFINITY 0x7fffffff
#define pdMS_TO_TICKS(x) ((TickType_t)((uint64_t)(x)*configTICK_RATE_HZ/1000))
''',
'freertos/task.h':'''#pragma once
#include "freertos/FreeRTOS.h"
typedef void *TaskHandle_t;
typedef struct {const char *pcTaskName;unsigned xTaskNumber,uxCurrentPriority,ulRunTimeCounter;int xCoreID;} TaskStatus_t;
#define eInvalid 0
static inline void vTaskGetInfo(void *h,TaskStatus_t *s,int stack,int state){
    (void)h;(void)stack;(void)state;
    *s=(TaskStatus_t){.pcTaskName="fixture",.xTaskNumber=1};
}
TaskHandle_t xTaskGetCurrentTaskHandle(void);
void vTaskDelay(TickType_t);
''',
'freertos/semphr.h':'''#pragma once
typedef struct fixture_lock *SemaphoreHandle_t;
SemaphoreHandle_t xSemaphoreCreateMutex(void);
SemaphoreHandle_t xSemaphoreCreateRecursiveMutex(void);
int xSemaphoreTake(SemaphoreHandle_t,unsigned);
int xSemaphoreTakeRecursive(SemaphoreHandle_t,unsigned);
int xSemaphoreGive(SemaphoreHandle_t);
int xSemaphoreGiveRecursive(SemaphoreHandle_t);
void vSemaphoreDelete(SemaphoreHandle_t);
''',
'esp_transport_ssl.h':'''#pragma once
#include "esp_transport.h"
esp_transport_handle_t esp_transport_ssl_init(void);
void esp_transport_ssl_crt_bundle_attach(esp_transport_handle_t,esp_err_t(*)(void*));
void esp_transport_ssl_set_common_name(esp_transport_handle_t,const char*);
''',
'esp_websocket_client.h':'''#pragma once
#include <stddef.h>
#include <stdbool.h>
#include "esp_transport.h"
#ifdef BRIDGE_LIFECYCLE_TEST
typedef struct esp_websocket_client *esp_websocket_client_handle_t;
#else
typedef struct fixture_client *esp_websocket_client_handle_t;
#endif
typedef const char *esp_event_base_t;
typedef void (*esp_event_handler_t)(void*,esp_event_base_t,int32_t,void*);
#ifdef BRIDGE_RECEIVE_TEST
#include "pinned_client_event_types.inc"
#else
typedef enum {WEBSOCKET_EVENT_ANY=-1,WEBSOCKET_EVENT_ERROR=0,
WEBSOCKET_EVENT_HEADER_RECEIVED,WEBSOCKET_EVENT_CONNECTED,
WEBSOCKET_EVENT_DISCONNECTED,WEBSOCKET_EVENT_DATA,WEBSOCKET_EVENT_CLOSED,
WEBSOCKET_EVENT_BEFORE_CONNECT,WEBSOCKET_EVENT_BEGIN,WEBSOCKET_EVENT_FINISH} esp_websocket_event_id_t;
typedef enum {WEBSOCKET_ERROR_TYPE_NONE,WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT,
WEBSOCKET_ERROR_TYPE_PONG_TIMEOUT,WEBSOCKET_ERROR_TYPE_HANDSHAKE,
WEBSOCKET_ERROR_TYPE_SERVER_CLOSE} esp_websocket_error_type_t;
#endif
#define WEBSOCKET_TRANSPORT_OVER_SSL 2
typedef struct {const char*host;int port;const char*path;int transport;bool disable_auto_reconnect,enable_close_reconnect;esp_transport_handle_t ext_transport;int network_timeout_ms,buffer_size,task_stack,task_prio,ping_interval_sec,pingpong_timeout_sec;} esp_websocket_client_config_t;
esp_websocket_client_handle_t esp_websocket_client_init(const esp_websocket_client_config_t*);
esp_err_t esp_websocket_client_destroy(esp_websocket_client_handle_t);
esp_err_t esp_websocket_client_start(esp_websocket_client_handle_t);
esp_err_t esp_websocket_client_destroy_on_exit(esp_websocket_client_handle_t);
esp_err_t esp_websocket_register_events(esp_websocket_client_handle_t,int,esp_event_handler_t,void*);
'''
}
for name,text in headers.items():(inc/name).write_text(text)
files=[Path('main/bench_diag.h'),Path('main/bridge_transport.c'),Path('main/bridge_transport.h'),Path('main/setup_bridge.h'),Path('tests/bridge_transport_test.c'),Path(__file__),sdk/'components/tcp_transport/transport.c',ws_source,ssl_source,sdk/'components/tcp_transport/include/esp_transport.h',sdk/'components/tcp_transport/private_include/esp_transport_internal.h',sdk/'components/esp-tls/esp_tls_errors.h',generated]
files.extend(inc/name for name in headers)
files.extend(pins)
mutant=os.environ.get('PET_CERT_TEST_MUTANT','')
if mutant:
    if mutant not in ('latch','flags') or args.case!='cert-connect':
        raise SystemExit('Unsupported certificate test mutation')
    app=Path('main/bridge_transport.c').read_text()
    needle=('if(result<0)note_connect_error(t,ssl);' if mutant=='latch'
            else 'if(flags&&!(flags&~failure_flags)){')
    replacement=('if(result<0&&false)note_connect_error(t,ssl);' if mutant=='latch'
                 else 'if(flags&&flags!=UINT32_MAX&&(failure_flags||true)){')
    assert app.count(needle)==1
    mutated=inc/'bridge-transport-mutant.inc'
    mutated.write_text(app.replace(needle,replacement,1))
    files.append(mutated)
if args.case in ('lifecycle','receive'):
    # No replacement task algorithm: compile the COMPLETE pinned task, start,
    # abort and destroy bodies. RTOS, event dispatch, TLS and receive are named
    # synthetic seams in bridge_lifecycle_test.c, not device qualification.
    client_source=Path(os.environ['PET_WS_COMPONENT'])/'esp_websocket_client.c'
    proof_root=Path(os.environ['PET_LIFECYCLE_PROOF'])
    proof_pins={
        client_source:'30d7d7d2eb02782557a79909df9647a4b8aef09d504735758c31110479acb51c',
        sdk/'components/pthread/pthread_local_storage.c':'838b9ea24df5493cc648248110bc5ce2e28c98bbad945ac7cfd70912977cc378',
        sdk/'components/freertos/FreeRTOS-Kernel/portable/xtensa/port.c':'747f3b861ae4a4ccb74e603fece20866be47e2d1f526df9c3f7c47cf09916cda',
        proof_root/'tasks.c':'193c3d8379acc847097632da8e6d4dd2ee0bf8e069d78979d68dd26b653d48df',
        proof_root/'esp_timer.c':'31715c3ff0931d6957df398efdc5e58a052a17cf88091752648192941d466c0b',
        proof_root/'event_groups.c':'5e1d21a871cc23a0fa493a9aa0d50dfd43abd67b06b9bcf87d8a6904b5c03f85',
    }
    for path,digest in proof_pins.items():
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise SystemExit(f'Lifecycle pinned-source mismatch: {path}')
    text=client_source.read_text()
    licence=('/* Exact Espressif source excerpts, generated only after full hash check. */\n'
             '/* SPDX-FileCopyrightText: 2015-2025 Espressif Systems (Shanghai) CO LTD */\n'
             '/* SPDX-License-Identifier: Apache-2.0 */\n')
    declarations=text[text.index('typedef struct {\n    const char                 *task_name;'):
                      text.index('\nstatic uint64_t _tick_get_ms')]
    types=inc/'pinned_client_types.inc'
    types.write_text(licence+declarations)
    bodies=[]
    for signature in (
        'static uint64_t _tick_get_ms(',
        *(('static esp_err_t esp_websocket_new_buf(',
           'static void esp_websocket_free_buf(',
           'static esp_err_t esp_websocket_client_dispatch_event(',
           'static esp_err_t esp_websocket_client_recv(') if args.case=='receive' else ()),
        'static esp_err_t esp_websocket_client_destroy_config(',
        'static void destroy_and_free_resources(',
        'esp_err_t esp_websocket_client_destroy(',
        'esp_err_t esp_websocket_client_destroy_on_exit(',
        'static esp_err_t esp_websocket_client_abort_connection(',
        'static void esp_websocket_client_task(',
        'esp_err_t esp_websocket_client_start(',
    ):
        if text.count(signature)!=1:
            raise SystemExit(f'Nonunique lifecycle function: {signature}')
        start=text.index(signature);end=text.index('\n}\n',start)+3
        bodies.append((signature,text[:start].count('\n')+1,text[start:end]))
    lifecycle=inc/'pinned_client_lifecycle.inc'
    lifecycle.write_text(licence+''.join(
        f'#line {line} {json.dumps(str(client_source))}\n{body}'
        for _,line,body in bodies))
    (out/'lifecycle-extraction.json').write_text(json.dumps({
        'pins':{str(p):h for p,h in proof_pins.items()},
        'executed_exact_functions':[{'signature':s,'line':line,
              'sha256':hashlib.sha256(body.encode()).hexdigest()} for s,line,body in bodies],
        'boundaries':'Synthetic init/event dispatch/RTOS/transport/TLS/recv; real host pthread destructors. '
                     'IDF pthread/port/tasks/timer/event files are hash-checked source evidence, NOT executed.',
    },indent=2)+'\n')
    files.extend([*proof_pins,types,lifecycle,Path('tests/bridge_lifecycle_test.c')])
    if args.case=='receive':
        tls_source=sdk/'components/esp-tls/esp_tls.c'
        tls_bytes=tls_source.read_bytes()
        if hashlib.sha256(tls_bytes).hexdigest()!='b8f5a79f0efd0b049e4ae132acbdecf22c6494eff269cb5bc93624a88158f3dc':
            raise SystemExit('Pinned IDF TLS get-and-clear source mismatch')
        tls_text=tls_bytes.decode('utf-8')
        signature='esp_err_t esp_tls_get_and_clear_last_error('
        assert tls_text.count(signature)==1
        a=tls_text.index(signature);b=tls_text.index('\n}\n',a)+3
        clear=inc/'pinned_tls_get_clear.inc'
        clear.write_text(licence+f'#line {tls_text[:a].count(chr(10))+1} {json.dumps(str(tls_source))}\n'+tls_text[a:b])
        files.extend([tls_source,clear])
        event_source=sdk/'components/esp_event/esp_event.c'
        event_bytes=event_source.read_bytes()
        if hashlib.sha256(event_bytes).hexdigest()!='30b9d6bf74d47ee2c2a5015866b3d01c1a04be404b2952f75108864c03cd8016':
            raise SystemExit('Pinned IDF event source mismatch')
        event_text=event_bytes.decode('utf-8')
        signature='esp_err_t esp_event_post_to('
        assert event_text.count(signature)==1
        start=event_text.index(signature);end=event_text.index('\n}\n',start)+3
        event_post=inc/'pinned_event_post.inc'
        event_post.write_text(licence+f'#line {event_text[:start].count(chr(10))+1} {json.dumps(str(event_source))}\n'+event_text[start:end])
        files.extend([event_source,event_post])
        header=client_source.parent/'include/esp_websocket_client.h'
        if hashlib.sha256(header.read_bytes()).hexdigest()!='9c318f1280aec39a2ce050f0d2450d45198bed8158761256829ba6478b930a5e':
            raise SystemExit('Pinned client event ABI header mismatch')
        h=header.read_text()
        a=h.index('typedef enum {\n    WEBSOCKET_EVENT_ANY')
        b=h.index('} esp_websocket_event_data_t;',a)+len('} esp_websocket_event_data_t;')
        event_types=inc/'pinned_client_event_types.inc'
        event_types.write_text(licence+h[a:b]+'\n')
        # Exact app callback and literal owner dequeue/parse/model/audio block.
        # The owner shell, audio producer/stream buffers and sends are synthetic;
        # no duplicate parser, callback ordering policy or model is implemented.
        voice=Path('main/bridge_voice.c');v=voice.read_text()
        sections=[]
        def section(name,a,b):
            body=v[a:b]
            sections.append({'name':name,'line':v[:a].count('\n')+1,
                             'sha256':hashlib.sha256(body.encode()).hexdigest()})
            return f'#line {v[:a].count(chr(10))+1} "main/bridge_voice.c"\n'+body
        chunks=[section('context',v.index('#define BV_MIC'),v.index('static bool take('))]
        for name in ('take','give','quiet','health_end','note_exit','stopping','exit_status','callback_failed','protocol_failed',
                     'failure','ws_event','transport_lost','observe_converse_send','converse_end','retry_left','send_frame_timed',
                     'send_frame','send_action','pace_sent_audio','append_audio','finish_capture',
                     'cleanup_unconfirmed','converse_tail'):
            import re
            matches=list(re.finditer(r'^static (?:bool|void|unsigned|const char\s*\*)\s*'+name+r'\(',v,re.M))
            if len(matches)!=1:raise SystemExit(f'App extraction anchor mismatch: {name}')
            a=matches[0].start();b=v.index('\n}\n',a)+3 if name not in ('take','give') else v.index('\n',a)
            chunks.append(section(name,a,b))
        a=v.index('if(xQueueReceive(c->frames,&frame,pdMS_TO_TICKS(5))==pdTRUE){')
        b=v.index('\n        if(transport_lost(c))',a)
        lost_end=v.index('\n        unsigned command=',b)
        chunks.append('static void production_consume_one(bridge_voice_t *c){\n'
                      '  do {bv_frame_t frame;\n'+section('owner_consume',a,b)+'\n'
                      +section('owner_transport_lost',b,lost_end)+'\n  }while(0);\n}\n')
        app=inc/'receive_app.inc';app.write_text('\n'.join(chunks)+'\n')
        (out/'receive-extraction.json').write_text(json.dumps({
            'app_source':str(voice),'sha256':hashlib.sha256(voice.read_bytes()).hexdigest(),
            'sections':sections,'event_header_sha256':hashlib.sha256(header.read_bytes()).hexdigest(),
            'scope':'exact SDK recv/dispatch and full WS transport -> literal ws_event/live_message_feed '
                    '-> literal owner consume block/bridge_parse/bridge_step; actual append/finish helpers. '
                    'Synthetic post/run event primitives, RTOS, raw TLS bytes, audio buffers, post-upgrade '
                    'connection and send_text. NOT full owner scheduling, actual TLS/codec/provider/device.'
        },indent=2)+'\n')
        builder=out/'golden-builder.py'
        builder.write_text('''import base64,hashlib,json,sys
from pathlib import Path
from pet_bridge import gateway_state as g
paths={str(Path(m.__file__).resolve()) for m in tuple(sys.modules.values())
       if getattr(m,"__file__",None) and Path(m.__file__).is_file()}
inputs={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sorted(paths)}
turn="123e4567-e89b-42d3-a456-426614174000"
text="Offline receive fixture: \u03c0 and exact bytes."
pcm=bytes((i*37+11)%256 for i in range(960))
frames={
 "started":g.build_session_started(),
 "began":g.build_turn_began(turn,"Receive fixture","1234abcd"),
 "preview":g.build_preview(turn,1,text,hashlib.sha256(text.encode()).hexdigest()),
 "accepted":g.build_send_result(turn,"accepted"),
 "reply_started":g.build_reply_started(turn),
 "audio":g.build_audio_delta(pcm),
 "done":g.build_reply_done(turn,"completed"),
 "closed":g.build_session_closed(),
 "text":text,
}
assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in inputs.items())
print(json.dumps({"frames":frames,"pcm":list(pcm),
 "inputs":inputs,"builder_input_integrity":True,"python":sys.version},ensure_ascii=True))
''')
        # Pure builder calls only. No Journal, credentials, target session or I/O
        # transport is instantiated. JSON stdout is the sole result channel.
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',
                 PYTHONPATH=str(Path('../../bridge').resolve()))
        golden=subprocess.run([sys.executable,'-B',str(builder)],check=True,
                              capture_output=True,text=True,env=env)
        fixture=json.loads(golden.stdout)
        if not fixture['builder_input_integrity'] or any(
                hashlib.sha256(Path(p).read_bytes()).hexdigest()!=h
                for p,h in fixture['inputs'].items()):
            raise SystemExit('Builder/stdlib source changed during golden generation')
        (out/'golden-fixtures.json').write_text(golden.stdout)
        gold=inc/'receive_golden.inc'
        gold.write_text(''.join('static const char g_'+name+'[]={'+
                        ','.join(str(x) for x in text.encode())+',0};\n'
                        for name,text in fixture['frames'].items())+
                        'static const unsigned char g_pcm[]={'+
                        ','.join(map(str,fixture['pcm']))+'};\n')
        files.extend([header,event_types,voice,app,builder,gold,
                      *map(Path,fixture['inputs']),Path('main/bridge_protocol.c'),
                      Path('main/bridge_protocol.h'),Path('main/live_protocol.h'),
                      sdk/'components/json/cJSON/cJSON.c'])
(out/'inputs.json').write_text(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},indent=2)+'\n')
cc=['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-g','-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie','-ffunction-sections','-fdata-sections','-DCONFIG_LOG_DYNAMIC_LEVEL_CONTROL=1','-DCONFIG_ESP_TLS_USING_MBEDTLS=1','-DCONFIG_WS_BUFFER_SIZE=4096','-I'+str(inc),'-Imain','-I'+str(sdk/'components/tcp_transport/include'),'-I'+str(sdk/'components/tcp_transport/private_include'),'-I'+str(sdk/'components/json/cJSON'),'-I'+str(sdk/'components/mbedtls/mbedtls/include'),'tests/bridge_transport_test.c',str(sdk/'components/tcp_transport/transport.c'),str(sdk/'components/tcp_transport/transport_ws.c'),os.environ['PET_TLS_HOST_LIB']+'/libmbedcrypto.a','-Wl,--gc-sections','-o',str(out/'test')]
# Native 64-bit builds expose vendor signed-size warnings not enabled by its
# SDK build. Scope the exception to unchanged SDK objects, never the app/tests.
base=cc[:cc.index('tests/bridge_transport_test.c')]
if args.micdiag:base.append('-DPET_PRIVATE_MIC_DIAG=1')
base.append('-I'+str(sdk/'components/esp-tls'))
if mutant:base.append('-DBRIDGE_TEST_TRANSPORT_MUTANT=1')
base.append(f'-DconfigTICK_RATE_HZ={args.tick_hz}')
if args.case in ('lifecycle','receive'):
    base.append('-DBRIDGE_LIFECYCLE_TEST=1')
if args.case=='receive':
    base.extend(['-DBRIDGE_RECEIVE_TEST=1','-DWS_TRANSPORT_HEADER_CALLBACK_SUPPORT=0'])
try:
    objects=[]
    for name in (('transport',) if args.case=='lifecycle' else ('transport','transport_ws')):
        obj=out/(name+'.o')
        rename=(['-Desp_transport_ws_init=fixture_vendor_ws_init']
                if args.case=='receive' and name=='transport_ws' else [])
        subprocess.run(base+rename+['-Wno-sign-compare','-c',str(sdk/f'components/tcp_transport/{name}.c'),'-o',str(obj)],check=True)
        objects.append(str(obj))
    test_file='tests/bridge_lifecycle_test.c' if args.case in ('lifecycle','receive') else 'tests/bridge_transport_test.c'
    if args.case=='receive':
        objects.extend(['main/bridge_protocol.c',str(sdk/'components/json/cJSON/cJSON.c')])
    subprocess.run(base+[test_file]+objects+[os.environ['PET_TLS_HOST_LIB']+'/libmbedcrypto.a','-Wl,--gc-sections','-o',str(out/'test')],check=True)
    # Assertion RED is a real nonzero result, never xfail/converted to PASS.
    # timeout bounds a broken repair that spins without polling fake time.
    cases=(('hard-deadline-timer-start','hard-deadline-timer-control',
            'stop-inner-io-admission','inner-io-admission-control',
            'slow-inner-before-io-budget','slow-inner-owned-budget',
            'cancel-during-timer-start','stop-during-timer-start','stop-before-connect',
            'stop-check-retry-deadline','stop-check-connect-budget','stop-check-first-connect-budget',
            'immediate-exit','finish-stopped-destructor','normal-close','peer-failure',
            'dispose-timeout','abandoned-ingress','abandoned-close','abandoned-empty','task-create-fail','timer-start-fail','key-create-fail',
            'setspecific-fail','self-destroy-fail','delayed-cutoff-reuse',
            'active-cutoff-join','active-cutoff-timeout','timer-stop-fail','timer-delete-fail')
           if args.case=='lifecycle' else (args.case,))
    if args.case=='receive':
        cases=('cert-event-order','cert-event-post-loss','rx-normal','rx-tcp-byte-splits','rx-continuations-controls',
               'rx-reply-coalesced-eof','rx-unsolicited-audio','rx-zero-controls',
               'rx-binary','rx-nested-text','rx-json-coalesced','rx-nul',
               'rx-queue-overflow','rx-truncated-eof','rx-empty-final-continuation',
               'rx-tcp-control-splits','ctrl-len1','ctrl-len125','ctrl-len0',
               'ctrl-bad-fin','ctrl-bad-opcode','ctrl-oversize','ctrl-small-buffer',
               'ctrl-partial-eof','ctrl-zero-after-byte','ctrl-error-after-byte',
               'ctrl-caller-budget','ctrl-hard-budget','ctrl-cancel-accumulating',
               'ctrl-zero-probe','ctrl-text-streaming','rx-converse','rx-converse-split',
               'close-protocol','close-policy','close-protocol-reason','close-policy-reason',
               'close-protocol-local','close-policy-local','close-ambiguous-local',
               'close-normal','close-normal-local','close-normal-dispatch','close-unsupported','close-unsupported-local',
               'close-bad-payload','close-bad-payload-local','close-bad-payload-reason',
               'close-bad-payload-cancel-final','close-too-large','close-too-large-local','close-too-large-reason',
               'close-too-large-local-cancel-final',
               'close-bad-payload-seed-protocol','close-too-large-seed-policy','close-normal-seed-payload',
               'close-wire-0000','close-wire-0000-local','close-wire-0999','close-wire-0999-local',
               'close-wire-1015','close-wire-1015-local','close-wire-1015-seed-policy',
               'close-wire-3000','close-wire-3000-local','close-wire-4999','close-wire-4999-local',
               'close-wire-5000','close-wire-5000-local','close-wire-65535','close-wire-65535-local',
               'close-reserved-1004','close-reserved-1005-local','close-reserved-1006','close-client-only-1010-local',
               'close-ambiguous','close-empty','close-empty-local','close-single','close-policy-cancel-final',
               'partial-close-eof-0','partial-close-error-0','partial-close-zero-0','partial-close-eof-1','partial-close-eof-2',
               'partial-close-1002','partial-close-1008','partial-close-1011','partial-close-cancel',
               'partial-ping-eof','partial-pong-eof','partial-ping-delayed','partial-pong-delayed',
               'bare-eof','bare-eof-after-close','bare-zero-after-close',
               'header-loss-one','header-loss-cancel','header-loss-extended-prior','header-loss-masked-prior',
               'header-loss-extended-zero-prior','header-loss-masked-zero-prior',
               'text-first-error','text-first-zero','bare-zero','bare-cancel','bare-eof-after-data',
               'origin-before-dispatch','origin-after-admission','origin-fragment-before','origin-fragment-after','eof-delayed-dispatch',
               'missing-ticket','missing-ticket-inactive','start-before-timer-delay',
               'lost-post-notice','lost-post-error','lost-post-middle',
               'cleanup-race-rx-normal','cleanup-race-rx-unconfirmed','cleanup-race-rx-json-closed',
               'cleanup-race-rx-confirmed','cleanup-race-rx-normal-before','cleanup-race-rx-bare-loss',
               'cleanup-race-rx-unconfirmed-only')
    for case in cases:
        subprocess.run([str(out/'test'),case],check=True,timeout=30)
finally:
    # Preserve the same input-integrity check on an expected RED, not just GREEN.
    changed=[p for p,v in json.loads((out/'inputs.json').read_text()).items()
             if hashlib.sha256(Path(p).read_bytes()).hexdigest()!=v]
    if changed:
        raise RuntimeError(f'Inputs changed during native check: {changed}')
    print('Input integrity verified (also on RED); this is not a test PASS.',flush=True)
if args.case=='lifecycle':
    print(f'PASS {len(cases)} lifecycle cases: actual adapter + complete pinned client task/start/abort/destroy; synthetic RTOS/events/TLS/receive, NOT physical proof')
elif args.case=='receive':
    print(f'PASS {len(cases)} receive cases: exact SDK recv/dispatch/WS -> actual callback/assembly/parser/model; synthetic RTOS/TLS/codec/send boundaries, NOT physical proof')
else:
    print(f'PASS CASE {args.case}: exact IDF transport/WS + verified SSL read/write and adapter; complete client task/abort policy and real TLS remain unqualified')
