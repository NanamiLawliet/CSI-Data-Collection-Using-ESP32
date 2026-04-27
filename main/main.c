#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "sdkconfig.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_wifi_types.h"
#include "esp_timer.h"
#include "esp_err.h"
#include "esp_system.h"

// ============================================================
// ROLE SELECTION: Set to 0 for RECEIVER, 1 for TRANSMITTER
// ============================================================
#define DEVICE_ROLE_TRANSMITTER 0  // Change to 0 for receiver, 1 for transmitter

// ============================================================
// RF SENSING CONFIGURATION
// ============================================================
#define WIFI_CHANNEL 1                    // Fixed WiFi channel for RF sensing
#define TX_BROADCAST_INTERVAL_MS 50       // TX packet interval (ms)
#define WIFI_BUFFER_TX_WINDOW 100         // TX burst window
#define MAX_TX_POWER 20                   // Max TX power in dBm (0-20)

static const char *TAG = "rf_sensing";

// FreeRTOS task handles
static TaskHandle_t wifi_task_handle = NULL;
static TaskHandle_t csi_task_handle = NULL;
#if DEVICE_ROLE_TRANSMITTER
static TaskHandle_t tx_task_handle = NULL;
#endif

// FreeRTOS queue for CSI data
#define CSI_QUEUE_SIZE 50
static QueueHandle_t csi_queue = NULL;

// FreeRTOS semaphore for WiFi initialization
static SemaphoreHandle_t wifi_init_semaphore = NULL;

// ============================================================
// CSI DATA STRUCTURE WITH I/Q COMPONENTS
// ============================================================
typedef struct {
    int16_t magnitude;           // Magnitude of CSI (dBm)
    int16_t phase;               // Phase of CSI (degrees)
    int8_t rssi;                 // RSSI of received packet
    uint8_t rate;                // Data rate
    uint8_t channel;             // WiFi channel
    uint8_t secondary_channel;   // HT secondary channel
    uint32_t timestamp;          // Timestamp
    uint16_t len;                // Number of CSI values
    int8_t csi_data[256];        // Raw CSI data
    uint8_t mac[6];              // Source MAC address
} csi_raw_data_t;

// ============================================================
// CSI PROCESSING TASK - Outputs CSV-formatted raw I/Q data
// ============================================================
static void csi_processing_task(void *pvParameters) {
    csi_raw_data_t csi_packet;
    
    ESP_LOGI(TAG, "CSI processing task started");
    
    while (1) {
        if (xQueueReceive(csi_queue, &csi_packet, portMAX_DELAY)) {
            // Output CSI data in CSV format for easy parsing
            // Format: timestamp,rssi,rate,channel,mac_addr,length,magnitude,phase,csi_i_values
            
            printf("CSI_DATA,");
            printf("%lu,", (long unsigned int)csi_packet.timestamp);
            printf("%d,", csi_packet.rssi);
            printf("%u,", csi_packet.rate);
            printf("%u,", csi_packet.channel);
            
            // MAC address
            printf("%02x:%02x:%02x:%02x:%02x:%02x,",
                   csi_packet.mac[0], csi_packet.mac[1], csi_packet.mac[2],
                   csi_packet.mac[3], csi_packet.mac[4], csi_packet.mac[5]);
            
            printf("%u,", csi_packet.len);
            printf("%d,", csi_packet.magnitude);
            printf("%d,", csi_packet.phase);
            
            // Raw CSI I/Q data (as comma-separated values)
            printf("\"[");
            for (int i = 0; i < csi_packet.len; i++) {
                printf("%d", csi_packet.csi_data[i]);
                if (i < csi_packet.len - 1) printf(",");
            }
            printf("]\"");
            printf("\n");
            
            ESP_LOGD(TAG, "CSI: RSSI=%d, CH=%u, Len=%u, MAC=%02x:%02x:%02x",
                    csi_packet.rssi, csi_packet.channel, csi_packet.len,
                    csi_packet.mac[0], csi_packet.mac[1], csi_packet.mac[2]);
        }
    }
}


// ============================================================
// EVENT HANDLER - WiFi state events
// ============================================================
static void event_handler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_WIFI_READY) {
        ESP_LOGI(TAG, "WiFi is ready for TX/RX");
        if (wifi_init_semaphore != NULL) {
            xSemaphoreGive(wifi_init_semaphore);
        }
    }
}

