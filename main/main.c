#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include <assert.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_now.h"

// Role selection — uncomment exactly ONE of the following:
#define ESP_NOW_ROLE_MASTER
// #define ESP_NOW_ROLE_MASTER_2
// #define ESP_NOW_ROLE_MASTER_3
// #define ESP_NOW_ROLE_SLAVE

#define ESPNOW_CHANNEL        1
#define ESPNOW_TX_POWER_DBM   78
#define SEND_INTERVAL_MS      50
#define CSI_MAX_DATA_LEN      256
#define CSI_LOG_BUFFER_LEN    2048
#define SENSOR_PAYLOAD_HEADER_LEN (sizeof(uint32_t) + sizeof(uint32_t))

// ── Shared types ──────────────────────────────────────────────────────────────

// tx_id + reserved[3] replace the first 4 bytes of the original padding[24].
// Total size must remain 32 bytes (verified by static_assert below).
typedef struct {
    uint32_t seq;
    uint32_t timestamp_ms;
    uint8_t  tx_id;       // 1 = TX1 (MASTER), 2 = TX2 (MASTER_2), 3 = TX3 (MASTER_3)
    uint8_t  reserved[3]; // formerly part of padding
    uint8_t  padding[20];
} __attribute__((packed)) sensor_payload_t;

static_assert(sizeof(sensor_payload_t) == 32, "payload size mismatch");

typedef struct {
    int32_t tx_id;        // identifies which transmitter sent this frame
    int32_t rssi;
    int32_t rate;
    int32_t channel;
    int32_t bandwidth;
    int32_t data_length;
    int64_t esp_timestamp;
    int8_t  csi_data[CSI_MAX_DATA_LEN];
} csi_event_t;

static const char *TAG = "ESP_NOW";

static esp_err_t configure_espnow_peer_rate(const uint8_t *peer_mac)
{
    esp_now_rate_config_t rate_config = {
        .phymode = WIFI_PHY_MODE_HT20,
        .rate    = WIFI_PHY_RATE_MCS0_LGI,
        .ersu    = false,
        .dcm     = false,
    };
    return esp_now_set_peer_rate_config(peer_mac, &rate_config);
}

// ── TX1 (Master) ──────────────────────────────────────────────────────────────
#ifdef ESP_NOW_ROLE_MASTER

static const uint8_t receiver_mac[ESP_NOW_ETH_ALEN] = {0x08, 0xd1, 0xf9, 0xf6, 0x7c, 0xec};

static sensor_payload_t current_payload = {0};

static void espnow_send_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status)
{
    ESP_LOGI(TAG, "ESP-NOW send status = %s",
             status == ESP_NOW_SEND_SUCCESS ? "SUCCESS" : "FAIL");
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
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
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
    peer.ifidx   = ESP_IF_WIFI_STA;
    peer.encrypt = false;

    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(configure_espnow_peer_rate(receiver_mac));
    return ESP_OK;
}

static void espnow_tx_task(void *pvParameter)
{
    while (1) {
        current_payload.seq++;
        current_payload.timestamp_ms = esp_log_timestamp();

        esp_err_t err = esp_now_send(receiver_mac, (uint8_t *)&current_payload, sizeof(current_payload));
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_now_send failed: %s", esp_err_to_name(err));
        } else {
            ESP_LOGI(TAG, "Sent seq=%u ts=%u",
                     current_payload.seq,
                     current_payload.timestamp_ms);
        }

        vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "ESP-NOW Master TX1 starting...");
    ESP_LOGI(TAG, "TX_ID=1 MAC will appear in RX logs");

    current_payload.tx_id = 1;

    ESP_ERROR_CHECK(init_wifi());
    ESP_ERROR_CHECK(init_espnow());

    xTaskCreate(espnow_tx_task, "espnow_tx_task", 4096, NULL, 5, NULL);
}

// ── TX2 (Master_2) ────────────────────────────────────────────────────────────
#elif defined(ESP_NOW_ROLE_MASTER_2)

