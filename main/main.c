#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include <assert.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "lwip/err.h"
#include "lwip/sockets.h"
#include "lwip/sys.h"
#include <lwip/netdb.h>
#include "driver/uart.h"
#include "esp_vfs_dev.h"
#include "esp_timer.h"

#define ESPNOW_CHANNEL        1
#define ESPNOW_TX_POWER_DBM   78
#define SEND_INTERVAL_MS      50
#define CSI_MAX_DATA_LEN      256
#define CSI_LOG_BUFFER_LEN    2048
#define SENSOR_PAYLOAD_HEADER_LEN (sizeof(uint32_t) + sizeof(uint32_t))
#define UDP_PORT              3333

// ── Shared types ──────────────────────────────────────────────────────────────

typedef struct {
    uint32_t seq;
    uint32_t timestamp_ms;
    uint8_t  tx_id;       // Dynamic ID: 1, 2, 3 etc.
    uint8_t  reserved[3]; 
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

static const char *TAG = "GENERIC_FIRMWARE";

// ── Configuration State (Stored in NVS) ───────────────────────────────────────
static uint8_t s_current_role = 0; // 0=IDLE, 1=TRANSMITTER, 2=RECEIVER
static uint8_t s_peer_mac[6] = {0};
static uint8_t s_tx_id = 1;

static bool s_wifi_connected = false;
static char s_ip_addr[16] = "0.0.0.0";
static int64_t s_mute_csi_until = 0; // Temp silent window for UART queries

// ── Dynamic Transmitter Mapping (For Receiver CSI Logs) ──────────────────────
#define MAX_KNOWN_TRANSMITTERS 10
typedef struct {
    uint8_t mac[6];
    uint8_t tx_id;
    uint32_t last_seen_ms;
} transmitter_map_t;

static transmitter_map_t s_known_transmitters[MAX_KNOWN_TRANSMITTERS] = {0};
static int s_known_transmitters_count = 0;

static void update_transmitter_map(const uint8_t *mac, uint8_t tx_id)
{
    // Search if already exists
    for (int i = 0; i < s_known_transmitters_count; i++) {
        if (memcmp(s_known_transmitters[i].mac, mac, 6) == 0) {
            s_known_transmitters[i].tx_id = tx_id;
            s_known_transmitters[i].last_seen_ms = esp_log_timestamp();
            return;
        }
    }
    // If not, add if space permits
    if (s_known_transmitters_count < MAX_KNOWN_TRANSMITTERS) {
        memcpy(s_known_transmitters[s_known_transmitters_count].mac, mac, 6);
        s_known_transmitters[s_known_transmitters_count].tx_id = tx_id;
        s_known_transmitters[s_known_transmitters_count].last_seen_ms = esp_log_timestamp();
        s_known_transmitters_count++;
        ESP_LOGI(TAG, "Dynamic Transmitter Registered: MAC %02x:%02x:%02x:%02x:%02x:%02x -> tx_id %d",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], tx_id);
    }
}

static int get_tx_id_from_mac(const uint8_t *mac)
{
    for (int i = 0; i < s_known_transmitters_count; i++) {
        if (memcmp(s_known_transmitters[i].mac, mac, 6) == 0) {
            return s_known_transmitters[i].tx_id;
        }
    }
    return 0; // Not found
}

// ── NVS Functions ─────────────────────────────────────────────────────────────
static void read_config_from_nvs(void)
{
    nvs_handle_t my_handle;
    esp_err_t err = nvs_open("storage", NVS_READWRITE, &my_handle);
    if (err == ESP_OK) {
        uint8_t role = 0;
        err = nvs_get_u8(my_handle, "role", &role);
        if (err == ESP_OK) {
            s_current_role = role;
        } else {
            s_current_role = 0; // Default: IDLE
        }

        size_t peer_mac_len = sizeof(s_peer_mac);
        err = nvs_get_blob(my_handle, "peer_mac", s_peer_mac, &peer_mac_len);
        if (err != ESP_OK) {
            memset(s_peer_mac, 0, 6);
        }

        uint8_t tx_id = 1;
        err = nvs_get_u8(my_handle, "tx_id", &tx_id);
        if (err == ESP_OK) {
            s_tx_id = tx_id;
        } else {
            s_tx_id = 1;
        }

        nvs_close(my_handle);
    } else {
        s_current_role = 0;
        memset(s_peer_mac, 0, 6);
        s_tx_id = 1;
    }
}

