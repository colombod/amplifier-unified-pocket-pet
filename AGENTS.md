# Working on this experiment

- GitHub issues own work, evidence and learning outcomes; do not create a parallel
  work-tracker queue.
- Preserve original product intent in `docs/intent.md`. Keep interpretations,
  hypotheses, proposed checks, measurements and decisions distinct.
- These are de-risking spikes, not frozen architecture or ratified Converge
  contracts. Do not activate a Converge manager or lanes for repository setup.
- Scope experiments explicitly. Record exact hardware/software versions,
  measurement methods, observations, failures and limitations in their issue.
  A documented negative or blocked outcome can conclude a spike; it is not
  successful product acceptance.
- Never commit credentials, Wi-Fi passwords, tailnet enrollment keys, private
  keys, raw audio or local captures. Ignore rules are not a security boundary;
  inspect the staged files before every push.
- Use supported interfaces only. Never invent endpoints from app action names,
  forge human provenance, bypass host ownership or silently take over sessions.
- The initial repository setup authorized records only, not firmware, installs,
  flashing, server/Tailscale changes or deployment. Later work follows its actual
  authorization, not that historical setup boundary.
- Toolkit provisioning is separately authorized at workspace `.toolchains/esp32s3`
  and `working-files/esp-idf-preflight`, isolated from Amplifier's environment.
  Keep SDK/build outputs outside this repo. Flashing waits for a later device
  handoff; toolkit authorization does not authorize it.
- `docs/VISION.md` is draft direction for review, not ratified direction or evidence
  that the product exists. Keep spike findings distinct from product acceptance.
- For docs-only changes: review the exact staged diff, run
  `git diff --cached --check`, check links and source quotations, and inspect for
  secrets. Report these checks honestly; no runtime test exists or is implied
  by this seed.
