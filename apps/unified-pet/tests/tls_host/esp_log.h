#pragma once
/* Host log seam; actual TCP source still uses the IDF direct INFO interface. */
#define ESP_LOG_INFO 3
void esp_log_write(int level,const char *tag,const char *format,...);