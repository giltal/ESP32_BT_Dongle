idf_component_register(SRCS "dongle_main.c" "h4_bridge.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES bt nvs_flash esp_driver_uart esp_app_format)
