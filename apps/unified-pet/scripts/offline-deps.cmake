# --offline-cache only. dependencies.lock is pinned and every cached source byte
# is checked by offline_cache.py before CMake. With the manager disabled these
# namespace-stripped components need the lock's manifest-only dependency edges.
# No SDK/vendor sources are patched. The LVGL spelling alias has no source.
function(pet_offline_manifest_edges)
  if(NOT TARGET __idf_main)
    return()
  endif()
  target_link_libraries(__idf_esp_io_expander_tca9554 PUBLIC __idf_esp_io_expander)
  target_link_libraries(__idf_esp_lcd_co5300 PRIVATE __idf_cmake_utilities)
  target_link_libraries(__idf_esp_lcd_panel_io_additions PUBLIC __idf_esp_io_expander PRIVATE __idf_cmake_utilities)
  target_link_libraries(__idf_esp_lcd_touch_cst816s PUBLIC __idf_esp_lcd_touch)
  target_link_libraries(__idf_esp_lcd_touch_ft5x06 PUBLIC __idf_esp_lcd_touch)
  target_link_libraries(__idf_esp_lvgl_port INTERFACE __idf_lvgl)
  target_link_libraries(__idf_main PRIVATE __idf_esp_lvgl_port __idf_esp_websocket_client __idf_lvgl __idf_esp32_s3_touch_amoled_1_8)
  target_link_libraries(__idf_esp32_s3_touch_amoled_1_8 PUBLIC
    __idf_esp_codec_dev __idf_esp_io_expander_tca9554 __idf_esp_lvgl_port
    PRIVATE __idf_esp_lcd_co5300 __idf_esp_lcd_panel_io_additions
    __idf_esp_lcd_touch_cst816s __idf_esp_lcd_touch_ft5x06 __idf_lvgl)
endfunction()
cmake_language(DEFER CALL pet_offline_manifest_edges)