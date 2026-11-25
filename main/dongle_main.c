/*
 * ESP32 BT Dongle: a classic ESP32 as a dual-mode (BR/EDR + LE) Bluetooth controller, reached over
 * HCI H4 on UART0, the UART wired to the dev board's USB-serial chip (CP2102 / CH340).
 *
 * The controller runs in VHCI mode and h4_bridge.c carries the packets to and from UART0. That (and
 * not the controller's own UART H4 mode) because the built-in mode is limited to UART1/2 and
 * 921600 baud, and because the bridge can answer a few vendor commands of its own (identify, baud
 * switch, link statistics).
 */
#include "nvs_flash.h"
#include "esp_bt.h"
#include "h4_bridge.h"

void app_main(void)
{
    /* NVS holds the PHY calibration data */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    esp_bt_controller_config_t cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_BTDM));

    h4_bridge_start();
}
