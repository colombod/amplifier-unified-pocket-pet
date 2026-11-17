#pragma once
/* Host-only configuration for the pinned SDK's unmodified lwIP pbuf/mem core. */
#define NO_SYS 1
#define SYS_LIGHTWEIGHT_PROT 0
#define LWIP_IPV4 1
#define LWIP_IPV6 0
#define LWIP_TCP 0
#define LWIP_UDP 0
#define LWIP_RAW 0
#define LWIP_IGMP 0
#define LWIP_ICMP 0
#define IP_FRAG 0
#define IP_REASSEMBLY 0
#define LWIP_DNS 0
#define LWIP_DHCP 0
#define LWIP_AUTOIP 0
#define LWIP_SOCKET 0
#define LWIP_NETCONN 0
#define LWIP_STATS 0
#define MEM_LIBC_MALLOC 1
#define MEMP_MEM_MALLOC 1
#define MEM_ALIGNMENT 8
#define LWIP_SUPPORT_CUSTOM_PBUF 0
#define LWIP_NETIF_LOOPBACK 0
#define LWIP_TIMERS 0
#include <stddef.h>
void *rx_malloc(size_t);
void *rx_calloc(size_t,size_t);
void rx_free(void *);
#define mem_clib_malloc rx_malloc
#define mem_clib_calloc rx_calloc
#define mem_clib_free rx_free