import Std.Tactic.BVDecide

namespace PocketPet

-- network_retry.h uses modulo-2^32 milliseconds and a signed deadline delta.
abbrev Tick := BitVec 32
def nextDelay (d : Tick) : Tick :=
  if d = 0 then 5000 else if d+d > 60000 then 60000 else d+d

theorem backoff_bounds (d : Tick) (h : d = 0 ∨ (5000 ≤ d ∧ d ≤ 60000)) :
    5000 ≤ nextDelay d ∧ nextDelay d ≤ 60000 := by
  rcases h with hz | bounds
  · subst d
    decide
  · unfold nextDelay
    split
    · decide
    · split
      · decide
      · simp [BitVec.le_def, BitVec.lt_def, BitVec.toNat_add] at *
        omega

def retryDue (scheduled connected joining queued setup : Bool) (now deadline last : Tick) : Bool :=
  scheduled && !connected && !joining && !queued &&
    (!setup || decide (now-last ≥ 60000)) && !(now-deadline).msb

theorem retry_preserves_activity (s c j q setup : Bool) (now due last : Tick)
    (h : retryDue s c j q setup now due last = true) :
    s = true ∧ c = false ∧ j = false ∧ q = false ∧
      (setup = true → now-last ≥ 60000) := by
  cases setup <;> simp_all [retryDue]

theorem retry_first_due_across_wrap (start delay : Tick) :
    retryDue true false false false false (start+delay-1) (start+delay) start = false ∧
    retryDue true false false false false (start+delay) (start+delay) start = true := by
  simp [retryDue]
  bv_decide

-- Conversation capture is a single writer. A full chunk is admitted or all
-- its samples are dropped. The consumer may only increase the available room.
def admittedBytes (used chunk capacity : Nat) := if used + chunk ≤ capacity then chunk else 0

theorem capture_bound (used chunk cap : Nat) (h : used ≤ cap) :
    used + admittedBytes used chunk cap ≤ cap ∧
    (admittedBytes used chunk cap = 0 ∨ admittedBytes used chunk cap = chunk) := by
  unfold admittedBytes
  split <;> omega

theorem capture_stall_budget : 48 * 5000 + 4800 = 244800 ∧
    (244800 : Nat) % 960 = 0 ∧ (15360 : Nat) / 48 = 320 := by decide

-- After a little-endian PCM16 chunk has been decoded, the C peak is
-- the maximum absolute amplitude. Backlog recovery may skip zero peak only.
def peak : List Int → Nat
  | [] => 0
  | x :: xs => max x.natAbs (peak xs)

theorem peak_zero_iff_silence (samples : List Int) :
    peak samples = 0 ↔ ∀ x ∈ samples, x = 0 := by
  induction samples with
  | nil => simp [peak]
  | cons x xs ih =>
    have zero : max x.natAbs (peak xs) = 0 ↔ x.natAbs = 0 ∧ peak xs = 0 := by omega
    simp [peak, zero, ih]

theorem catchup_keeps_nonzero (samples : List Int) (x : Int)
    (present : x ∈ samples) (nonzero : x ≠ 0) (queued : Nat) :
    ¬ (peak samples = 0 ∧ queued > 24000) := by
  intro skipped
  exact nonzero ((peak_zero_iff_silence samples).mp skipped.1 x present)

-- Compare with the current LVGL value before invoking a setter. For any
-- sequence of targets, every tick presents exactly the requested value.
def setIfChanged [DecidableEq α] (shown target : α) : α :=
  if shown = target then shown else target

def rendered [DecidableEq α] (shown : α) : List α → List α
  | [] => [shown]
  | target :: rest => shown :: rendered (setIfChanged shown target) rest

theorem refresh_sequence [DecidableEq α] (shown : α) (targets : List α) :
    rendered shown targets = shown :: targets := by
  induction targets generalizing shown with
  | nil => rfl
  | cons x xs ih =>
    have same : setIfChanged shown x = x := by simp [setIfChanged]
    simp [rendered, same, ih]

-- Confirming a power action requires a release observed after the modal
-- opened, then a press on its confirm button. A lost press cancels that press.
inductive Touch | waiting | ready | pressed deriving DecidableEq
inductive Input | release | hold | press | lost | accept deriving DecidableEq

def touchStep : Touch → Input → Touch
  | _, .accept => .waiting
  | .waiting, .release => .ready
  | .ready, .press => .pressed
  | .pressed, .lost => .ready
  | s, _ => s

theorem held_input_never_arms (events : List Input) (h : Input.release ∉ events) :
    events.foldl touchStep .waiting = .waiting := by
  induction events with
  | nil => rfl
  | cons e es ih =>
    simp only [List.mem_cons, not_or] at h
    cases e <;> simp_all [touchStep]

theorem cannot_confirm_twice (s : Touch) : touchStep s .accept ≠ .pressed := by
  cases s <;> decide

inductive ReadResult | found | missing | error deriving DecidableEq
def readLegacy (own : ReadResult) (legacyReady : Bool) : Bool :=
  decide (own = .missing) && legacyReady

theorem legacy_requires_missing (own : ReadResult) (ready : Bool)
    (h : readLegacy own ready = true) : own = .missing ∧ ready = true := by
  simpa [readLegacy] using h

-- Independent bit-level spec for the two status bytes in register order.
-- Index 0 = unknown, 1 = USB, 2 = battery, matching power_source_t.
def source (s₁ s₂ : BitVec 8) : Nat :=
  let good := s₁.getLsbD 5
  let cell := s₁.getLsbD 3
  let current := (s₂ >>> 5) &&& 3
  if good && (current == 0 || (cell && current == 1)) then 1
  else if !good && cell && current == 2 then 2 else 0

theorem battery_requires_cell (a b : BitVec 8) (h : source a b = 2) :
    a.getLsbD 3 = true ∧ a.getLsbD 5 = false ∧ ((b >>> 5) &&& 3) = 2 := by
  simp only [source] at h
  split at h <;> simp_all

theorem usb_only (a b : BitVec 8) (g : a.getLsbD 5 = true) (c : a.getLsbD 3 = false)
    (i : ((b >>> 5) &&& 3) = 0) : source a b = 1 := by
  simp_all [source]

end PocketPet

-- Machine-readable oracle. Compare every input with the compiled firmware decoder.
def main : IO Unit := do
  for a in [:256] do
    let row := (List.range 256).map fun b =>
      toString (PocketPet.source (BitVec.ofNat 8 a) (BitVec.ofNat 8 b))
    IO.println (String.join row)
