idf_component_register(SRCS "pet_tls.c" INCLUDE_DIRS "include"
    REQUIRES mbedtls esp_timer esp_hw_support freertos lwip tailtiny)