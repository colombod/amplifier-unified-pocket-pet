#pragma once
#include <stdarg.h>
static inline void enrollment_discard_log(const char *tag,const char *format,...) {
    (void)tag;(void)format;
}
#define ESP_LOGD enrollment_discard_log
#define ESP_LOGI enrollment_discard_log
#define ESP_LOGW enrollment_discard_log
#define ESP_LOGE enrollment_discard_log