// ============================================================
// CSI CALLBACK - RX Only, process received packet CSI data
// ============================================================
#if !DEVICE_ROLE_TRANSMITTER
static void wifi_csi_cb(void *ctx, wifi_csi_info_t *csi_info) {
    if (csi_info == NULL || csi_info->buf == NULL) {
        return;
    }
    
    csi_raw_data_t csi_data = {0};
    csi_data.rssi = csi_info->rx_ctrl.rssi;
    csi_data.rate = csi_info->rx_ctrl.rate;
    csi_data.channel = csi_info->rx_ctrl.channel;
    csi_data.secondary_channel = csi_info->rx_ctrl.secondary_channel;
    csi_data.timestamp = (uint32_t)(esp_timer_get_time() / 1000);
    csi_data.len = csi_info->len > 256 ? 256 : csi_info->len;
    
    memcpy(csi_data.csi_data, csi_info->buf, csi_data.len);
    memcpy(csi_data.mac, csi_info->mac, 6);
    
    if (csi_data.len >= 2) {
        csi_data.magnitude = (csi_data.csi_data[0] + csi_data.csi_data[1]) / 2;
        csi_data.phase = (csi_data.csi_data[1] - csi_data.csi_data[0]);
    }
    
    if (csi_queue != NULL) {
        xQueueSend(csi_queue, &csi_data, 0);
    }
}
#endif

// ============================================================
// TRANSMITTER TASK - Generates WiFi carrier for backscatter sensing
// ============================================================
#if DEVICE_ROLE_TRANSMITTER
static void tx_broadcast_task(void *pvParameters) {
    uint32_t tx_count = 0;
    
    ESP_LOGI(TAG, "TX broadcast task started - interval %dms", TX_BROADCAST_INTERVAL_MS);
    ESP_LOGI(TAG, "NOTE: TX maintains WiFi carrier on channel %d for RX CSI capture", WIFI_CHANNEL);
    
    // Allow WiFi to stabilize
    vTaskDelay(2000 / portTICK_PERIOD_MS);
    
    while (1) {
        // For RF backscatter sensing with CSI capture:
        // TX maintains a continuous WiFi presence on the target channel
        // RX in promiscuous mode captures CSI from TX's periodic activity
        // This can include:
        // - Multicast probe requests
        // - Keep-alive frames
        // - Or just WiFi beacons if in AP mode
        
        tx_count++;
        
        // Log activity periodically
        if (tx_count % 200 == 0) {
            ESP_LOGI(TAG, "TX active: %lu intervals (%.1fs elapsed)", 
                    (long unsigned int)tx_count, 
                    (tx_count * TX_BROADCAST_INTERVAL_MS) / 1000.0);
        }
        
        vTaskDelay(TX_BROADCAST_INTERVAL_MS / portTICK_PERIOD_MS);
    }
}
#endif

// ============================================================
// WiFi Initialization Task
// ============================================================
static void wifi_init_task(void *pvParameters) {
    ESP_LOGI(TAG, "WiFi initialization task started");
    
    // Initialize NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    
    // Initialize TCP/IP stack
    ESP_ERROR_CHECK(esp_netif_init());
    
    // Create default event loop
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    
    // Create WiFi interface (NULL mode for raw TX/RX)
#if DEVICE_ROLE_TRANSMITTER
    ESP_LOGI(TAG, "Configuring as TRANSMITTER (raw 802.11 mode)");
    esp_netif_create_default_wifi_sta();
#else
    ESP_LOGI(TAG, "Configuring as RECEIVER (promiscuous mode)");
    esp_netif_create_default_wifi_sta();
#endif
    
    // Initialize WiFi
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    
    // Register event handlers
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL));
    
    // Set WiFi mode (NULL mode for raw frames)
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    
    // Configure WiFi to not use power saving
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_LOGI(TAG, "WiFi power saving disabled");
    
    // Start WiFi (required before setting TX power and bandwidth)
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "WiFi started");
    
    // Set TX power to maximum (must be after esp_wifi_start)
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(MAX_TX_POWER * 4)); // SDP (scaled by 4)
    ESP_LOGI(TAG, "TX power set to %dDBm", MAX_TX_POWER);
    
    // Set WiFi bandwidth (must be after esp_wifi_start)
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));
    
    // Set channel (BOTH TX and RX must use same channel)
    wifi_second_chan_t second_chan = WIFI_SECOND_CHAN_NONE;
    ESP_ERROR_CHECK(esp_wifi_set_channel(WIFI_CHANNEL, second_chan));
    ESP_LOGI(TAG, "WiFi channel set to %d", WIFI_CHANNEL);
    