// REPLACE WITH ACTUAL RX MAC (same receiver device as TX1 uses)
static const uint8_t receiver_mac[ESP_NOW_ETH_ALEN] = {0x08, 0xd1, 0xf9, 0xf6, 0x7c, 0xec};

static sensor_payload_t current_payload = {0};

static void espnow_send_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status)
{
    ESP_LOGI(TAG, "ESP-NOW send status = %s",
             status == ESP_NOW_SEND_SUCCESS ? "SUCCESS" : "FAIL");
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
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
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
    peer.ifidx   = ESP_IF_WIFI_STA;
    peer.encrypt = false;

    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(configure_espnow_peer_rate(receiver_mac));
    return ESP_OK;
}

static void espnow_tx_task(void *pvParameter)
{
    while (1) {
        current_payload.seq++;
        current_payload.timestamp_ms = esp_log_timestamp();

        esp_err_t err = esp_now_send(receiver_mac, (uint8_t *)&current_payload, sizeof(current_payload));
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_now_send failed: %s", esp_err_to_name(err));
        } else {
            ESP_LOGI(TAG, "Sent seq=%u ts=%u",
                     current_payload.seq,
                     current_payload.timestamp_ms);
        }

        vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "ESP-NOW Master TX2 starting...");
    ESP_LOGI(TAG, "TX_ID=2 MAC will appear in RX logs");

    current_payload.tx_id = 2;

    ESP_ERROR_CHECK(init_wifi());
    ESP_ERROR_CHECK(init_espnow());

    xTaskCreate(espnow_tx_task, "espnow_tx_task", 4096, NULL, 5, NULL);
}

// ── TX3 (Master_3) ────────────────────────────────────────────────────────────
#elif defined(ESP_NOW_ROLE_MASTER_3)

// REPLACE WITH ACTUAL RX MAC (same receiver device as TX1 and TX2 use)
static const uint8_t receiver_mac[ESP_NOW_ETH_ALEN] = {0x08, 0xd1, 0xf9, 0xf6, 0x7c, 0xec};

static sensor_payload_t current_payload = {0};

static void espnow_send_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status)
{
    ESP_LOGI(TAG, "ESP-NOW send status = %s",
             status == ESP_NOW_SEND_SUCCESS ? "SUCCESS" : "FAIL");
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
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
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
    peer.ifidx   = ESP_IF_WIFI_STA;
    peer.encrypt = false;

    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(configure_espnow_peer_rate(receiver_mac));
    return ESP_OK;
}

static void espnow_tx_task(void *pvParameter)
{
    while (1) {
        current_payload.seq++;
        current_payload.timestamp_ms = esp_log_timestamp();

        esp_err_t err = esp_now_send(receiver_mac, (uint8_t *)&current_payload, sizeof(current_payload));
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_now_send failed: %s", esp_err_to_name(err));
        } else {
            ESP_LOGI(TAG, "Sent seq=%u ts=%u",
                     current_payload.seq,
                     current_payload.timestamp_ms);
        }

        vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "ESP-NOW Master TX3 starting...");
    ESP_LOGI(TAG, "TX_ID=3 MAC will appear in RX logs");

    current_payload.tx_id = 3;

    ESP_ERROR_CHECK(init_wifi());
    ESP_ERROR_CHECK(init_espnow());

    xTaskCreate(espnow_tx_task, "espnow_tx_task", 4096, NULL, 5, NULL);
}

// ── RX (Slave) ────────────────────────────────────────────────────────────────
#elif defined(ESP_NOW_ROLE_SLAVE)

// REPLACE WITH ACTUAL MAC of the ESP32 flashed as ESP_NOW_ROLE_MASTER (TX1)
static const uint8_t tx1_mac[ESP_NOW_ETH_ALEN] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x01};
// REPLACE WITH ACTUAL MAC of the ESP32 flashed as ESP_NOW_ROLE_MASTER_2 (TX2)
static const uint8_t tx2_mac[ESP_NOW_ETH_ALEN] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x02};
// REPLACE WITH ACTUAL MAC of the ESP32 flashed as ESP_NOW_ROLE_MASTER_3 (TX3)
static const uint8_t tx3_mac[ESP_NOW_ETH_ALEN] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x03};

