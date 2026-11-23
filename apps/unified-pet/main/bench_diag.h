#pragma once
#if defined(PET_PRIVATE_MIC_DIAG)
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
/* Private, fixed-size records. Names are firmware task names, never handles.
 * Keep a copy: TaskStatus_t.pcTaskName becomes invalid after task deletion. */
typedef struct {
    char name[configMAX_TASK_NAME_LEN];
    unsigned number,priority;
    int core;
    uint32_t runtime;
} pet_bench_task_t;
static inline void pet_bench_task_copy(pet_bench_task_t *out,const TaskStatus_t *in){
    snprintf(out->name,sizeof(out->name),"%s",in->pcTaskName);
    out->number=in->xTaskNumber;out->priority=in->uxCurrentPriority;
    out->core=in->xCoreID==tskNO_AFFINITY?-1:in->xCoreID;out->runtime=in->ulRunTimeCounter;
}
static inline void pet_bench_task_final(pet_bench_task_t *out){
    TaskStatus_t s;
    vTaskGetInfo(NULL,&s,pdFALSE,eInvalid);
    pet_bench_task_copy(out,&s);
}
typedef struct {
    uint64_t write_wait_us_total,write_wait_us_max,write_wait_count;
    uint64_t write_us_total,write_us_max;
    uint64_t read_hold_us_total,read_hold_us_max,read_hold_count,idle_read_polls;
} pet_bench_io_t;
typedef struct {
    pet_bench_io_t io;
    pet_bench_task_t task;
} pet_bench_transport_t;
static inline void pet_bench_time(uint64_t *total,uint64_t *max,uint64_t us){
    *total+=us;if(us>*max)*max=us;
}
#endif