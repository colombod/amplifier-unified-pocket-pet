#define _GNU_SOURCE
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"

typedef struct { cJSON *value; int code; const char *error; } setup_reply_t;
typedef struct { bool setup, setup_paused; char ap_password[17]; } network_status_t;
static network_status_t status={.setup=true,.ap_password="fixture-code"};
static bool permit=true,setup_activity_seen;
static int leases,saves,setup_activity_at;
static bool network_activity_begin(void) {if(permit)leases++;return permit;}
static void network_activity_end(void) {assert(leases>0);leases--;}
static void network_snapshot(network_status_t *out) {*out=status;}
#define xSemaphoreTake(lock,timeout) ((void)0)
#define xSemaphoreGive(lock) ((void)0)
static int now_ms(void) {return 10;}
static cJSON *setup_status(bool phone) {assert(!phone);return cJSON_CreateObject();}
static void submit_settings(setup_reply_t *out,cJSON *json) {
    if(!json)out->error="Invalid JSON";
    else {assert(cJSON_IsString(cJSON_GetObjectItem(json,"password")));saves++;out->value=cJSON_CreateObject();}
    cJSON_Delete(json);
}
#include "usb-production.inc"
static char output[256];
static void capture(const char *p,size_t n) {assert(n<sizeof(output));memcpy(output,p,n);output[n]=0;}
static void run(const char *input,const char *expected,int expected_saves) {
    char frame[1700];snprintf(frame,sizeof(frame),"%s",input);output[0]=0;
    network_usb_setup(frame,capture);assert(strstr(output,expected));assert(!strstr(output,"fixture-code"));
    assert(!strstr(output,"private-value"));assert(saves==expected_saves);assert(leases==0);
}
int main(void) {
    const char *save="1234abcd fixture-code save {\"password\":\"private-value\"}";
    run("1234abcd fixture-code status","1234abcd OK {}",0);
    run("1234abcd wrong-code save {}","1234abcd ERR ",0);
    status.setup_paused=true;run(save," ERR ",0);status.setup_paused=false;
    status.setup=false;run(save," ERR ",0);status.setup=true;
    permit=false;run(save," ERR ",0);permit=true;
    run("1234abcd fixture-code save [1]"," ERR ",0);
    run("1234abcd fixture-code save {\"password\":\"a\\u0000b\"}"," ERR ",0);
    run(save," OK {}",1);
    run("badid fixture-code status","",1);assert(output[0]==0);
    assert(setup_activity_seen&&setup_activity_at==10);
    puts("PASS production USB admission, parsing and response correlation; network storage stubbed");
}