static void process_config_command(const char *cmd)
{
    // Command formats:
    // SET_ROLE:IDLE
    // SET_ROLE:RECEIVER
    // SET_ROLE:TRANSMITTER:<peer_mac_hex>:<tx_id>

    if (strncmp(cmd, "SET_ROLE:", 9) == 0) {
        const char *role_str = cmd + 9;
        uint8_t new_role = 0;
        uint8_t new_peer_mac[6] = {0};
        uint8_t new_tx_id = 1;

        if (strncmp(role_str, "IDLE", 4) == 0) {
            new_role = 0;
        } else if (strncmp(role_str, "RECEIVER", 8) == 0) {
            new_role = 2;
        } else if (strncmp(role_str, "TRANSMITTER", 11) == 0) {
            new_role = 1;
            const char *mac_ptr = strchr(role_str, ':');
            if (mac_ptr) {
                mac_ptr++;
                unsigned int m[6];
                int parsed = sscanf(mac_ptr, "%02x:%02x:%02x:%02x:%02x:%02x",
                                    &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]);
                if (parsed == 6) {
                    for (int i = 0; i < 6; i++) {
                        new_peer_mac[i] = (uint8_t)m[i];
                    }
                }
                
                // Parse optional tx_id (located after the 6th colon of the MAC address)
                int colon_count = 0;
                const char *temp = mac_ptr;
                while (*temp) {
                    if (*temp == ':') {
                        colon_count++;
                        if (colon_count == 6) {
                            new_tx_id = atoi(temp + 1);
                            break;
                        }
                    }
                    temp++;
                }
            }
        } else {
            ESP_LOGE(TAG, "Unknown role input: %s", role_str);
            return;
        }

        nvs_handle_t my_handle;
        esp_err_t err = nvs_open("storage", NVS_READWRITE, &my_handle);
        ESP_LOGI(TAG, "nvs_open('storage') status: %s", esp_err_to_name(err));
        if (err == ESP_OK) {
            esp_err_t e_role = nvs_set_u8(my_handle, "role", new_role);
            esp_err_t e_peer = nvs_set_blob(my_handle, "peer_mac", new_peer_mac, 6);
            esp_err_t e_txid = nvs_set_u8(my_handle, "tx_id", new_tx_id);
            esp_err_t e_commit = nvs_commit(my_handle);
            nvs_close(my_handle);
            
            ESP_LOGI(TAG, "NVS Write: role_res=%s, peer_res=%s, tx_id_res=%s, commit_res=%s",
                     esp_err_to_name(e_role), esp_err_to_name(e_peer), esp_err_to_name(e_txid), esp_err_to_name(e_commit));
            ESP_LOGI(TAG, "Configuration stored: Role=%d, Peer=%02X:%02X:%02X:%02X:%02X:%02X, TXID=%d",
                     new_role, new_peer_mac[0], new_peer_mac[1], new_peer_mac[2],
                     new_peer_mac[3], new_peer_mac[4], new_peer_mac[5], new_tx_id);
            ESP_LOGI(TAG, "Restarting ESP32 to apply role...");
            vTaskDelay(pdMS_TO_TICKS(500));
            esp_restart();
        } else {
            ESP_LOGE(TAG, "NVS open failed: %s", esp_err_to_name(err));
        }
    }
}

// ── WiFi Initialization (Standalone Mode) ─────────────────────────────────────
static esp_err_t init_wifi(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(ESPNOW_TX_POWER_DBM));

    // Force default channel setting (succeeds immediately in standalone STA mode)
    ESP_ERROR_CHECK(esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE));

    return ESP_OK;
}

