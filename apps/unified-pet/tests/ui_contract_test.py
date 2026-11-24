"""Source-level UI regression guard, not a rendered-screen or hardware test.

Check the actual constructor/callback code used by the target build. Keep one
primary voice control and useful settings; never reintroduce extra Stop or
user-facing qualification controls.

Conversation mode (user decision 2026-11-14): the microphone button is only
mic on / mic off. There is no "Done talking", transcript preview, Send or
voice Cancel button, and no device-side send confirmation. The host, not the
pet, decides what is sent. The separate device-power modal may offer Cancel.
"""
from pathlib import Path
import re
import unittest

APP = Path(__file__).resolve().parents[1]
SOURCE = (APP / "main/main.c").read_text(encoding="utf-8")
LIVE = (APP / "main/live_voice.c").read_text(encoding="utf-8")
LIVE_H = (APP / "main/live_voice.h").read_text(encoding="utf-8")
BRIDGE = (APP / "main/bridge_voice.c").read_text(encoding="utf-8")


class PetUI(unittest.TestCase):
    def test_one_primary_voice_control_no_extra_stop(self):
        self.assertEqual(SOURCE.count("talk_button=lv_button_create(screen)"), 1)
        self.assertNotIn("bridge_stop=", SOURCE)
        self.assertNotRegex(SOURCE, r'button\(screen,\s*"[^"\n]*Stop')
        # No substitute button with a different name may bind Stop at root.
        self.assertNotRegex(SOURCE, r'button\(screen,[^;]*bridge_stop_clicked')
        roots = re.findall(r'button\(screen,([^;]+)', SOURCE)
        self.assertEqual(len(roots), 1)  # Settings; voice has its own constructor.
        self.assertIn("settings_clicked", roots[0])

    def test_normal_screens_have_no_qualification_controls(self):
        for text in (
            '"Test Tailscale"', '"Cancel Tailscale"', '"Audio check"',
            '"1. Speaker tone"', '"2. Record 3 seconds"',
            '"First check the speaker.', '"Join %d TCP %d TLS %d HTTP %d',
            '"%s%s  error %d',
        ):
            with self.subTest(text=text):
                self.assertNotIn(text, SOURCE)
        self.assertNotIn("tailnet_spike_start()", SOURCE)
        self.assertNotIn("audio_test_request(", SOURCE)
        self.assertNotIn(" dBm", SOURCE)

    def test_useful_settings_and_artwork_remain(self):
        for text in ('"Phone setup"', '"Volume"', '"Touch alignment"',
                     '"Back to Tachikoma"', '"Tachikoma"'):
            self.assertIn(text, SOURCE)
        self.assertIn("audio_test_save_volume()", SOURCE)
        self.assertIn("touch_calibration_begin()", SOURCE)
        self.assertIn("network_open_setup()", SOURCE)
        self.assertIn("lv_image_set_src(pet,&frames[frame])", SOURCE)
        self.assertIn("tailnet_spike_snapshot(&tn)", SOURCE)  # Admission intact.

    def test_microphone_button_is_only_mic_on_and_mic_off(self):
        # The whole tap handler: off when on, on when off. Nothing else.
        body = SOURCE.split("static void talk_clicked(lv_event_t *event){", 1)[1].split("\n}\n", 1)[0]
        self.assertEqual(
            [line.strip() for line in body.strip().splitlines()],
            ["(void)event;live_status_t s;live_voice_snapshot(&s);",
             "if(s.active)live_voice_stop();",
             "else live_voice_start();"])
        # Only that tap starts or stops voice: never a timer, refresh or reconnect.
        self.assertEqual(SOURCE.count("live_voice_start("), 1)
        self.assertEqual(SOURCE.count("live_voice_stop("), 1)
        self.assertEqual(SOURCE.count("lv_obj_add_event_cb(talk_button,talk_clicked,LV_EVENT_CLICKED,NULL)"), 1)
        for text in ('"Mic off - tap to talk"', '"Mic on - tap to mute"', '"Tap to cancel"', '"Closing..."'):
            self.assertIn(text, SOURCE)
        self.assertEqual(SOURCE.count("lv_label_set_text(talk_label,"), 1)

    def test_reconnecting_keeps_the_mic_on_look_and_only_while_mic_is_on(self):
        # Mic-on look is everything except connecting and closing, so the
        # reconnecting state keeps it; the screen has no reconnect control.
        self.assertIn("bool mic_on=voice.active&&!closing&&!connecting;", SOURCE)
        self.assertIn("bool connecting=voice.active&&voice.state==LIVE_CONNECTING;", SOURCE)
        self.assertNotIn("econnect", SOURCE.replace("LIVE_RECONNECTING", "").split("void app_main(void)")[1])
        self.assertEqual(SOURCE.count("LIVE_RECONNECTING"), 1)  # the pet's animation only
        # The bridge reconnects only inside its own mic-on loop: it waits by
        # polling the parent's mic-off, and nothing else calls run_call.
        loop = BRIDGE.split("void bridge_voice_run(", 1)[1]
        self.assertIn('if(parent_stopped&&parent_stopped()){between_calls(LIVE_OFF,"Mic off");return;}', loop)
        self.assertIn("if(!call_report.again)return;", loop)
        # live_voice still starts the bridge only from the worker of an accepted tap.
        self.assertEqual(LIVE.count("bridge_voice_run("), 1)

    def test_no_preview_send_or_cancel_in_conversation_mode(self):
        for text in ("Done talking", "Confirm your words", '"Send"', "Start voice",
                     "bridge_panel", "bridge_send", "bridge_text", "bridge_display",
                     "bridge_stop_clicked", "bridge_send_clicked", "BR_PREVIEW",
                     "can_finish_capture", "can_send", "copy_preview", "confirm_send",
                     "finish_capture", "bridge_protocol.h", "stop_icon"):
            with self.subTest(text=text):
                self.assertNotIn(text, SOURCE)
        # No path from the screen or the voice worker to a device-side send
        # confirmation or to "done talking": those entry points are gone.
        for text in ("live_voice_confirm_send", "live_voice_finish_capture",
                     "live_voice_copy_preview", "live_voice_bridge_snapshot",
                     "bridge_voice_confirm", "bridge_voice_finish_capture",
                     "bridge_voice_copy_preview"):
            with self.subTest(text=text):
                self.assertNotIn(text, SOURCE)
                self.assertNotIn(text, LIVE)
                self.assertNotIn(text, LIVE_H)

    def test_power_confirmation_is_local_and_exclusive(self):
        for label,callback in (("Restart","restart_clicked"),("Shut down","shutdown_clicked")):
            self.assertRegex(SOURCE, rf'button\(settings_panel,"{label}",[^;]*{callback}\)')
        cancels = [line for line in SOURCE.splitlines() if 'button(' in line and '"Cancel"' in line]
        self.assertEqual(cancels, ['    power_cancel=button(power_panel,"Cancel",-80,-174,132,power_close);'])
        dismiss = SOURCE.split("static void power_close(",1)[1].split("\n}",1)[0]
        self.assertNotRegex(dismiss, r"(claim|device_power_confirmed|live_voice|restart\()")
        self.assertEqual(SOURCE.count("device_power_confirmed("),1)
        accept = SOURCE.split("static void power_accept(",1)[1].split("\n}",1)[0]
        effect = accept.index("device_power_confirmed(")
        for check in ("power_touch_ready()", "power_touch_accept(&power_touch)",
                      "tailnet_spike_power_claim()", "network_power_claim()"):
            self.assertLess(accept.index(check),effect)
        self.assertNotRegex(accept,r"(live_voice_|bridge_voice_|nvs_)")

    def test_bridge_voice_starts_conversation_mode_only(self):
        self.assertEqual(BRIDGE.count("bridge_command(&c->model,BR_CMD_CONVERSE,0)"), 1)
        # Every call, first or re-established, goes through the one run_call.
        loop = BRIDGE.split("void bridge_voice_run(", 1)[1]
        self.assertEqual(loop.count("run_call("), 1)
        self.assertEqual(BRIDGE.count("run_call("), 2)
        # The one-turn model is never started by the firmware.
        self.assertNotIn("BR_CMD_CONNECT", BRIDGE)
        self.assertEqual(LIVE.count("bridge_voice_run("), 1)
        # The longer conversation deadline applies to the bridge path only.
        self.assertIn("#define VOICE_SESSION_LIMIT_MS 60000", LIVE)
        self.assertIn("#define BRIDGE_SESSION_LIMIT_MS 660000", LIVE)


    def test_settings_names_only_the_network_associated_now(self):
        network = (APP / "main/network.c").read_text(encoding="utf-8")
        # One writer of the name: the station's own access-point record.
        self.assertEqual(network.count("wifi_name_update(state.ssid,"), 1)
        self.assertNotRegex(network, r"state\.ssid,\s*cmd->ssid")
        self.assertNotRegex(network, r"strlcpy\(state\.ssid")
        # Joining wipes it before anything else; losing the link reaches the
        # refresh with connected already false.
        join = network.split("static bool join_network(", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("state.connected=false; memset(state.ssid,0,sizeof(state.ssid));", join)
        self.assertNotIn("ssid", join.split("ESP_ERROR_CHECK(esp_wifi_set_config", 1)[1].split("nvs_set_str", 1)[0])
        worker = network.split("static void worker(", 1)[1]
        self.assertEqual(worker.count("refresh_station(s.connected);"), 1)
        self.assertLess(worker.index("s.connected=false;message("), worker.index("refresh_station(s.connected);"))
        # The network name is never logged.
        for line in network.splitlines():
            if "ESP_LOG" in line:
                self.assertNotIn("ssid", line.replace("ap_ssid", ""))
        # The screen words it through the one rule, on one fixed line; the
        # signal words stay, without numbers.
        self.assertEqual(SOURCE.count("s.ssid"), 1)
        self.assertIn("wifi_name_line(connection,sizeof(connection),s.connected,s.joining,s.ssid);", SOURCE)
        self.assertIn("lv_label_set_long_mode(settings_connection,LV_LABEL_LONG_MODE_DOTS);", SOURCE)
        self.assertIn('lv_label_set_text_fmt(settings_signal,"Signal %s",signal);', SOURCE)
        self.assertIn('lv_label_set_text(settings_signal,"");', SOURCE)
        settings = SOURCE.split("settings_panel=lv_obj_create(screen)", 1)[1]
        self.assertNotIn("password", settings)
        rule = (APP / "main/wifi_name.h").read_text(encoding="utf-8")
        for text in ('"Wi-Fi: %s"', '"Wi-Fi connected"', '"Wi-Fi connecting..."', '"Wi-Fi disconnected"'):
            self.assertIn(text, rule)
        self.assertNotIn("ESP_LOG", rule)


if __name__ == "__main__":
    unittest.main()
