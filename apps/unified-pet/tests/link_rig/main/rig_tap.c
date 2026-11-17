/* Host file-descriptor I/O for the tap device. Kept apart from the lwIP glue:
 * on the linux target the SDK maps the socket headers to lwIP unless this is
 * defined before any of them is seen. */
#define LWIP_HDR_LINUX_SYS_SOCKETS_H
#include <sys/socket.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/types.h>
#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <linux/if.h>
#include <linux/if_tun.h>
#include "rig_tap.h"

int rig_tap_open(const char *name){
    int fd=open("/dev/net/tun",O_RDWR|O_NONBLOCK);
    if(fd<0)return -1;
    struct ifreq ifr;memset(&ifr,0,sizeof(ifr));
    snprintf(ifr.ifr_name,sizeof(ifr.ifr_name),"%s",name);
    ifr.ifr_flags=IFF_TAP|IFF_NO_PI;
    if(ioctl(fd,TUNSETIFF,(void*)&ifr)<0){close(fd);return -1;}
    return fd;
}
long rig_tap_read(int fd,void *frame,unsigned long cap){return (long)read(fd,frame,cap);}
long rig_tap_write(int fd,const void *frame,unsigned long len){return (long)write(fd,frame,len);}