// ── UART / Serial Command Reader ──────────────────────────────────────────────
static void serial_rx_task(void *pvParameters)
{
    char rx_buffer[128];
    int index = 0;
    ESP_LOGI(TAG, "Serial UART0 reader task started.");

    while (1) {
        uint8_t c;
        int len = uart_read_bytes(UART_NUM_0, &c, 1, pdMS_TO_TICKS(50));
        if (len > 0) {
            s_mute_csi_until = esp_timer_get_time() + 1500000;
            if (c == '\n' || c == '\r') {
                if (index > 0) {
                    rx_buffer[index] = '\0';
                    // Strip trailing whitespaces
                    while (index > 0 && (rx_buffer[index-1] == '\r' || rx_buffer[index-1] == '\n' || rx_buffer[index-1] == ' ')) {
                        rx_buffer[--index] = '\0';
                    }
                    if (index > 0) {
                        ESP_LOGI(TAG, "Received Serial command: %s", rx_buffer);
                        if (strncmp(rx_buffer, "GET_STATUS", 10) == 0) {
                            uint8_t mac[6];
                            esp_wifi_get_mac(WIFI_IF_STA, mac);
                            char role_str[16];
                            if (s_current_role == 1) strcpy(role_str, "TRANSMITTER");
                            else if (s_current_role == 2) strcpy(role_str, "RECEIVER");
                            else strcpy(role_str, "IDLE");

                            printf("STATUS:Silikonlabs:ESP32:MAC:%02X:%02X:%02X:%02X:%02X:%02X:IP:0.0.0.0:ROLE:%s:PEER:%02X:%02X:%02X:%02X:%02X:%02X:TXID:%d\n",
                                     mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
                                     role_str,
                                     s_peer_mac[0], s_peer_mac[1], s_peer_mac[2], s_peer_mac[3], s_peer_mac[4], s_peer_mac[5],
                                     s_tx_id);
                        } else {
                            process_config_command(rx_buffer);
                        }
                    }
                    index = 0;
                }
            } else {
                if (index < sizeof(rx_buffer) - 1) {
                    rx_buffer[index++] = c;
                } else {
                    index = 0; // Overflow
                }
            }
        }
    }
}

// ── Periodic Serial Status Advertiser ─────────────────────────────────────────
static void status_print_task(void *pvParameters)
{
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, mac);

    while (1) {
        char role_str[16];
        if (s_current_role == 1) strcpy(role_str, "TRANSMITTER");
        else if (s_current_role == 2) strcpy(role_str, "RECEIVER");
        else strcpy(role_str, "IDLE");

        // We only advertise via console regularly if we are IDLE or TRANSMITTER
        // In RECEIVER mode, we avoid polluting the raw CSI data stream on serial console
        if (s_current_role != 2) {
            printf("STATUS:Silikonlabs:ESP32:MAC:%02X:%02X:%02X:%02X:%02X:%02X:IP:0.0.0.0:ROLE:%s:PEER:%02X:%02X:%02X:%02X:%02X:%02X:TXID:%d\n",
                     mac[0], mac[1], mac[2], mac[3], mac[4], mac[5],
                     role_str,
                     s_peer_mac[0], s_peer_mac[1], s_peer_mac[2], s_peer_mac[3], s_peer_mac[4], s_peer_mac[5],
                     s_tx_id);
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

// ── ESP-NOW Rate Configurations ───────────────────────────────────────────────
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

// ── TRANSMITTER Mode ──────────────────────────────────────────────────────────
static sensor_payload_t s_current_payload = {0};

static void espnow_send_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status)
{
    ESP_LOGI("ESP_NOW_TX", "ESP-NOW status = %s",
             status == ESP_NOW_SEND_SUCCESS ? "SUCCESS" : "FAIL");
}

static void espnow_tx_task(void *pvParameter)
{
    while (1) {
        s_current_payload.seq++;
        s_current_payload.timestamp_ms = esp_log_timestamp();

        esp_err_t err = esp_now_send(s_peer_mac, (uint8_t *)&s_current_payload, sizeof(s_current_payload));
        if (err != ESP_OK) {
            ESP_LOGE("ESP_NOW_TX", "esp_now_send failed: %s", esp_err_to_name(err));
        } else {
            ESP_LOGI("ESP_NOW_TX", "Sent seq=%u ts=%u to %02X:%02X:%02X:%02X:%02X:%02X",
                     s_current_payload.seq,
                     s_current_payload.timestamp_ms,
                     s_peer_mac[0], s_peer_mac[1], s_peer_mac[2], s_peer_mac[3], s_peer_mac[4], s_peer_mac[5]);
        }

        vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
    }
}

// ── RECEIVER Mode ─────────────────────────────────────────────────────────────
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
        ESP_LOGW("ESP_NOW_RX", "Gelen veri gecersiz veya cok kisa: %d", len);
        return;
    }

    const sensor_payload_t *payload = (const sensor_payload_t *)data;

    // Dynamically register or update transmitter mapping based on incoming ESP-NOW message
    update_transmitter_map(esp_now_info->src_addr, payload->tx_id);

    int rssi = esp_now_info && esp_now_info->rx_ctrl ? esp_now_info->rx_ctrl->rssi : 0;
    ESP_LOGI("ESP_NOW_RX", "Gelen paket: seq=%" PRIu32 " tx_id=%u rssi=%d",
             payload->seq,
             payload->tx_id,
             rssi);
}

