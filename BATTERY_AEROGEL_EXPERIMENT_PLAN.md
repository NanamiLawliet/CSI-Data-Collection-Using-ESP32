# Li-ion Batarya & Aerojel Sıcaklık / Voltaj / Backscatter Deney Planı ve Rehberi

> **Amaç:** Bu doküman, hocanın paylaştığı **[Aerogel Backscatter Temperature Sensing for Li-ion Batteries A Practical Guide.docx](./Aerogel%20Backscatter%20Temperature%20Sensing%20for%20Li-ion%20Batteries%20A%20Practical%20Guide.docx)** belgesi ile **Li-ion Batarya Voltaj / Darbe / RF Backscatter** teknik notlarını ve mevcut **ESP32 CSI Veri Toplama Sistemimizi** (`main/main.c`, `web_app.py`) tek bir çatı altında birleştiren, Perşembe günü çalışmaya başlarken adım adım takip edilebilecek çalışma ve deney planıdır.

---

## 1. Hocanın İstediği Çalışmanın Özeti (Büyük Resim)

Hocanın temel hedefi: **Bir Li-ion bataryanın şarj/deşarj sırasındaki sıcaklığını (Aerojel sensör ile), voltajını ve iç direncini güvenli bir şekilde ölçmek ve bunu kablosuz/backscatter (geri saçılım) mantığıyla ESP32 üzerinden okumak.**

### ⚠️ En Kritik Fiziksel ve Güvenlik Kuralları
1. **Batarya Kutuplarına (`+` / `-`) Doğrudan RF Sinyali Enjekte Edilmez:**
   * Bataryanın doğrudan `+` ve `-` uçlarından içeriye GHz seviyesinde RF sinyali basıp yansıma okumak (*Core Reflectometry*) tek başına bir ESP32 ile mümkün değildir (pahalı VNA, yönsel kuplör ve geniş bant yükselteçler gerektirir) ve batarya için tehlikelidir.
   * Bunun yerine **Aerojel**, bataryanın dış yüzeyine yapıştırılan **ayrı, pasif bir sensör** olarak çalışır.
2. **Batarya `+` Ucu Asla Doğrudan ESP32 Pinine Bağlanmaz:**
   * Li-ion batarya tam doluyken **4.2 V** verir. ESP32 GPIO/ADC pinleri ise maksimum **3.3 V** destekler.
   * Voltaj ölçümü mutlaka **Voltaj Bölücü Direnç Devresi (100 kΩ + 100 kΩ)** üzerinden yapılmalıdır (`4.2 V -> ~2.1 V`).
3. **Wi-Fi / CSI Açıkken Sadece `ADC1` Pinleri Kullanılır:**
   * ESP32'de Wi-Fi (veya ESP-NOW / CSI) aktifken `ADC2` donanımı Wi-Fi sürücüsü tarafından kilitlenir.
   * Bu yüzden tüm analog ölçümler için mutlaka **`ADC1` pinleri (`GPIO32`, `GPIO33`, `GPIO34`, `GPIO35`, `GPIO36`, `GPIO39`)** kullanılmalıdır.
