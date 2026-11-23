#pragma once
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#define LWIP_PLATFORM_ASSERT(message) do { (void)(message);assert(0); } while(0)
#define LWIP_PLATFORM_DIAG(message) do { } while(0)