static void wifi_csi_cb(void *ctx, wifi_csi_info_t *info)
{
    if (info == NULL) {
        return;
    }

    csi_callback_count++;

    bool receiver_match = memcmp(info->dmac, local_mac, ESP_NOW_ETH_ALEN) == 0;
    if (!receiver_match) return;

    int tx_id = get_tx_id_from_mac(info->mac);
    if (tx_id == 0) return; // Discard CSI if we don't have tx_id mapped yet

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
        if (esp_timer_get_time() < s_mute_csi_until) {
            continue;
        }
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
        ESP_LOGI("DIAG",
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

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    return ESP_OK;
}

// ── ESP-NOW Initialize ────────────────────────────────────────────────────────
static esp_err_t init_espnow(void)
{
    ESP_ERROR_CHECK(esp_now_init());

    if (s_current_role == 1) { // TRANSMITTER
        ESP_ERROR_CHECK(esp_now_register_send_cb(espnow_send_cb));

        esp_now_peer_info_t peer = {0};
        memcpy(peer.peer_addr, s_peer_mac, ESP_NOW_ETH_ALEN);
        peer.channel = ESPNOW_CHANNEL;
        peer.ifidx   = ESP_IF_WIFI_STA;
        peer.encrypt = false;

        ESP_ERROR_CHECK(esp_now_add_peer(&peer));
        ESP_ERROR_CHECK(configure_espnow_peer_rate(s_peer_mac));
    } 
    else if (s_current_role == 2) { // RECEIVER
        ESP_ERROR_CHECK(esp_now_register_recv_cb(espnow_recv_cb));
    }

    return ESP_OK;
}

// ── Application Main ──────────────────────────────────────────────────────────
void app_main(void)
{
    // Configure UART0
    uart_config_t uart_config = {
        .baud_rate = 115200,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    
    // Install UART driver on UART_NUM_0 at the absolute start of boot
    uart_driver_install(UART_NUM_0, 256 * 2, 0, 0, NULL, 0);
    uart_param_config(UART_NUM_0, &uart_config);
    esp_vfs_dev_uart_use_driver(0);

    // Initialize NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // Read saved configuration
    read_config_from_nvs();

    ESP_LOGI(TAG, "Booting Generic ESP32 Firmware...");
    ESP_LOGI(TAG, "Role from NVS: %d (%s)", s_current_role, 
             s_current_role == 1 ? "TRANSMITTER" : (s_current_role == 2 ? "RECEIVER" : "IDLE"));

    if (s_current_role == 1) {
        ESP_LOGI(TAG, "Target Peer: %02X:%02X:%02X:%02X:%02X:%02X, TXID=%d",
                 s_peer_mac[0], s_peer_mac[1], s_peer_mac[2], s_peer_mac[3], s_peer_mac[4], s_peer_mac[5], s_tx_id);
        s_current_payload.tx_id = s_tx_id;
    }

    // Initialize Wi-Fi
    ESP_ERROR_CHECK(init_wifi());

    // Start background tasks
    xTaskCreate(serial_rx_task, "serial_rx_task", 4096, NULL, 3, NULL);
    xTaskCreate(status_print_task, "status_print_task", 4096, NULL, 3, NULL);

    // Start role-specific tasks
    if (s_current_role == 1) { // TRANSMITTER
        ESP_ERROR_CHECK(init_espnow());
        xTaskCreate(espnow_tx_task, "espnow_tx_task", 4096, NULL, 5, NULL);
    } 
    else if (s_current_role == 2) { // RECEIVER
        ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, local_mac));
        csi_queue = xQueueCreate(64, sizeof(csi_event_t));
        if (csi_queue == NULL) {
            ESP_LOGE(TAG, "Failed to create CSI queue");
            return;
        }
        ESP_ERROR_CHECK(init_espnow());
        ESP_ERROR_CHECK(init_csi());

        xTaskCreate(csi_processing_task, "csi_processing_task", 8192, NULL, 8, NULL);
        xTaskCreate(csi_diagnostic_task, "csi_diagnostic_task", 4096, NULL, 1, NULL);
    }

    // Keep task alive
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