static QueueHandle_t     csi_queue            = NULL;
static uint8_t           local_mac[ESP_NOW_ETH_ALEN] = {0};
static volatile uint32_t csi_callback_count   = 0;
static volatile uint32_t csi_match_count      = 0;
static volatile uint32_t csi_zero_len_count   = 0;
static volatile uint32_t csi_queue_drop_count = 0;
static volatile uint32_t csi_tx1_count        = 0;
static volatile uint32_t csi_tx2_count        = 0;
static volatile uint32_t csi_tx3_count        = 0;

static void espnow_recv_cb(const esp_now_recv_info_t *esp_now_info, const uint8_t *data, int len)
{
    if (data == NULL || len < SENSOR_PAYLOAD_HEADER_LEN) {
        ESP_LOGW(TAG, "Gelen veri gecersiz veya cok kisa: %d", len);
        return;
    }

    if (len != sizeof(sensor_payload_t)) {
        ESP_LOGW(TAG, "Gelen veri beklenmeyen boyutta: %d (beklenen: %u)", len, (unsigned)sizeof(sensor_payload_t));
    }

    const sensor_payload_t *payload = (const sensor_payload_t *)data;
    int rssi = esp_now_info && esp_now_info->rx_ctrl ? esp_now_info->rx_ctrl->rssi : 0;

    ESP_LOGI(TAG, "Gelen paket: seq=%" PRIu32 " tx_id=%u rssi=%d",
             payload->seq,
             payload->tx_id,
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
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(ESPNOW_TX_POWER_DBM));
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, local_mac));

    return ESP_OK;
}

static esp_err_t init_espnow(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_recv_cb(espnow_recv_cb));

    esp_now_peer_info_t peer = {0};

    // Add TX1 as peer
    memcpy(peer.peer_addr, tx1_mac, ESP_NOW_ETH_ALEN);
    peer.channel = ESPNOW_CHANNEL;
    peer.ifidx   = ESP_IF_WIFI_STA;
    peer.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(configure_espnow_peer_rate(tx1_mac));

    // Add TX2 as peer
    memset(&peer, 0, sizeof(peer));
    memcpy(peer.peer_addr, tx2_mac, ESP_NOW_ETH_ALEN);
    peer.channel = ESPNOW_CHANNEL;
    peer.ifidx   = ESP_IF_WIFI_STA;
    peer.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(configure_espnow_peer_rate(tx2_mac));

    // Add TX3 as peer
    memset(&peer, 0, sizeof(peer));
    memcpy(peer.peer_addr, tx3_mac, ESP_NOW_ETH_ALEN);
    peer.channel = ESPNOW_CHANNEL;
    peer.ifidx   = ESP_IF_WIFI_STA;
    peer.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(configure_espnow_peer_rate(tx3_mac));

    return ESP_OK;
}

static void wifi_csi_cb(void *ctx, wifi_csi_info_t *info)
{
    if (info == NULL) {
        return;
    }

    csi_callback_count++;

    bool receiver_match = memcmp(info->dmac, local_mac, ESP_NOW_ETH_ALEN) == 0;

    int tx_id = 0;
    if      (memcmp(info->mac, tx1_mac, ESP_NOW_ETH_ALEN) == 0) tx_id = 1;
    else if (memcmp(info->mac, tx2_mac, ESP_NOW_ETH_ALEN) == 0) tx_id = 2;
    else if (memcmp(info->mac, tx3_mac, ESP_NOW_ETH_ALEN) == 0) tx_id = 3;
    if (tx_id == 0 || !receiver_match) return;

    csi_match_count++;
    if      (tx_id == 1) csi_tx1_count++;
    else if (tx_id == 2) csi_tx2_count++;
    else                 csi_tx3_count++;

    if (info->buf == NULL || info->len <= 0) {
        csi_zero_len_count++;
        return;
    }

    csi_event_t event = {
        .tx_id         = (int32_t)tx_id,
        .rssi          = info->rx_ctrl.rssi,
        .rate          = info->rx_ctrl.rate,
        .channel       = info->rx_ctrl.channel,
        .bandwidth     = info->rx_ctrl.cwb ? 40 : 20,
        .data_length   = info->len,
        .esp_timestamp = (int64_t)info->rx_ctrl.timestamp,
    };

    int copy_len = event.data_length;
    if (copy_len > CSI_MAX_DATA_LEN) {
        copy_len = CSI_MAX_DATA_LEN;
        event.data_length = CSI_MAX_DATA_LEN;
    }
    memcpy(event.csi_data, info->buf, copy_len);

    BaseType_t xHigherPriorityTaskWoken = pdFALSE;
    if (xQueueSendFromISR(csi_queue, &event, &xHigherPriorityTaskWoken) != pdTRUE) {
        csi_queue_drop_count++;
        return;
    }
    portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
}

