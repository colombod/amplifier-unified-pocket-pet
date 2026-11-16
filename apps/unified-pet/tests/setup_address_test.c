#include <assert.h>
#include <stdio.h>
#include <arpa/inet.h>
#include "setup_address.h"

int main(void)
{
    struct sockaddr_in v4 = {.sin_family = AF_INET};
    assert(inet_pton(AF_INET, "192.168.4.1", &v4.sin_addr) == 1);
    assert(setup_address_is_ap((struct sockaddr *)&v4, sizeof(v4)));
    assert(!setup_address_is_ap((struct sockaddr *)&v4, sizeof(v4)-1));
    assert(inet_pton(AF_INET, "192.168.1.20", &v4.sin_addr) == 1);
    assert(!setup_address_is_ap((struct sockaddr *)&v4, sizeof(v4)));

    struct sockaddr_in6 v6 = {.sin6_family = AF_INET6};
    assert(inet_pton(AF_INET6, "::ffff:192.168.4.1", &v6.sin6_addr) == 1);
    assert(setup_address_is_ap((struct sockaddr *)&v6, sizeof(v6)));
    /* The former getsockname buffer was too short for this valid endpoint. */
    assert(!setup_address_is_ap((struct sockaddr *)&v6, sizeof(v4)));
    assert(inet_pton(AF_INET6, "::ffff:192.168.1.20", &v6.sin6_addr) == 1);
    assert(!setup_address_is_ap((struct sockaddr *)&v6, sizeof(v6)));
    assert(inet_pton(AF_INET6, "::1", &v6.sin6_addr) == 1);
    assert(!setup_address_is_ap((struct sockaddr *)&v6, sizeof(v6)));
    assert(inet_pton(AF_INET6, "::", &v6.sin6_addr) == 1);
    assert(!setup_address_is_ap((struct sockaddr *)&v6, sizeof(v6)));
    assert(!setup_address_is_ap(NULL, 0));
    puts("PASS: IPv4/mapped IPv6 AP access; foreign, truncated and invalid endpoints denied");
}
