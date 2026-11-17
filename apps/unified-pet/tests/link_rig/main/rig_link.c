/* The radio seam: an Ethernet tap between the device's lwIP and the host
 * kernel, with scripted impairments. Everything above it (lwIP TCP, TLS,
 * WebSocket, the transport wrapper, the voice owner) is production code.
 *
 * RIG_LINK is a ';' separated list of  kind:dir:from_ms:to_ms:value
 *   kind  delay (value = added one-way ms), loss (value = per mille),
 *         drop (value ignored: every packet is lost), rate (value = kbit/s)
 *   dir   up (device to host), down (host to device), both
 *   from/to  milliseconds after the device first shows "listening";
 *         0:0 means always, including before the call is up.
 * A dropped packet is gone, as on a radio: recovery is TCP's own. */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "esp_err.h"
#include "esp_netif.h"
#include "esp_netif_net_stack.h"
#include "lwip/esp_netif_net_stack.h"
#include "rig_tap.h"
#include "lwip/opt.h"
#include "lwip/pbuf.h"
#include "lwip/ip_addr.h"
#include "netif/etharp.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_timer.h"
#include "rig.h"

#define MTU_FRAME 1518
#define QUEUE 512
enum {K_DELAY,K_LOSS,K_DROP,K_RATE};
enum {D_UP=1,D_DOWN=2};
typedef struct {int kind,dir;long from,to,value;} rule_t;
typedef struct {unsigned char *data;size_t len;int64_t due;} packet_t;
typedef struct {packet_t q[QUEUE];unsigned head,n;int64_t last_due,next_free;
                unsigned long passed,lost,dropped,shaped,full;} lane_t;
typedef struct {esp_netif_driver_base_t base;int fd;} rig_io_t;

static rig_io_t io={.fd=-1};
static rule_t rules[32];
static unsigned rule_n;
static lane_t up,down;
static SemaphoreHandle_t lock;
static uint32_t prng=0x2026u;

