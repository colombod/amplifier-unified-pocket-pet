#include <assert.h>
#include <stdio.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
#include "tailtiny_internal.h"
#include "esp_timer.h"
#include "lwip/sockets.h"
/* Actual ml_tcp.c linked separately. Only syscall/clock/RTOS seams are replaced. */
static tailtiny_t ml;
static uint64_t clock_ms,ready_at,cancel_at,shutdown_at;
static unsigned sockets,binds,connects,closes,selects,logs,cases;
static unsigned phase,reason,wg_ms,conn_ms,dst,req,ready,match,policy,port;
static int diag_errno,soerr;
static bool socket_fail,bind_fail,getfl_fail,setfl_fail,connect_fail;
static bool select_fail,getopt_fail,allocation_fail,immediate_connect;
static bool select_ready,select_interrupt;
static int socket_error;
int64_t esp_timer_get_time(void) {return (int64_t)clock_ms*1000;}
void vTaskDelay(unsigned ms) {
    assert(ms>0&&ms<=25);clock_ms+=ms;
    if(ready_at&&clock_ms>=ready_at)__atomic_store_n(&ml.wg_ready_ip,ml.policy.ip,__ATOMIC_RELEASE);
}
int test_socket(int family,int type,int protocol) {
    assert(family==AF_INET&&type==SOCK_STREAM&&protocol==IPPROTO_TCP);sockets++;
    if(socket_fail){errno=EMFILE;return -1;}return 41;
}
int test_bind(int fd,const struct sockaddr *addr,socklen_t n) {
    assert(fd==41&&n==sizeof(struct sockaddr_in));
    assert(ntohl(((const struct sockaddr_in *)addr)->sin_addr.s_addr)==0x64400101);
    binds++;if(bind_fail){errno=EADDRNOTAVAIL;return -1;}return 0;
}
int test_fcntl(int fd,int cmd,...) {
    assert(fd==41);
    if((cmd==F_GETFL&&getfl_fail)||(cmd==F_SETFL&&setfl_fail)){errno=EBADF;return -1;}
    assert(cmd==F_GETFL||cmd==F_SETFL);return 0;
}
int test_connect(int fd,const struct sockaddr *addr,socklen_t n) {
    assert(fd==41&&n==sizeof(struct sockaddr_in));connects++;
    assert(ntohl(((const struct sockaddr_in *)addr)->sin_addr.s_addr)==ml.policy.ip);
    assert(ntohs(((const struct sockaddr_in *)addr)->sin_port)==8941);
    if(connect_fail){errno=ENETUNREACH;return -1;}
    if(immediate_connect)return 0;
    errno=EINPROGRESS;return -1;
}
int test_select(int n,fd_set *r,fd_set *w,fd_set *e,struct timeval *tv) {
    assert(n==42&&!r&&w&&!e&&tv->tv_usec==50000);selects++;clock_ms+=50;
    if(select_fail){errno=EIO;return -1;}
    if(select_interrupt&&selects==1){errno=EINTR;return -1;}
    return socket_error||getopt_fail||select_ready?1:0;
}
int test_getsockopt(int fd,int level,int option,void *value,socklen_t *len) {
    assert(fd==41&&level==SOL_SOCKET&&option==SO_ERROR&&*len==sizeof(int));
    if(getopt_fail){errno=ENOPROTOOPT;return -1;}
    *(int *)value=socket_error;return 0;
}
int test_close(int fd) {assert(fd==41);closes++;errno=EIO;return 0;}
void *test_calloc(size_t n,size_t size) {return allocation_fail?NULL:calloc(n,size);}
void tailtiny_runtime_snapshot(tailtiny_t *p,tailtiny_runtime_status_t *s) {
    *s=(tailtiny_runtime_status_t){.peer_ip=__atomic_load_n(&p->policy.ip,__ATOMIC_ACQUIRE)};
}
static unsigned mgr_entry,mgr_return,mgr_ready,triggers,cmms,up_calls;
static bool up_result;static int trigger_result;
void esp_log_write(int level,const char *tag,const char *format,...) {
    assert(level==ESP_LOG_INFO);char text[300];va_list args;va_start(args,format);
    int n=vsnprintf(text,sizeof(text),format,args);va_end(args);
    assert(n>0&&(size_t)n<sizeof(text));
    if(!strcmp(tag,"tailnet_tcp")) {
        logs++;
        assert(sscanf(text,"tcp_diag phase=%u reason=%u errno=%d soerr=%d wgMs=%u connMs=%u dst=%u req=%u ready=%u match=%u policy=%u port=%u",
            &phase,&reason,&diag_errno,&soerr,&wg_ms,&conn_ms,&dst,&req,&ready,&match,&policy,&port)==12);
    }else{
        assert(!strcmp(tag,"tailnet_wg"));
        unsigned p,r,u,rd;int ret;
        assert(sscanf(text,"wg_app phase=%u req=%u trigger=%d wgUp=%u ready=%u",&p,&r,&ret,&u,&rd)==5);
        assert(r==ml.policy.ip);
        if(p==1){mgr_entry++;assert(!triggers&&!cmms);}
        else if(p==2){mgr_return++;assert(triggers==1&&!cmms&&ret==trigger_result);}
        else {assert(p==3&&u==1&&rd==ml.policy.ip&&triggers==1&&cmms==1);mgr_ready++;}
    }
    /* Prove diagnostic logging cannot corrupt caller-visible errno. */
    errno=ERANGE;
}
static int ml_wg_mgr_trigger_handshake(tailtiny_t *p,uint32_t ip) {
    assert(p==&ml&&ip==ml.policy.ip&&mgr_entry==1);triggers++;return trigger_result;
}
static void ml_wg_mgr_send_cmm(tailtiny_t *p,uint32_t ip) {
    assert(p==&ml&&ip==ml.policy.ip&&mgr_return==1);cmms++;
}
static bool ml_wg_mgr_peer_is_up(tailtiny_t *p,uint32_t ip) {
    assert(p==&ml&&ip==ml.policy.ip);up_calls++;return up_result;
}
#include "tcp-manager.inc"
static bool cancel(void *arg) {
    (void)arg;
    if(shutdown_at&&clock_ms>=shutdown_at)ml.events=ML_EVT_SHUTDOWN_REQUEST;
    return cancel_at&&clock_ms>=cancel_at;
}
static void reset(void) {
    ml=(tailtiny_t){.state=ML_STATE_CONNECTED,.vpn_ip=0x64400101,.policy={.ip=0x64400102,.port=8941}};
    clock_ms=1000;ready_at=cancel_at=shutdown_at=0;
    sockets=binds=connects=closes=selects=logs=0;
    socket_fail=bind_fail=getfl_fail=setfl_fail=connect_fail=false;
    select_fail=getopt_fail=allocation_fail=immediate_connect=false;socket_error=0;
    select_ready=select_interrupt=false;
    phase=reason=wg_ms=conn_ms=dst=req=ready=match=policy=port=0;diag_errno=soerr=0;
    mgr_entry=mgr_return=mgr_ready=triggers=cmms=up_calls=0;up_result=false;trigger_result=0;
    errno=0;
}
static tailtiny_tcp_socket_t *call(void) {
    return tailtiny_tcp_connect_until(&ml,ml.policy.ip,8941,1100,cancel,NULL);
}
static void check(unsigned p,unsigned r) {
    assert(logs==1&&phase==p&&reason==r&&dst==ml.policy.ip&&policy==ml.policy.ip&&port==8941);
    assert(req==ml.app_connect_ip&&ready==ml.wg_ready_ip&&match==(ready==dst));
}
static void pass(const char *name){cases++;printf("PASS routed-tcp %s\n",name);}
int main(void) {
    reset();assert(!call()&&!connects&&closes==1);
    assert(logs==1); /* Functional RED: original WG timeout had no observation. */
    check(5,1);assert(wg_ms==100&&!conn_ms&&!diag_errno&&!soerr);pass("WG-timeout-never-connects");
    reset();ready_at=1025;assert(!call());check(7,1);
    assert(connects==1&&closes==1&&wg_ms==25&&conn_ms==100);pass("ready-then-connect-timeout-distinct");
    reset();bind_fail=true;assert(!call());check(3,4);
    assert(diag_errno==EADDRNOTAVAIL&&!connects&&closes==1&&errno==EIO);pass("bind-errno-survives-close-and-log");
    reset();ml.wg_ready_ip=ml.policy.ip;socket_error=ECONNREFUSED;assert(!call());check(8,4);
    assert(!diag_errno&&soerr==ECONNREFUSED&&closes==1);pass("SO_ERROR-not-errno-survives-close");
    reset();socket_fail=true;assert(!call());check(2,4);
    assert(diag_errno==EMFILE&&!closes&&errno==EMFILE);pass("socket-failure");
    for(unsigned i=0;i<2;i++){
        reset();getfl_fail=i==0;setfl_fail=i==1;assert(!call());check(4,4);
        assert(diag_errno==EBADF&&closes==1&&!connects);pass("fcntl-failure");
    }
    reset();ml.wg_ready_ip=ml.policy.ip;connect_fail=true;assert(!call());check(6,4);
    assert(diag_errno==ENETUNREACH&&closes==1);pass("connect-syscall-failure");
    reset();ml.wg_ready_ip=ml.policy.ip;select_fail=true;assert(!call());check(7,4);
    assert(diag_errno==EIO&&closes==1);pass("select-failure");
    reset();ml.wg_ready_ip=ml.policy.ip;getopt_fail=true;assert(!call());check(8,4);
    assert(diag_errno==ENOPROTOOPT&&!soerr&&closes==1);pass("getsockopt-failure");
    reset();cancel_at=1050;assert(!call());check(5,2);assert(!connects&&closes==1);pass("cancel-during-WG-wait");
    reset();shutdown_at=1050;assert(!call());check(5,3);assert(!connects&&closes==1);pass("shutdown-during-WG-wait");
    reset();ml.wg_ready_ip=ml.policy.ip;cancel_at=1050;assert(!call());check(7,2);pass("cancel-during-connect");
    reset();ml.wg_ready_ip=ml.policy.ip;shutdown_at=1050;assert(!call());check(7,3);pass("shutdown-during-connect");
    reset();ml.wg_ready_ip=ml.policy.ip;immediate_connect=true;allocation_fail=true;
    assert(!call());check(9,6);assert(closes==1);pass("allocation-failure");
    reset();ml.wg_ready_ip=ml.policy.ip;immediate_connect=true;
    tailtiny_tcp_socket_t *s=call();assert(s&&tailtiny_tcp_borrow_fd(s)==41&&!closes);check(10,7);
    tailtiny_tcp_close(s);assert(closes==1&&logs==1);pass("success-ownership-cleanup");
    reset();ml.wg_ready_ip=ml.policy.ip;select_ready=true;
    s=call();assert(s&&connects==1&&selects==1&&!closes);check(10,7);
    assert(conn_ms==50&&!soerr);tailtiny_tcp_close(s);assert(closes==1);pass("async-connect-SO_ERROR-zero-success");
    reset();ml.wg_ready_ip=ml.policy.ip;select_interrupt=true;
    assert(!call());check(7,1);assert(selects==2&&!diag_errno);pass("select-EINTR-retried-within-existing-budget");
    reset();assert(!tailtiny_tcp_connect_until(NULL,0,0,1100,NULL,NULL));
    assert(logs==1&&phase==1&&reason==5&&!req&&!ready&&!match&&!policy);pass("null-validation-safe");
    reset();assert(!tailtiny_tcp_connect_until(&ml,ml.policy.ip,80,1100,NULL,NULL));
    assert(logs==1&&phase==1&&reason==5&&!sockets);pass("wrong-port-no-socket");
    reset();assert(!tailtiny_tcp_connect_until(&ml,ml.policy.ip+1,8941,1100,NULL,NULL));
    assert(logs==1&&phase==1&&reason==5&&!sockets);pass("wrong-peer-no-socket");
    reset();cancel_at=1000;assert(!call());check(1,2);assert(!sockets);pass("initial-cancel");
    reset();ml.events=ML_EVT_SHUTDOWN_REQUEST;assert(!call());check(1,3);assert(!sockets);pass("initial-shutdown");
    reset();clock_ms=1100;assert(!call());check(1,1);assert(!sockets);pass("initial-deadline");
    for(unsigned invalid=0;invalid<4;invalid++) {
        reset();
        if(invalid==0)ml.state=ML_STATE_ERROR;
        if(invalid==1)ml.vpn_ip=0;
        uint32_t ip=invalid==2?0:ml.policy.ip;
        uint16_t p=invalid==3?0:8941;
        assert(!tailtiny_tcp_connect_until(&ml,ip,p,1100,NULL,NULL));
        assert(logs==1&&phase==1&&reason==5&&!sockets);pass("invalid-input-single-observation");
    }
    reset();bool requested_once=false,ready_once=false;
    manager_poll(&ml,&requested_once,&ready_once);assert(!mgr_entry&&!triggers);
    ml.app_connect_ip=ml.policy.ip;trigger_result=-1;
    for(unsigned i=0;i<4;i++)manager_poll(&ml,&requested_once,&ready_once);
    assert(mgr_entry==1&&mgr_return==1&&!mgr_ready&&triggers==1&&cmms==1&&!ml.wg_ready_ip);
    up_result=true;
    for(unsigned i=0;i<4;i++)manager_poll(&ml,&requested_once,&ready_once);
    assert(mgr_ready==1&&triggers==1&&cmms==1&&ml.wg_ready_ip==ml.policy.ip);
    up_result=false;manager_poll(&ml,&requested_once,&ready_once);assert(!ml.wg_ready_ip&&mgr_ready==1);
    pass("manager-ignored-trigger-return-one-shot-first-ready-only");
    printf("PASS routed-tcp total=%u; actual TCP source and extracted manager block\n",cases);
}