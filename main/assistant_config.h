#pragma once
/* The voice transport and backend are independent. This isolated audio spike
 * uses managed Responses. Replace this seam with authenticated Unified client
 * delegation once its installed-host contract and device access are qualified.
 * This configuration has no Unified access and performs no external actions. */
#define PET_VOICE_MODEL "gpt-live-1"
#define PET_REASONING_MODEL "gpt-6-luna"
#define PET_GREETING "Greet the caller now in English. Say hello, introduce yourself as Tachikoma, and ask how you can help. Then pause and listen."
#define PET_LIVE_START \
 "{\"type\":\"session.start\",\"event_id\":\"pet_start\",\"session\":{" \
 "\"model\":\"" PET_VOICE_MODEL "\",\"store\":false," \
 "\"input\":[{\"type\":\"message\",\"role\":\"developer\",\"content\":[{\"type\":\"input_text\",\"text\":\"" PET_GREETING "\"}]}]," \
 "\"instructions\":\"You are Tachikoma, a warm, curious pocket assistant. " \
 "Keep spoken answers brief and clear. Delegate reasoning to the backend. " \
 "You are in an isolated voice test and cannot access Amplifier Unified, " \
 "workspaces, notifications or external actions. Never claim to have done so.\"," \
 "\"audio\":{\"format\":{\"type\":\"audio/pcm\",\"rate\":24000},\"output\":{\"voice\":\"marin\"}}," \
 "\"delegation\":{\"type\":\"responses\",\"responses\":{" \
 "\"model\":\"" PET_REASONING_MODEL "\",\"instructions\":\"Help with concise explanations, planning and reasoning. " \
 "No Unified connector or external tools are available. Be explicit about that limitation.\"}}}}"
#define PET_GREETING_EVENT "{\"type\":\"session.commentary.append\",\"event_id\":\"pet_greeting\",\"delegation_id\":null,\"content\":\"" PET_GREETING "\"}"