4. **Batarya Güvenliği (Thermal Runaway Önlemi):**
   * Batarya kesinlikle delinmez veya dış metal/poşet kılıfı açılmaz (metal kılıf RF'i blokladığı için sensör dış yüzeye konur).
   * Her zaman koruma devreli (BMS/PCM) **tek hücreli (1S) Li-ion pil** ile çalışılır ve yanında referans bir sıcaklık sensörü (NTC, DS18B20 veya termokupl) bulundurulur.

---

## 2. Donanım Devre Şemaları (Başlangıç Kurulumu)

### 🛠️ Lehimsiz (100% Breadboard) Kurulum İpuçları
Bu deneylerin **hiçbiri için lehim yapmana gerek yok**, tamamını breadboard üzerinde kurabilirsin:
1. **Batarya Bağlantısı:** Li-ion pile kesinlikle kablo lehimlemeye çalışma (ısı pile zarar verir ve çok tehlikelidir). Bunun yerine **kablolu 18650 pil yuvası (battery holder)** veya uçları kancalı **krokodil (timsah ağzı) jumper kablo** kullanıp doğrudan breadboard'a tak.
2. **Aerojel Bağlantısı (Kırılgan Malzeme):** Aerojele lehim tutmaz ve bacak batırmak malzemeyi çatlatabilir. İki parça **yapışkanlı bakır folyo bant (copper tape)** veya alüminyum folyo kes, aerojeli iki folyo arasına sandviç gibi koyup dışından **Kapton bant** (veya normal elektrik bandı) ile sıkıca sar. Bakır folyoların dışarı taşan uçlarına **krokodil kablo** kıstırarak breadboard'a bağla.
3. **Voltaj Bölücü Dirençler ve Kondansatör:** Standart bacaklı (through-hole) `100 kΩ` dirençler, `10 kΩ` direnç ve `100 nF` kondansatör doğrudan breadboard deliklerine takılır.
4. **ESP32 Bağlantısı:** ESP32 dev board'u doğrudan breadboard'a oturt veya Dişi-Erkek (Female-to-Male) Dupont jumper kablolarla `GND`, `3V3`, `GPIO34` ve `GPIO35` pinlerinden breadboard'a hat çek.

### 2.1. Devre 1: Li-ion Batarya Voltaj Ölçüm Devresi (`GPIO34`)
Şarj veya deşarj esnasında batarya voltajını güvenli okumak için:

```text
   [Li-ion Batarya (+)]  (3.0V - 4.2V)
            │
           ┌┴┐
           │ │ R1 = 100 kΩ
           └┬┘
            ├──────────────────────► ESP32 GPIO34 (ADC1_CH6)
           ┌┴┐                       (Max ~2.1V görür -> Güvenli)
           │ │ R2 = 100 kΩ
           └┬┘
            ├──────────────────────► 100 nF Seramik Kondansatör (Gürültü filtresi - Opsiyonel)
            │
   [Li-ion Batarya (−)] ───────────► ESP32 GND (Ortak Toprak / Common Ground)
```

* **Hesaplama:** $\text{V}_{\text{batarya}} = \text{V}_{\text{ADC}} \times \frac{R_1 + R_2}{R_2} \times \text{CAL} = \text{V}_{\text{ADC}} \times 2.0 \times \text{CAL}$
* **Kalibrasyon (`CAL`):** Multimetre ile pilin gerçek voltajı ölçülür, ESP32'nin okuduğu değere bölünerek `CAL` katsayısı (`~1.0`) ince ayarlanır.
* **Akım Ölçümü (İsteğe Bağlı):** Şarj/deşarj hattına seri `0.1 Ω` şönt direnç veya `INA219` / `INA226` (I²C) modülü eklenebilir.

---

### 2.2. Devre 2: Aerojel Sıcaklık Sensörü Okuma Devresi (`GPIO35` — Option A: Kablolu Rezistif)
Eğer elimizdeki aerojel **iletken/yarı-iletken tip** ise (Karbon, Grafen, CNT, MXene aerojel), sıcaklıkla direnci ($R_{\text{aero}}$) değişir ve tıpkı bir termistör gibi okunur:

```text
   [ESP32 3.3V]
        │
       ┌┴┐
       │ │ R_fixed (Aerojelin oda sıcaklığındaki direncine yakın, örn. 10 kΩ)
       └┬┘
        ├──────────────────────► ESP32 GPIO35 (ADC1_CH7)
       ┌┴┐
       │ │ R_aero (Batarya yüzeyine Kapton bantla sabitlenmiş Aerojel numunesi)
       └┬┘
        │
   [ESP32 GND]
```

* **Hesaplama:**
  1. $V = \text{analogReadMilliVolts}(35) / 1000.0$
  2. $R_{\text{aero}} = R_{\text{fixed}} \times \frac{V}{3.3 - V}$
  3. $T(^\circ\text{C}) = a + b \cdot R_{\text{aero}} + c \cdot R_{\text{aero}}^2$ *(Kalibrasyon eğrisinden gelen katsayılar)*

> **Not — Aerojel Yalıtkan İse (Silika / Polimer Aerojel):**
> Direnç yerine **kapasitans / dielektrik sabiti ($\varepsilon$)** değişir (**Option B / C / D**). İki bakır bant arasına aerojel konularak paralel plakalı kondansatör yapılır; `ESP32 touchRead()`, `FDC2214` (I²C) veya doğrudan bizim **2.4 GHz Wi-Fi CSI Backscatter** düzeneğimizle okunur.

---

### 2.3. Devre 3: Elektriksel Darbe (Pulse) ile Batarya İç Direnci Ölçümü (`R_internal`)
Bataryanın yaşlanmasını ve anlık sağlığını ölçmek için:
* Bir ESP32 GPIO pini (ör. `GPIO25`) ile bir **Logic-Level N-MOSFET** sürülür.
* MOSFET çok kısa bir süre (ör. `50 ms`) bataryaya bilinen bir yük direncini ($R_{\text{load}}$, örn. `10 Ω`) bağlar.
* Darbeden hemen önceki voltaj ($V_{\text{open}}$) ve darbe sırasındaki voltaj ($V_{\text{load}}$) `GPIO34`'ten okunur:
  $$\Delta V = V_{\text{open}} - V_{\text{load}}, \quad \Delta I = \frac{V_{\text{load}}}{R_{\text{load}}}, \quad R_{\text{internal}} \approx \frac{\Delta V}{\Delta I}$$

---

## 3. Aerojel Okuma Mimarilerinin Karşılaştırması (`.docx` Özeti)

Hocanın gönderdiği rehber, çalışmayı **basitten zora 4 aşamaya** ayırmaktadır:

| Seçenek | Fiziksel Özellik | Sinyal Tipi | Gerekli Donanım | Zorluk |
| :--- | :--- | :--- | :--- | :--- |
| **Option A: Kablolu Rezistif** | Direnç ($R$) | DC Voltaj Bölücü | ESP32 (`GPIO35`) + 1 Sabit Direnç | **Kolay (İlk Adım)** |
| **Option B: Kablolu Kapasitif** | Kapasitans ($C$) | 1–100 kHz AC | ESP32 + `FDC1004` / `FDC2214` veya `AD5933` | Orta |
| **Option C: Kablosuz LC Rezonans** | Kapasitans ($C \to f_0$) | 1–12 MHz İndüktif Tarama | ESP32 + `AD9833`/`AD5933` + Spiral Bobinler | Orta–Zor |
| **Option D: GHz RF Backscatter** | Dielektrik / Yansıma | 2.4 GHz Wi-Fi CSI / 1–6 GHz RF | **Mevcut 2x ESP32 CSI Sistemimiz** veya NanoVNA | İleri Seviye |

---

## 4. Mevcut ESP32 CSI Sistemimizle (`CSI-Data-Collection-Using-ESP32`) Entegrasyon

Şu an kurduğumuz sistem (`main/main.c` + `web_app.py`) hocanın istediği yapıyla **iki şekilde** doğrudan birleşmektedir:

### Senaryo 1: 2.4 GHz Wi-Fi CSI ile Kablosuz Aerojel / Backscatter Algılama
```text
┌──────────────────────┐                     ┌────────────────────────┐                     ┌──────────────────────┐
│  ESP32 TRANSMITTER   │  2.4 GHz ESP-NOW    │   Li-ion Batarya +     │  Yansıma / Saçılım  │    ESP32 RECEIVER    │
│       (COM9)         │ ──────────────────► │  Yüzeydeki Aerojel /   │ ──────────────────► │       (COM11)        │
│                      │                     │  Anten Empedans Anaht. │   (CSI Değişimi)    │   web_app.py (CSV)   │
└──────────────────────┘                     └────────────────────────┘                     └──────────────────────┘
```
1. **Pasif Aerojel Backscatter:**
   * TX ve RX ESP32 antenlerinin arasına/yakınına üzerine aerojel yerleştirilmiş Li-ion batarya (veya ısıtıcı tabla üzerindeki aerojel) konur.
   * Aerojel ısındıkça dielektrik/iletkenlik özelliği değişir; bu da alıcı ESP32'deki **64 alt taşıyıcının (Subcarrier) CSI genlik/faz profilini ve RSSI değerini** değiştirir.
2. **Aktif RF Backscatter (GPIO ile Anten Anahtarlama):**
   * Hocanın 2. notunda belirttiği gibi: Bir ESP32 bataryanın voltajını ve sıcaklığını ölçer, ardından bir GPIO piniyle bir RF transistörünü/anahtarını açıp kapatarak yansıma (backscatter) oluşturur; bizim `RECEIVER` ESP32 ise bu değişimi CSI üzerinden yakalar.

### Senaryo 2: ESP32 Firmware (`main/main.c`) İçine ADC Okumasının Eklenmesi
* `TRANSMITTER` (veya `RECEIVER`) ESP32 kartının `GPIO34` pinine **Batarya Voltaj Bölücüsü**, `GPIO35` pinine **Aerojel Direnç Bölücüsü** bağlanır.
* `main/main.c` içerisindeki görev döngüsüne ADC okuması eklenerek her pakette:
  * **CSI verisi (64 alt taşıyıcı + RSSI)**
  * **Batarya Voltajı (`v_batt`)**
  * **Aerojel Direnci (`r_aero`) ve Sıcaklığı (`t_aero`)**
  * **Referans Sıcaklık (`t_ref`)**
  aynı anda `web_app.py` üzerinden aynı `.csv` dosyasına kaydedilir. Böylece makine öğrenmesi / kalibrasyon için kusursuz eşzamanlı veri seti oluşur.

---

## 5. Perşembe Günü Uygulanacak Adım Adım Deney Planı

### Adım 1: Malzeme ve Aerojel Kontrolü
- [ ] Aerojelin türünü netleştir (Karbon/Grafen gibi **iletken** mi, yoksa Silika/Polimer gibi **yalıtkan** mı?).
- [ ] Multimetre ile oda sıcaklığında aerojelin iki ucu arasındaki direnci ölç:
  * Eğer ölçülebilir bir direnç varsa (örn. `100 Ω – 1 MΩ` arası) **Option A (Kablolu Rezistif)** ile başla.
  * Eğer açık devre (`OL` / yalıtkan) ise **Option B (Kapasitif)** veya **2.4 GHz CSI Backscatter** ile devam et.
- [ ] Aerojeli nemden korumak için **Kapton bant** ile sarmala (silika aerojeller nem çektiğinde sıcaklık ölçümü sapar).

### Adım 2: Masaüstü Güvenli Voltaj + Aerojel ADC Testi (ESP32)
- [ ] Breadboard üzerinde `100 kΩ + 100 kΩ` dirençlerle batarya voltaj bölücüsünü `GPIO34` pinine kur.
- [ ] Multimetre ile batarya voltajını ölçüp ESP32'nin okuduğu voltajla karşılaştır ve `CAL` değerini doğrula.
- [ ] Aerojel + sabit direnç devresini `GPIO35` pinine bağla ve yanına referans sıcaklık sensörünü (NTC / DS18B20 / Termokupl) yerleştir.

### Adım 3: Sıcaklık Kalibrasyonu (20 °C – 70 °C)
- [ ] Aerojeli ve referans termometreyi kontrollü olarak (ısıtıcı tabla veya şarj/deşarj olan pil üzerinde) 20 °C'den 60–70 °C'ye kadar yavaşça ısıt.
- [ ] Her saniye hem **referans sıcaklığı** hem **aerojel direncini/kapasitansını** hem de **`web_app.py` üzerinden CSI kaydını (`Record CSV`)** kaydet.
- [ ] Python (`numpy.polyfit`) ile sensör okuması $\leftrightarrow$ gerçek sıcaklık kalibrasyon eğrisini ($a, b, c$ katsayılarını) çıkar.

### Adım 4: Gerçek Şarj / Deşarj Döngüsü ve CSI Backscatter Kaydı
- [ ] Koruma devreli tek bir 18650 / kese (pouch) Li-ion hücreyi şarj/deşarj döngüsüne al.
- [ ] `py web_app.py` üzerinden **Record CSV** başlatarak şarj/deşarj boyunca CSI genliklerini, RSSI değerini, batarya voltajını ve aerojel sıcaklığını kaydet.
- [ ] Tekrarlanabilirliği (histerezis olup olmadığını) görmek için ısıtma-soğutma döngüsünü en az 2-3 kez tekrarla.

---

## 6. Referans Kod Parçacıkları (Hızlı Kullanım İçin)

### 6.1. Batarya Voltajı + Aerojel Direnci Okuma (Arduino / ESP32 Test Kodu)
```cpp
const int PIN_BATT_ADC = 34;    // ADC1_CH6 - Batarya voltaj bölücü
const int PIN_AERO_ADC = 35;    // ADC1_CH7 - Aerojel direnç bölücü

const float R1 = 100000.0;      // 100 kOhm üst direnç
const float R2 = 100000.0;      // 100 kOhm alt direnç
const float CAL = 1.000;        // Multimetre ile kalibre edilecek

const float R_FIXED = 10000.0;  // Aerojelin oda sıcaklığı direncine yakın sabit direnç (10 kOhm)
float coeff_a = 0.0, coeff_b = 0.0, coeff_c = 0.0; // Kalibrasyon katsayıları

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_BATT_ADC, ADC_11db);
  analogSetPinAttenuation(PIN_AERO_ADC, ADC_11db);
}

void loop() {
  // 1. Batarya Voltajı (32 örnek ortalaması, mV bazlı)
  long mv_batt_sum = 0;
  for (int i = 0; i < 32; i++) mv_batt_sum += analogReadMilliVolts(PIN_BATT_ADC);
  float v_adc_batt = (mv_batt_sum / 32.0) / 1000.0;
  float v_batt = v_adc_batt * ((R1 + R2) / R2) * CAL;

  // 2. Aerojel Direnci ve Sıcaklığı
  long mv_aero_sum = 0;
  for (int i = 0; i < 32; i++) mv_aero_sum += analogReadMilliVolts(PIN_AERO_ADC);
  float v_adc_aero = (mv_aero_sum / 32.0) / 1000.0;
  float r_aero = (v_adc_aero < 3.25) ? (R_FIXED * v_adc_aero / (3.3 - v_adc_aero)) : -1.0;
  float t_aero = coeff_a + coeff_b * r_aero + coeff_c * r_aero * r_aero;

  Serial.printf("V_batt=%.3f V | R_aero=%.1f ohm | T_aero=%.2f C\n", v_batt, r_aero, t_aero);
  delay(1000);
}
```

### 6.2. Python ile Kalibrasyon Eğrisi Çıkarma (`numpy.polyfit`)
```python
import numpy as np

# Deneyde ölçülen örnek veriler (Aerojel direnci Ohm -> Referans Sıcaklık °C)
r_aero_measured = np.array([12500, 11800, 11050, 10200, 9450, 8700], dtype=float)
temp_ref_c      = np.array([25.0,  30.0,  35.0,  40.0,  45.0, 50.0], dtype=float)

# 2. dereceden polinom fit: T = c * R^2 + b * R + a
c, b, a = np.polyfit(r_aero_measured, temp_ref_c, 2)
print(f"float coeff_a = {a:.6f};")
print(f"float coeff_b = {b:.6e};")
print(f"float coeff_c = {c:.6e};")
```
