#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "driver/adc.h"

// Sonradan kolayca değiştirilebilir: sadece aşağıdaki satırı değiştirin.
#define ESP_NOW_ROLE_MASTER
// #define ESP_NOW_ROLE_SLAVE

#define ESPNOW_CHANNEL 1
#define ESPNOW_TX_POWER_DBM 78
#define MOVING_AVG_SIZE 20
#define SEND_INTERVAL_MS 100
#define ADC1_CHANNEL ADC1_CHANNEL_6 // GPIO34

#ifdef ESP_NOW_ROLE_MASTER
static const uint8_t receiver_mac[ESP_NOW_ETH_ALEN] = {0x08, 0xd1, 0xf9, 0xf6, 0x7c, 0xec};
#endif

#ifdef ESP_NOW_ROLE_SLAVE
static const uint8_t sender_mac[ESP_NOW_ETH_ALEN]   = {0x68, 0xfe, 0x71, 0x0b, 0xa4, 0x00};
#endif

static const char *TAG = "ESP_NOW";

typedef struct {
    uint32_t seq;
    int32_t raw_adc;
    int32_t filtered_adc;
    uint32_t timestamp_ms;
} __attribute__((packed)) sensor_payload_t;

#ifdef ESP_NOW_ROLE_MASTER
static sensor_payload_t current_payload = {0};
static int32_t moving_avg_buffer[MOVING_AVG_SIZE] = {0};
static size_t moving_avg_index = 0;
static size_t moving_avg_count = 0;

static void espnow_send_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status)
{
    ESP_LOGI(TAG, "ESP-NOW send status = %s",
             status == ESP_NOW_SEND_SUCCESS ? "SUCCESS" : "FAIL");
}

static void init_adc(void)
{
    adc1_config_width(ADC_WIDTH_BIT_12);
    adc1_config_channel_atten(ADC1_CHANNEL, ADC_ATTEN_DB_11);
}

static int32_t filter_adc_value(int32_t adc_value)
{
    moving_avg_buffer[moving_avg_index] = adc_value;
    moving_avg_index = (moving_avg_index + 1) % MOVING_AVG_SIZE;
    if (moving_avg_count < MOVING_AVG_SIZE) {
        moving_avg_count++;
    }

    int64_t sum = 0;
    for (size_t i = 0; i < moving_avg_count; i++) {
        sum += moving_avg_buffer[i];
    }
    return (int32_t)(sum / moving_avg_count);
}

static esp_err_t init_wifi(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(ESPNOW_TX_POWER_DBM));

    return ESP_OK;
}


static esp_err_t init_espnow(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_send_cb(espnow_send_cb));

    esp_now_peer_info_t peer = {0};
    memcpy(peer.peer_addr, receiver_mac, ESP_NOW_ETH_ALEN);
    peer.channel = ESPNOW_CHANNEL;
    peer.ifidx = ESP_IF_WIFI_STA;
    peer.encrypt = false;

    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    return ESP_OK;
}

static void espnow_tx_task(void *pvParameter)
{
    init_adc();

    while (1) {
        int32_t raw_value = adc1_get_raw(ADC1_CHANNEL);
        int32_t filtered_value = filter_adc_value(raw_value);

        current_payload.seq++;
        current_payload.raw_adc = raw_value;
        current_payload.filtered_adc = filtered_value;
        current_payload.timestamp_ms = esp_log_timestamp();

        esp_err_t err = esp_now_send(receiver_mac, (uint8_t *)&current_payload, sizeof(current_payload));
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_now_send failed: %s", esp_err_to_name(err));
        } else {
            ESP_LOGI(TAG, "Sent seq=%u raw=%d filt=%d ts=%u",
                     current_payload.seq,
                     current_payload.raw_adc,
                     current_payload.filtered_adc,
                     current_payload.timestamp_ms);
        }

        vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "ESP-NOW Master (TX) starting...");
    ESP_ERROR_CHECK(init_wifi());
    ESP_ERROR_CHECK(init_espnow());

    xTaskCreate(espnow_tx_task, "espnow_tx_task", 4096, NULL, 5, NULL);
}

#elif defined(ESP_NOW_ROLE_SLAVE)

static void espnow_recv_cb(const esp_now_recv_info_t *esp_now_info, const uint8_t *data, int len)
{
    if (data == NULL || len != sizeof(sensor_payload_t)) {
        ESP_LOGW(TAG, "Gelen veri beklenmeyen boyutta: %d", len);
        return;
    }

    const sensor_payload_t *payload = (const sensor_payload_t *)data;
    int rssi = esp_now_info && esp_now_info->rx_ctrl ? esp_now_info->rx_ctrl->rssi : 0;

    printf("RX,SEQ=%" PRIu32 ",RAW=%" PRId32 ",FILT=%" PRId32 ",RSSI=%d,TS=%" PRIu32 "\n",
           payload->seq,
           payload->raw_adc,
           payload->filtered_adc,
           rssi,
           payload->timestamp_ms);

    ESP_LOGI(TAG, "Gelen paket: seq=%" PRIu32 " raw=%" PRId32 " filt=%" PRId32 " rssi=%d",
             payload->seq,
             payload->raw_adc,
             payload->filtered_adc,
             rssi);
}

static esp_err_t init_wifi(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(ESPNOW_TX_POWER_DBM));

    return ESP_OK;
}

static esp_err_t init_espnow(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_recv_cb(espnow_recv_cb));

    esp_now_peer_info_t peer = {0};
    memcpy(peer.peer_addr, sender_mac, ESP_NOW_ETH_ALEN);
    peer.channel = ESPNOW_CHANNEL;
    peer.ifidx = ESP_IF_WIFI_STA;
    peer.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    return ESP_OK;
}

void app_main(void)
{
    ESP_LOGI(TAG, "ESP-NOW Slave (RX) starting...");
    ESP_ERROR_CHECK(init_wifi());
    ESP_ERROR_CHECK(init_espnow());

    ESP_LOGI(TAG, "Ready and waiting for ESP-NOW packets...");
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

#else
#error "Please define either ESP_NOW_ROLE_MASTER or ESP_NOW_ROLE_SLAVE at the top of main.c"
#endif
