# Browser setup preview

The Pages site pairs with a running pet through Web Serial. On the pet select Settings → Phone setup, then enter the displayed setup Wi-Fi password on the USB settings tab. The browser sends framed requests over USB. Configuration fields and flash backups stay local. Disconnection during a save leaves its outcome uncertain. Reconnect and refresh before deciding whether to repeat it.

The Firmware tab resets the selected port into the Espressif loader. It requires a matching 16 MB ESP32-S3, recognized application digest, matching partition table and compatible chip revision. Store the full backup in a new private local file. The site verifies the saved file, asks for a separate Flash action, checks the entire flash again and writes only the candidate app at 0x10000. It verifies the new image and the untouched regions before restarting. Keep a failed update connected or disconnect without rebooting. The [project updater](../apps/unified-pet/scripts/update_app.py) describes the supported layout. The backup contains credentials and belongs outside Git.

## Build and verify

Run `npm ci` here. Run `npm run build -- /path/to/preview/unified_pet.bin` using the exact image identified in `firmware.json`. The reviewed preview binary is also included in the published setup archive. Independently rebuilt firmware can have a different digest because ESP-IDF embeds build metadata. Check the application first and update the pin intentionally when preparing a new preview.

For `npm test`, set `PET_PRIOR_APP` to the public 0.3.21 release's unified_pet.bin and `PET_PARTITION_TABLE` to the supported build's partition-table.bin. Set `CHROME_PATH` when Chrome is outside the default macOS location. The tests use fake Web Serial and synthetic flash populated with public firmware bytes. They do not request a real port. The production firmware admission test is included in the app native suite. The Pages workflow deploys the digest-pinned, self-contained release archive when the pages/setup-preview branch advances.

The 0.3.22 preview requires physical qualification of Web Serial control-line reset, throughput, recovery after a real cable interruption, configuration persistence, wake accuracy in a future detector build and normal pet UX after reboot. It contains no wake detector.