static unsigned draw(void){prng^=prng<<13;prng^=prng>>17;prng^=prng<<5;return prng;}
static void parse_rules(void){
    char *copy=strdup(rig_env_text("RIG_LINK")),*save=NULL;
    for(char *item=strtok_r(copy,";",&save);item&&rule_n<32;item=strtok_r(NULL,";",&save)){
        char kind[8],dir[8];rule_t r={0};
        if(sscanf(item,"%7[a-z]:%7[a-z]:%ld:%ld:%ld",kind,dir,&r.from,&r.to,&r.value)!=5){
            fprintf(stderr,"RIG bad link rule: %s\n",item);exit(2);
        }
        r.kind=!strcmp(kind,"delay")?K_DELAY:!strcmp(kind,"loss")?K_LOSS:!strcmp(kind,"drop")?K_DROP:
               !strcmp(kind,"rate")?K_RATE:-1;
        r.dir=!strcmp(dir,"up")?D_UP:!strcmp(dir,"down")?D_DOWN:!strcmp(dir,"both")?D_UP|D_DOWN:0;
        if(r.kind<0||!r.dir){fprintf(stderr,"RIG bad link rule: %s\n",item);exit(2);}
        rules[rule_n++]=r;
    }
    free(copy);
    prng=(uint32_t)rig_env("RIG_SEED",0x2026)|1u;
}
static bool active(const rule_t *r,int dir){
    if(!(r->dir&dir))return false;
    if(!r->from&&!r->to)return true;
    if(!rig_listening_seen())return false;
    int64_t t=rig_since_listening_ms();
    return t>=r->from&&(!r->to||t<r->to);
}
/* lock held. Takes ownership of nothing: copies the frame when it is queued. */
static void offer(lane_t *lane,int dir,const void *frame,size_t len){
    int64_t now=esp_timer_get_time(),delay=0;long rate=0;
    for(unsigned i=0;i<rule_n;i++){
        const rule_t *r=&rules[i];
        if(!active(r,dir))continue;
        if(r->kind==K_DROP){lane->dropped++;return;}
        if(r->kind==K_LOSS&&(long)(draw()%1000u)<r->value){lane->lost++;return;}
        if(r->kind==K_DELAY)delay+=r->value*1000;
        if(r->kind==K_RATE)rate=r->value;
    }
    int64_t start=now;
    if(rate>0){
        if(lane->next_free>start)start=lane->next_free;
        if(start-now>200000){lane->shaped++;return;} /* a 200 ms radio queue, then tail drop */
        lane->next_free=start+(int64_t)len*8*1000/rate;
    }
    if(lane->n==QUEUE){lane->full++;return;}
    packet_t *p=&lane->q[(lane->head+lane->n)%QUEUE];
    p->data=malloc(len);if(!p->data){lane->full++;return;}
    memcpy(p->data,frame,len);p->len=len;
    p->due=start+delay;
    if(p->due<lane->last_due)p->due=lane->last_due; /* a link does not reorder */
    lane->last_due=p->due;lane->n++;lane->passed++;
}
/* lock held. */
static bool take_due(lane_t *lane,packet_t *out){
    if(!lane->n||lane->q[lane->head].due>esp_timer_get_time())return false;
    *out=lane->q[lane->head];lane->head=(lane->head+1)%QUEUE;lane->n--;return true;
}
static void link_task(void *arg){
    (void)arg;unsigned char frame[MTU_FRAME];
    for(;;){
        for(unsigned i=0;i<64;i++){
            long n=rig_tap_read(io.fd,frame,sizeof(frame));
            if(n<=0)break;
            xSemaphoreTake(lock,portMAX_DELAY);offer(&down,D_DOWN,frame,(size_t)n);xSemaphoreGive(lock);
        }
        for(;;){
            packet_t p;
            xSemaphoreTake(lock,portMAX_DELAY);bool have=take_due(&down,&p);xSemaphoreGive(lock);
            if(!have)break;
            esp_netif_receive(io.base.netif,p.data,p.len,NULL);free(p.data);
        }
        for(;;){
            packet_t p;
            xSemaphoreTake(lock,portMAX_DELAY);bool have=take_due(&up,&p);xSemaphoreGive(lock);
            if(!have)break;
            if(rig_tap_write(io.fd,p.data,p.len)!=(long)p.len)fprintf(stderr,"RIG tap write failed\n");
            free(p.data);
        }
        vTaskDelay(1);
    }
}
static esp_err_t transmit(void *h,void *buffer,size_t len){
    (void)h;
    xSemaphoreTake(lock,portMAX_DELAY);offer(&up,D_UP,buffer,len);xSemaphoreGive(lock);
    return ESP_OK;
}
static esp_err_t post_attach(esp_netif_t *netif,void *arg){
    rig_io_t *self=arg;self->base.netif=netif;
    esp_netif_action_start(netif,0,0,0);
    xTaskCreate(link_task,"rig_link",8192,NULL,23,NULL); /* the Wi-Fi task's priority */
    return ESP_OK;
}
static err_t link_output(struct netif *netif,struct pbuf *p){
    unsigned char frame[MTU_FRAME];
    if(p->tot_len>sizeof(frame))return ERR_IF;
    pbuf_copy_partial(p,frame,p->tot_len,0);
    return esp_netif_transmit(esp_netif_get_handle_from_netif_impl(netif),frame,p->tot_len)==ESP_OK?ERR_OK:ERR_IF;
}
static err_t stack_init(struct netif *netif){
    netif->name[0]='r';netif->name[1]='g';
    netif->output=etharp_output;netif->linkoutput=link_output;netif->mtu=1500;
    const unsigned char mac[6]={0x02,0x12,0x34,0x56,0x78,0xab};
    memcpy(netif->hwaddr,mac,6);netif->hwaddr_len=6;
    netif->flags=NETIF_FLAG_BROADCAST|NETIF_FLAG_ETHARP;
    netif_set_link_up(netif);
    return ERR_OK;
}
static void stack_input(void *h,void *buffer,size_t len,void *l2){
    (void)l2;struct netif *netif=h;
    struct pbuf *p=pbuf_alloc(PBUF_RAW,len,PBUF_RAM);
    if(!p)return;
    memcpy(p->payload,buffer,len);
    if(netif->input(p,netif)!=ERR_OK)pbuf_free(p);
}
void rig_link_start(void){
    parse_rules();
    lock=xSemaphoreCreateMutex();
    io.fd=rig_tap_open(*rig_env_text("RIG_TAP")?rig_env_text("RIG_TAP"):"rigtap0");
    if(io.fd<0){perror("RIG tap open");exit(2);}
    io.base.post_attach=post_attach;
    esp_netif_driver_ifconfig_t driver={.handle=&io,.transmit=transmit};
    struct esp_netif_netstack_config stack={.lwip={.init_fn=stack_init,.input_fn=stack_input}};
    esp_netif_ip_info_t ip={0};
    ip.ip.addr=ipaddr_addr("192.168.77.2");ip.netmask.addr=ipaddr_addr("255.255.255.0");
    ip.gw.addr=ipaddr_addr("192.168.77.1");
    esp_netif_inherent_config_t base={.if_key="RIG",.ip_info=&ip,.flags=ESP_NETIF_FLAG_AUTOUP,.route_prio=100};
    esp_netif_config_t cfg={.base=&base,.driver=&driver,.stack=&stack};
    esp_netif_t *netif=esp_netif_new(&cfg);
    if(!netif||esp_netif_attach(netif,&io)!=ESP_OK){fprintf(stderr,"RIG netif failed\n");exit(2);}
}
void rig_link_report(void){
    printf("RIG link up passed=%lu lost=%lu dropped=%lu shaped=%lu full=%lu\n",up.passed,up.lost,up.dropped,up.shaped,up.full);
    printf("RIG link down passed=%lu lost=%lu dropped=%lu shaped=%lu full=%lu\n",down.passed,down.lost,down.dropped,down.shaped,down.full);
}
