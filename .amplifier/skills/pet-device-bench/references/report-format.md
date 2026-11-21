# Bench console and report format

Bench build only (`PET_PRIVATE_MIC_DIAG=1`). Lines are ASCII, newline terminated,
positional unsigned decimals. Times are microseconds unless the name says ms.
Nothing here carries audio, words, names, addresses or identifiers.

## Console commands (31 characters at most, newline terminated)

| Command | Effect | Answer |
| --- | --- | --- |
| `voice on` / `voice off` | same as the on-screen mic control | `pet-bench-v1 voice on` / `off` |
| `voice core 0\|1\|any` | core of the voice owner, following calls | `pet-bench-v1 voice core` |
| `voice prio 4\|5\|6` | priority of the voice owner, following calls | `pet-bench-v1 voice prio` |
| `voice audio <5\|6\|7> <a\|0\|1>` | priority and core (a = unpinned) of the audio tasks, next call only | `pet-bench-v1 voice audio` |
| `voice jitter 320\|480\|640\|800\|960\|1280` | playback prebuffer in ms, following calls | `pet-bench-v1 voice jitter <ms>` |
| `voice bw 20\|40` | station channel bandwidth | `pet-bench-v1 voice bw <bw> <result>` |
| `voice last 0\|1\|2` | print a kept report (0 = newest) | see Kept reports |
| `voice scan` | scan for the joined network's access points | see Scan |

An acknowledgement confirms the command was taken, not that a call connected.

## Report, written when a call has been released

```
pet-mic-v1 begin <bins> <duration_ms> <clipped> <residual_bytes> <exit_reason>
pet-mic-v1 bin <i> <read> <read_errors> <read_max_us> <startup> <gated> <admitted> <queue_peak> <popped> <completed>
pet-mic-v1 end
pet-bench-v1 begin <bins> <clipped>
pet-bench-v1 bin <i> <send_count> <send_us_total> <send_us_max> <frame_count> <frame_us_total> <frame_us_max> <loop_us_max> <frames_waiting_peak> <playback_peak> <play_bytes> <play_skipped> <play_write_us_max>
pet-bench-v1 transport <write_wait_us_total> <write_wait_us_max> <write_wait_count> <write_us_total> <write_us_max> <read_hold_us_total> <read_hold_us_max> <read_hold_count> <idle_read_polls>
pet-bench-v1 task <name> <core|-1> <priority> <delta_us>
pet-bench-v1 tasks_status <before_count> <after_count> <lost_count>
pet-bench-v1 tasks_total <elapsed_us>
pet-bench-v1 playback <prebuffer_ms> <underruns> <underrun_ms_total> <underrun_ms_max> <u_lt100> <u_lt200> <u_lt400> <u_lt800> <u_lt1600> <u_ge1600> <arrivals> <arrival_gap_ms_max> <a_lt150> <a_lt250> <a_lt400> <a_lt800> <a_lt1600> <a_ge1600>
pet-bench-v1 wifi <rssi_start> <rssi_min> <rssi_end> <channel> <second> <bandwidth> <is_11n>
pet-bench-v1 end
```

- 32 one-second bins from the start of capture. Audio is 48000 bytes per second
  (24 kHz, 16-bit, mono), so a healthy bin reads, admits and completes 48000.
- `queue_peak` is the microphone buffer (96000 = full, two seconds). `completed`
  is audio the socket accepted, not audio the other end received.
- `play_write_us_max` above 60000 means the codec ran dry.
- RSSI is sent as a positive number (57 means -57 dBm); 0 means unavailable.
- A report without `pet-bench-v1 end` is incomplete and must be discarded.

`exit_reason`: 0 none, 1 microphone read failed, 2 microphone buffer overflow,
3 guard, 4 callback, 5 send failed, 6 disconnected, 7 time limit, 8 mic off by the
person, 9 ended by the host, 10 could not connect, 11 setup, 12 audio unavailable,
13 speaker, 14 protocol, 15 host never began, 16 reconnect window.

## Kept reports

```
pet-bench-v1 kept <back> <calls_since_boot> <bytes> <call_start_uptime_ms> <now_uptime_ms>
<the report above, unchanged>
pet-bench-v1 kept-end
```

`bytes` 0 means no such call since boot. `kept-end` is printed only after every
byte was written.

## Scan

```
pet-bench-v1 ap <index> <tag> <channel> <rssi> <joined>
pet-bench-v1 scan <listed> <result_code>
```

`tag` is a 16-bit hash that tells access points apart; it is not an address.
