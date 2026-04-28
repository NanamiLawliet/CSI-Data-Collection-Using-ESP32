# ESP-NOW Haberleşme Kurulumu

## 🔧 Plan

1. `main/espnow_master.c` dosyasını TX modu için hazırlayacağız.
   - COM9 üzerindeki ESP32 cihazı bu kodu kullanacak.
   - Hedef MAC adresi: `68:fe:71:0b:a4:00`.
   - Wi-Fi kanalı 1'e sabitlenecek.
   - `esp_wifi_set_max_tx_power(78)` ile maksimum RF gücü ayarlanacak.
   - ADC1 pininden veri okunacak (örnek: GPIO34).
   - 20 elemanlı temiz bir `Moving Average` filtresi uygulanacak.
   - Her 100 ms'de bir veri gönderilecek.

2. `main/espnow_slave.c` dosyasını RX modu için hazırlayacağız.
   - COM10 üzerindeki ESP32 cihazı bu kodu kullanacak.
   - ESP-NOW ile kanal 1'de dinleme yapılacak.
   - Gelen veri callback içinde yakalanacak.
   - RSSI değeri `recv_info->rx_ctrl.rssi` içinden alınacak.
   - Seri port çıktısı `RX,SEQ=...,RAW=...,FILT=...,RSSI=...,TS=...` şeklinde olacak.
   - `esp_wifi_set_max_tx_power(78)` hassasiyet için uygulanacak.

3. `main/CMakeLists.txt` dosyasını kontrol edeceğiz.
   - `REQUIRES esp_wifi esp_now driver` bağımlılıkları eklenmeli.

4. Flash talimatlarını yazacağız.
   - VS Code alt barından port seçimi.
   - Önce COM9 (TX), sonra COM10 (RX) yüklemesi.

## ✅ Yapılanlar

- [x] `main/espnow_master.c` eklendi.
- [x] `main/espnow_slave.c` eklendi.
- [x] `main/CMakeLists.txt` bağımlılık kontrolü güncellendi.
- [x] Flash ve çalışma talimatları yazıldı.

## 📌 Kullanım

### 1) TX kodunu yükleme (COM9)

1. VS Code altındaki ESP-IDF/Serial port menüsünden `COM9` seçin.
2. `main/espnow_master.c` kodunu derleyip yükleyin.
   - VS Code ESP-IDF uzantısı kullanıyorsanız: `idf.py -p COM9 flash monitor`
3. Cihaz başladıktan sonra 100 ms aralıklarla veri gönderecektir.

### 2) RX kodunu yükleme (COM10)

1. Portu `COM10` olarak değiştirin.
2. `main/espnow_slave.c` kodunu derleyip yükleyin.
   - `idf.py -p COM10 flash monitor`
3. Seri monitörde `RX,SEQ=...` şeklinde gelen paketleri göreceksiniz.

## 🧠 Notlar

- ADC1 kullandık; ADC2 Wi-Fi ile stabil çalışmaz.
- Eğer 20 elemanlı `Moving Average` çok yavaş gelirse, `MOVING_AVG_SIZE` değerini `10` yapabilirsiniz.
- TX cihazı veri gönderirken hedef MAC adresini `receiver_mac` içinde ayarladık.
- RX cihazı `sender_mac` peer ekleyerek yalnızca TX cihazından gelen paketleri kabul edecek.

## 🚀 Örnek çalışma çıktısı

- TX cihazı:
  - `Sent seq=1 raw=2048 filt=2034 ts=1234`
- RX cihazı:
  - `RX,SEQ=1,RAW=2048,FILT=2034,RSSI=-45,TS=1234`

## 📁 Dosya yapısı

- `main/espnow_master.c`
- `main/espnow_slave.c`
- `main/CMakeLists.txt`
- `ESP_NOW_SETUP.md`
