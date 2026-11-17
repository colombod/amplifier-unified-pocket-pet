#pragma once
/* Hardware/RTOS-only stand-ins for compiling production ml_h2.c. The test
 * translation unit supplies the same declarations before exact source bodies. */
#include "tailtiny.h"
#include <stdlib.h>
#include <string.h>
#define ML_H2_BUFFER_SIZE (256 * 1024)
void *ml_psram_malloc(size_t n);