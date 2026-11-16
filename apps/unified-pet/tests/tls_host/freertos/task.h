#pragma once
#include <time.h>
#ifdef TCP_DIAG_TEST
void vTaskDelay(unsigned ms);
#else
static inline void vTaskDelay(unsigned ms) {
    struct timespec t={.tv_sec=ms/1000,.tv_nsec=(long)(ms%1000)*1000000};
    nanosleep(&t,NULL);
}
#endif