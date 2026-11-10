#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>

/* HTTPD can return an IPv4-mapped address from its dual-stack listener.
 * Authorize the AP's local endpoint in either socket representation. */
static inline bool setup_address_is_ap(const struct sockaddr *address, socklen_t size)
{
    if (!address || size < sizeof(struct sockaddr)) return false;
    if (address->sa_family == AF_INET && size >= sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *v4 = (const struct sockaddr_in *)address;
        return v4->sin_addr.s_addr == htonl(UINT32_C(0xc0a80401));
    }
    if (address->sa_family == AF_INET6 && size >= sizeof(struct sockaddr_in6)) {
        const struct sockaddr_in6 *v6 = (const struct sockaddr_in6 *)address;
        static const unsigned char ap_mapped[16] = {
            0,0,0,0,0,0,0,0,0,0,255,255,192,168,4,1
        };
        return memcmp(&v6->sin6_addr, ap_mapped, sizeof(ap_mapped)) == 0;
    }
    return false;
}
