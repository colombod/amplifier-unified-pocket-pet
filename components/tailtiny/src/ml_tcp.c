/* TailTiny routed TCP: deadline/cancellation, mandatory VPN source bind.
 * Derived from TailTiny v3.1.0 ml_tcp.c; upstream MIT notice in ../LICENSE. */
#include "tailtiny_internal.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "lwip/sockets.h"
#include <fcntl.h>
#include <errno.h>
#include <stdlib.h>
#include <unistd.h>
struct tailtiny_tcp_socket {
    tailtiny_t *ml;
    int fd;
    bool connected;
};
static uint64_t tcp_now(void) {return (uint64_t)esp_timer_get_time()/1000;}
/* Numeric diagnostic reasons: 1 deadline, 2 cancel, 3 shutdown, 4 syscall,
 * 5 protocol/validation, 6 allocation, 7 success. Preserve short-circuit order. */
static unsigned tcp_stop_reason(tailtiny_t *ml,uint64_t until,tailtiny_cancel_fn cancel,void *arg) {
    if(tcp_now()>=until)return 1;
    if(cancel&&cancel(arg))return 2;
    return (xEventGroupGetBits(ml->events)&ML_EVT_SHUTDOWN_REQUEST)?3:0;
}
static bool done(tailtiny_t *ml,uint64_t until,tailtiny_cancel_fn cancel,void *arg) {
    return tcp_stop_reason(ml,until,cancel,arg)!=0;
}
static void tcp_observe(tailtiny_t *ml,uint32_t ip,uint16_t port,unsigned phase,unsigned reason,
        int syscall_errno,int soerr,uint64_t wg_ms,uint64_t conn_ms) {
    int saved_errno=errno;
    uint32_t requested=ml?__atomic_load_n(&ml->app_connect_ip,__ATOMIC_ACQUIRE):0;
    uint32_t ready=ml?__atomic_load_n(&ml->wg_ready_ip,__ATOMIC_ACQUIRE):0;
    uint32_t policy=ml?__atomic_load_n(&ml->policy.ip,__ATOMIC_ACQUIRE):0;
    esp_log_write(ESP_LOG_INFO,"tailnet_tcp",
        "tcp_diag phase=%u reason=%u errno=%d soerr=%d wgMs=%u connMs=%u dst=%u req=%u ready=%u match=%u policy=%u port=%u\n",
        phase,reason,syscall_errno,soerr,(unsigned)wg_ms,(unsigned)conn_ms,
        (unsigned)ip,(unsigned)requested,(unsigned)ready,(unsigned)(ip&&ready==ip),
        (unsigned)policy,(unsigned)port);
    errno=saved_errno;
}
tailtiny_tcp_socket_t *tailtiny_tcp_connect_until(tailtiny_t *ml,uint32_t ip,uint16_t port,
        uint64_t until,tailtiny_cancel_fn cancel,void *arg) {
    /* Phases: VALIDATE=1 SOCKET=2 BIND=3 FCNTL=4 WG_WAIT=5 CONNECT=6
     * SELECT=7 SO_ERROR=8 ALLOC=9 OK=10. One terminal observation per call. */
    unsigned phase=1,reason=5;
    int syscall_errno=0,soerr=0,fd=-1;
    uint64_t wg_began=0,wg_ms=0,conn_began=0;
    bool connecting=false;
    tailtiny_tcp_socket_t *sock=NULL;
    if(!ml||!ip||!port||!ml->vpn_ip||ml->state!=ML_STATE_CONNECTED)goto observed;
    tailtiny_runtime_status_t s;tailtiny_runtime_snapshot(ml,&s);
    if(s.peer_conflict||s.peer_ip!=ip||port!=ml->policy.port)goto observed;
    if((reason=tcp_stop_reason(ml,until,cancel,arg)))goto observed;
    /* Only wg_mgr owns WG state. Its periodic loop handshakes admitted peer.
     * No cross-task direct calls into the wireguard engine here. */
    phase=2;fd=socket(AF_INET,SOCK_STREAM,IPPROTO_TCP);
    if(fd<0){syscall_errno=errno;reason=4;goto observed;}
    struct sockaddr_in src={.sin_family=AF_INET,.sin_addr.s_addr=htonl(ml->vpn_ip)};
    phase=3;
    if(bind(fd,(struct sockaddr *)&src,sizeof(src))!=0){syscall_errno=errno;reason=4;goto fail;}
    phase=4;
    int flags=fcntl(fd,F_GETFL,0);
    if(flags<0||fcntl(fd,F_SETFL,flags|O_NONBLOCK)<0){syscall_errno=errno;reason=4;goto fail;}
    __atomic_store_n(&ml->app_connect_ip,ip,__ATOMIC_RELEASE);
    phase=5;wg_began=tcp_now();
    while(__atomic_load_n(&ml->wg_ready_ip,__ATOMIC_ACQUIRE)!=ip) {
        if((reason=tcp_stop_reason(ml,until,cancel,arg))){wg_ms=tcp_now()-wg_began;goto fail;}
        vTaskDelay(pdMS_TO_TICKS(25));
    }
    wg_ms=tcp_now()-wg_began;
    struct sockaddr_in dst={.sin_family=AF_INET,.sin_port=htons(port),.sin_addr.s_addr=htonl(ip)};
    phase=6;conn_began=tcp_now();connecting=true;
    int rc=connect(fd,(struct sockaddr *)&dst,sizeof(dst));
    if(rc!=0 && errno!=EINPROGRESS){syscall_errno=errno;reason=4;goto fail;}
    while(rc!=0) {
        phase=7;
        if((reason=tcp_stop_reason(ml,until,cancel,arg)))goto fail;
        fd_set w;FD_ZERO(&w);FD_SET(fd,&w);
        struct timeval tv={.tv_sec=0,.tv_usec=50000};
        int n=select(fd+1,NULL,&w,NULL,&tv);
        if(n<0&&errno!=EINTR){syscall_errno=errno;reason=4;goto fail;}
        if(n>0) {
            phase=8;
            int e=0;socklen_t len=sizeof(e);
            if(getsockopt(fd,SOL_SOCKET,SO_ERROR,&e,&len)!=0){syscall_errno=errno;reason=4;goto fail;}
            soerr=e;
            if(e){reason=4;goto fail;}
            rc=0;
        }
    }
    if((reason=tcp_stop_reason(ml,until,cancel,arg)))goto fail;
    phase=9;sock=calloc(1,sizeof(*sock));
    if(!sock){reason=6;goto fail;}
    sock->ml=ml;sock->fd=fd;sock->connected=true;
    phase=10;reason=7;goto observed;
fail:
    close(fd);
observed:
    tcp_observe(ml,ip,port,phase,reason,syscall_errno,soerr,wg_ms,connecting?tcp_now()-conn_began:0);
    return sock;
}
tailtiny_tcp_socket_t *tailtiny_tcp_connect(tailtiny_t *ml,uint32_t ip,uint16_t port,uint32_t timeout) {
    return tailtiny_tcp_connect_until(ml,ip,port,tcp_now()+(timeout?timeout:15000),NULL,NULL);
}
int tailtiny_tcp_borrow_fd(const tailtiny_tcp_socket_t *s) {return s&&s->connected?s->fd:-1;}
esp_err_t tailtiny_tcp_send(tailtiny_tcp_socket_t *s,const void *data,size_t len) {
    if(!s||!s->connected||!data)return ESP_ERR_INVALID_ARG;
    uint64_t until=tcp_now()+15000;size_t sent=0;
    while(sent<len&&!done(s->ml,until,NULL,NULL)) {
        int n=send(s->fd,(const uint8_t *)data+sent,len-sent,0);
        if(n>0)sent+=n;
        else if(n<0&&(errno==EAGAIN||errno==EWOULDBLOCK||errno==EINTR))vTaskDelay(pdMS_TO_TICKS(10));
        else return ESP_FAIL;
    }
    return sent==len?ESP_OK:ESP_ERR_TIMEOUT;
}
int tailtiny_tcp_recv(tailtiny_tcp_socket_t *s,void *buf,size_t len,uint32_t timeout) {
    if(!s||!s->connected||!buf)return -1;
    uint64_t until=tcp_now()+(timeout?timeout:10000);
    do {
        int n=recv(s->fd,buf,len,0);
        if(n>0)return n;
        if(n==0){s->connected=false;return -1;}
        if(errno!=EAGAIN&&errno!=EWOULDBLOCK&&errno!=EINTR)return -1;
        vTaskDelay(pdMS_TO_TICKS(10));
    }while(!done(s->ml,until,NULL,NULL));
    return 0;
}
bool tailtiny_tcp_is_connected(const tailtiny_tcp_socket_t *s) {return s&&s->connected;}
void tailtiny_tcp_close(tailtiny_tcp_socket_t *s) {
    if(!s)return;
    if(s->fd>=0){shutdown(s->fd,SHUT_RDWR);close(s->fd);}
    free(s);
}
