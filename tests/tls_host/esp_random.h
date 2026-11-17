#pragma once
#include <stddef.h>
#include <sys/random.h>
#include <stdlib.h>
static inline void esp_fill_random(void *p,size_t n) {
    if(getrandom(p,n,0)!=(ssize_t)n)abort();
}