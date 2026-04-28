idf_component_register(SRCS "main.c"
                    INCLUDE_DIRS "."
                    REQUIRES esp_wifi driver esp_event esp_netif nvs_flash esp_system)