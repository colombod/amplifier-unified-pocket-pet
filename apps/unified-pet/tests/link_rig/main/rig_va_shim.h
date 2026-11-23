/* Host-ABI shim, applied ONLY to the pinned WebSocket client translation unit.
 *
 * Its error path formats one va_list twice without va_copy. The device's
 * Xtensa ABI passes va_list by value, so the second use starts afresh there
 * (checked with the target compiler: sizeof a va_list parameter equals
 * sizeof(va_list)). On x86-64 a va_list parameter is a pointer, the first use
 * consumes it and the second crashes. Give the host the device's by-value
 * behaviour instead of editing the pinned source. Nothing else is changed. */
#pragma once
#include <stdarg.h>
#include <stdio.h>
static inline int rig_vsnprintf_by_value(char *out,size_t n,const char *format,va_list args){
    va_list copy;va_copy(copy,args);
    int result=vsnprintf(out,n,format,copy);
    va_end(copy);return result;
}
#define vsnprintf rig_vsnprintf_by_value