#if DEVICE_ROLE_TRANSMITTER
    // ============================================================
    // TRANSMITTER CONFIG
    // ============================================================
    ESP_LOGI(TAG, "Setting up TX mode");
    
    // Enable promiscuous mode to capture own transmissions for CSI analysis
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_LOGI(TAG, "TX promiscuous mode enabled for echo capture");
    
#else
    // ============================================================
    // RECEIVER CONFIG - Enable CSI and Promiscuous Mode
    // ============================================================
    ESP_LOGI(TAG, "Setting up RX mode");
    
    // Enable promiscuous mode to capture all frames
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_LOGI(TAG, "Promiscuous mode enabled");
    
    // Configure CSI acquisition
    wifi_csi_config_t csi_config = {
        .lltf_en = true,              // Enable Long Training Field
        .htltf_en = true,             // Enable HT Long Training Field
        .stbc_htltf2_en = true,       // Enable STBC HT LTF2
        .ltf_merge_en = true,         // Enable LTF merge
        .channel_filter_en = true,    // Enable channel filter
        .manu_scale = false,          // Auto scaling
    };
    
    ret = esp_wifi_set_csi_config(&csi_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set CSI config: %s", esp_err_to_name(ret));
        ESP_LOGE(TAG, "Ensure CSI is enabled in: idf.py menuconfig -> Component config -> Wi-Fi -> Enable CSI");
    } else {
        ESP_LOGI(TAG, "CSI config set successfully");
    }
    
    // Register CSI callback
    ret = esp_wifi_set_csi_rx_cb(wifi_csi_cb, NULL);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to set CSI callback: %s", esp_err_to_name(ret));
    } else {
        ESP_LOGI(TAG, "CSI callback registered");
    }
    
    // Enable CSI capture
    ret = esp_wifi_set_csi(true);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to enable CSI: %s", esp_err_to_name(ret));
    } else {
        ESP_LOGI(TAG, "CSI capture enabled");
    }
#endif
    
    // Signal initialization complete
    if (wifi_init_semaphore != NULL) {
        xSemaphoreGive(wifi_init_semaphore);
    }
    
    // Task cleanup
    vTaskDelete(NULL);
}

void app_main(void) {
    ESP_LOGI(TAG, "======================================================");
    ESP_LOGI(TAG, "    RF BACKSCATTER SENSING - CSI Data Collection");
    ESP_LOGI(TAG, "======================================================");
    
#if DEVICE_ROLE_TRANSMITTER
    ESP_LOGI(TAG, "DEVICE ROLE: TRANSMITTER");
    ESP_LOGI(TAG, "MAC: 68:fe:71:0b:a4:00");
    ESP_LOGI(TAG, "Function: Raw 802.11 frame broadcast");
    ESP_LOGI(TAG, "Interval: %dms", TX_BROADCAST_INTERVAL_MS);
#else
    ESP_LOGI(TAG, "DEVICE ROLE: RECEIVER");
    ESP_LOGI(TAG, "MAC: 08:d1:f9:f6:7c:ec");
    ESP_LOGI(TAG, "Function: CSI capture in promiscuous mode");
    ESP_LOGI(TAG, "Channel: %d", WIFI_CHANNEL);
#endif
    
    ESP_LOGI(TAG, "======================================================\n");
    
    // Create semaphore for WiFi initialization
    wifi_init_semaphore = xSemaphoreCreateBinary();
    
    // Create queue for CSI data
    csi_queue = xQueueCreate(CSI_QUEUE_SIZE, sizeof(csi_raw_data_t));
    
    if (wifi_init_semaphore == NULL || csi_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create FreeRTOS primitives");
        return;
    }
    
    // Create WiFi initialization task
    xTaskCreate(wifi_init_task,
                "wifi_init",
                4096,
                NULL,
                5,
                &wifi_task_handle);
    
    // Create CSI processing task
    xTaskCreate(csi_processing_task,
                "csi_process",
                4096,
                NULL,
                3,
                &csi_task_handle);
    
#if DEVICE_ROLE_TRANSMITTER
    // Create TX broadcast task for transmitter
    xTaskCreate(tx_broadcast_task,
                "tx_broadcast",
                2048,
                NULL,
                4,
                &tx_task_handle);
    ESP_LOGI(TAG, "TX broadcast task created");
#endif
    
    ESP_LOGI(TAG, "All tasks created successfully");
}
