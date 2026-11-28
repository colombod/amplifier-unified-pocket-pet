/- Executable requirements model for a future local wake detector and attention channel.
   The clock is monotonic; the caller orders mute and the hard cap before media events.
   Loop timing, model accuracy and the gateway transport are outside this proof. -/
namespace PocketWake

inductive Phase where
  | muted | armed | listening | waiting | playing
  deriving Repr, DecidableEq

structure Config where
  phrase : Nat  -- installed model identity selected by the owner
  enabled : Bool

structure Session where
  phase : Phase
  deadline : Nat   -- next human-turn deadline, monotonic seconds
  expires : Nat    -- immutable duration cap for this call
  attention : Bool -- abstract existence of a pending authenticated question
  deriving Repr, DecidableEq

inductive Event where
  | mute | tap | detected (phrase : Nat) | speech | submitted
  | exactReply | playbackDrained | linkedQuestion | tick
  deriving DecidableEq

def resting (c : Config) := if c.enabled then Phase.armed else Phase.muted

def active : Phase → Bool
  | .listening | .waiting | .playing => true
  | _ => false

def step (c : Config) (s : Session) (e : Event) (now : Nat) : Session :=
  let closed := {s with phase := resting c}
  let start := {s with phase := .listening, deadline := now + 90, expires := now + 600}
  if e == .mute then {s with phase := .muted}
  else if active s.phase && now ≥ s.expires then
    {closed with attention := s.attention || e == .linkedQuestion}
  else match e with
    | .linkedQuestion => {s with attention := true}
    | .tap => if active s.phase then s else start
    | .detected phrase =>
      if c.enabled && s.phase == .armed && phrase == c.phrase then start else s
    | .speech => if s.phase == .listening then {s with deadline := now + 90} else s
    | .submitted => if s.phase == .listening then {s with phase := .waiting} else s
    | .exactReply => if s.phase == .waiting then {s with phase := .playing} else s
    | .playbackDrained =>
      if s.phase == .playing then {s with phase := .listening, deadline := now + 90} else s
    | .tick => if s.phase == .listening && now ≥ s.deadline then closed else s
    | .mute => s

theorem stop_wins (c s t) : (step c s .mute t).phase = .muted := by simp [step]
theorem mute_blocks_wake (c s phrase t) (h : s.phase = .muted) :
    step c s (.detected phrase) t = s := by simp [step, h, active]
theorem wrong_phrase_ignored (c s phrase t) (h : s.phase = .armed) (ne : phrase ≠ c.phrase) :
    step c s (.detected phrase) t = s := by simp [step, h, active, ne]
theorem selected_phrase_opens (c s t) (h : s.phase = .armed) (en : c.enabled = true) :
    (step c s (.detected c.phrase) t).phase = .listening := by simp [step,h,en,active]
theorem wait_survives_silence (c s t) (h : s.phase = .waiting) (room : t < s.expires) :
    step c s .tick t = s := by simp [step,h,active,Nat.not_le.mpr room]
theorem playing_survives_silence (c s t) (h : s.phase = .playing) (room : t < s.expires) :
    step c s .tick t = s := by simp [step,h,active,Nat.not_le.mpr room]
theorem playback_gives_fresh_turn (c s t) (h : s.phase = .playing) (room : t < s.expires) :
    (step c s .playbackDrained t).deadline = t+90 := by simp [step,h,active,Nat.not_le.mpr room]
theorem silence_closes (c s t) (h : s.phase = .listening) (idle : s.deadline ≤ t) :
    (step c s .tick t).phase = resting c := by
  by_cases cap : s.expires ≤ t <;> simp [step,h,active,idle,cap]
theorem question_never_opens_muted (c s t) (h : s.phase = .muted) :
    (step c s .linkedQuestion t).phase = .muted ∧ (step c s .linkedQuestion t).attention := by
  simp [step,h,active]
theorem cap_closes_wait (c s t) (h : s.phase = .waiting) (cap : s.expires ≤ t) :
    (step c s .exactReply t).phase = resting c := by simp [step,h,active,cap]
end PocketWake