static void csi_processing_task(void *pvParameter)
{
    csi_event_t event;
    while (xQueueReceive(csi_queue, &event, portMAX_DELAY) == pdTRUE) {
        char csi_log[CSI_LOG_BUFFER_LEN];
        int written = snprintf(csi_log,
                               sizeof(csi_log),
                               "CSI_START{\"tx_id\":%" PRId32 ",\"rssi\":%" PRId32 ",\"rate\":%" PRId32 ",\"channel\":%" PRId32 ",\"bandwidth\":%" PRId32 ",\"data_length\":%" PRId32 ",\"esp_timestamp\":%" PRId64 ",\"csi_data\":[",
                               event.tx_id,
                               event.rssi,
                               event.rate,
                               event.channel,
                               event.bandwidth,
                               event.data_length,
                               event.esp_timestamp);
        for (int i = 0; i < event.data_length; i++) {
            if (written < 0 || written >= (int)sizeof(csi_log)) {
                break;
            }
            written += snprintf(csi_log + written,
                                sizeof(csi_log) - written,
                                (i + 1 < event.data_length) ? "%d," : "%d",
                                event.csi_data[i]);
        }

        if (written >= 0 && written < (int)sizeof(csi_log)) {
            snprintf(csi_log + written, sizeof(csi_log) - written, "]}CSI_END\n");
            printf("%s", csi_log);
        }
    }
}

static void csi_diagnostic_task(void *pvParameter)
{
    while (1) {
        ESP_LOGI(TAG,
                 "CSI diag: callbacks=%" PRIu32 " matches=%" PRIu32 " zero_len=%" PRIu32 " queue_drop=%" PRIu32 " tx1=%" PRIu32 " tx2=%" PRIu32 " tx3=%" PRIu32,
                 csi_callback_count,
                 csi_match_count,
                 csi_zero_len_count,
                 csi_queue_drop_count,
                 csi_tx1_count,
                 csi_tx2_count,
                 csi_tx3_count);
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}

static esp_err_t init_csi(void)
{
    wifi_csi_config_t csi_cfg = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = false,
        .manu_scale        = false,
        .shift             = 0,
        .dump_ack_en       = false,
    };

    ESP_ERROR_CHECK(esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    return ESP_OK;
}

void app_main(void)
{
    ESP_LOGI(TAG, "ESP-NOW Slave (RX) starting...");

    csi_queue = xQueueCreate(64, sizeof(csi_event_t));
    if (csi_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create CSI queue");
        return;
    }

    ESP_ERROR_CHECK(init_wifi());
    ESP_ERROR_CHECK(init_espnow());
    ESP_ERROR_CHECK(init_csi());

    xTaskCreate(csi_processing_task, "csi_processing_task", 8192, NULL, 8, NULL);
    xTaskCreate(csi_diagnostic_task, "csi_diagnostic_task", 4096, NULL, 1, NULL);

    ESP_LOGI(TAG, "Ready and waiting for ESP-NOW packets...");
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

#else
#error "Please define ESP_NOW_ROLE_MASTER, ESP_NOW_ROLE_MASTER_2, ESP_NOW_ROLE_MASTER_3, or ESP_NOW_ROLE_SLAVE at the top of main.c"
#endif
