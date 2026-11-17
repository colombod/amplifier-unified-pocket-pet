"""Task boundary and dirty-baseline preservation checks (no Git dependency)."""
import argparse
import hashlib
import json
import os
import re
import unittest
from pathlib import Path, PurePosixPath

APP=Path(__file__).resolve().parents[1]
ROOT=APP.parents[1]
BASE=None
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
# Explicit Task 2 edits only. This is not a directory-wide app exemption:
# every other app file stays pinned to reviewed Task 1, including NVS storage.
TASK2_PATHS={
    "apps/unified-pet/main/live_voice.c",
    "apps/unified-pet/tests/run_native.sh",
    "apps/unified-pet/tests/voice_pipeline_test.c",
    "apps/unified-pet/tests/voice_host.h",
    "apps/unified-pet/tests/run_voice_pipeline.sh",
    "apps/unified-pet/tests/isolation_test.py",
    "apps/unified-pet/tests/isolation_contract.json",
    "apps/unified-pet/tests/isolation_negative_test.py",
}
# Parent-owned status ledger changes throughout task execution. Exempt content
# for this ONE path only, not .superpowers, manifests, product source or config.
MUTABLE_METADATA={
    ".superpowers/sdd/2026-11-11-unified-pet-foundation/progress.md":
        "Parent-owned mutable task status; not immutable product source",
}
CAP_FIXTURE_PATHS={
    "apps/unified-pet/tests/audio_lifecycle_test.c",
    "apps/unified-pet/tests/run_audio_lifecycle.sh",
}
INPUT_DIAG_PATHS={
    "apps/unified-pet/main/main.c",
    "apps/unified-pet/tests/input_diag_test.py",
}


def typed_manifest(path, *, artifacts=False, absent=False, section=None):
    """Historical source entries are objects; artifact values are digest strings."""
    value=json.loads(path.read_text(encoding="utf-8"))
    if section is not None:
        value=value[section]
    if not isinstance(value,dict):
        raise ValueError(f"manifest must be an object: {path}")
    for name,entry in value.items():
        p=PurePosixPath(name)
        if not name or ".." in p.parts or (p.is_absolute() and not artifacts):
            raise ValueError(f"invalid manifest path: {name}")
        if artifacts:
            valid=isinstance(entry,str) and re.fullmatch("[a-f0-9]{64}",entry)
        elif entry is None:
            valid=absent
        else:
            valid=isinstance(entry,dict) and (
                (set(entry)=={"sha256"} and isinstance(entry["sha256"],str)
                 and re.fullmatch("[a-f0-9]{64}",entry["sha256"]))
                or (set(entry)=={"link"} and isinstance(entry["link"],str)))
        if not valid:
            raise ValueError(f"invalid manifest entry: {name}")
    return value


def source_paths(root):
    """Owned source scopes, not generated/cache/build artifacts or SDD receipts."""
    for scope in ("main","components","tests","apps"):
        for directory,dirs,files in os.walk(root/scope,followlinks=False):
            # Match the recorded source snapshot's output exclusions.
            dirs[:]=[d for d in dirs if d not in {
                ".git","build","managed_components","generated","__pycache__"}]
            for name in dirs+files:
                p=Path(directory)/name
                if p.is_symlink() or p.is_file():
                    yield p.relative_to(root).as_posix()

class Isolation(unittest.TestCase):
    def assert_entry(self,name,entry):
        p=ROOT/name
        if entry is None:
            self.assertFalse(p.exists() or p.is_symlink(),f"moved path resurrected: {name}")
        elif "link" in entry:
            self.assertTrue(p.is_symlink(),name)
            self.assertEqual(str(p.readlink()),entry["link"],name)
        else:
            self.assertFalse(p.is_symlink(),name)
            self.assertTrue(p.is_file(),name)
            self.assertEqual(sha(p),entry["sha256"],name)

    def assert_retained_artifacts(self,artifacts):
        # Shared by the full public check and private-copy tamper regression.
        # Relative paths retain their original repository-root meaning.
        for name,digest in artifacts.items():
            p=Path(name) if name.startswith("/") else ROOT/name
            self.assertTrue(p.is_file(),name)
            self.assertEqual(sha(p),digest,name)

    def test_source_preservation(self):
        contract_path=APP/"tests/isolation_contract.json"
        contract=json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertEqual(contract["version"],1)
        source_path=BASE.parent/"source-manifest.json"
        self.assertEqual(sha(source_path),contract["original_source_manifest_sha256"])
        manifest=typed_manifest(source_path)
        changes=typed_manifest(contract_path,section="task1_changes",absent=True)
        additions=typed_manifest(contract_path,section="task1_additions")
        fixtures=typed_manifest(contract_path,section="cap_fixture_changes")
        diagnostics=typed_manifest(contract_path,section="input_diag_sources")
        # Diagnostic-only app change: exact files AND digests, never root main
        # or a directory-wide source exemption. Historical baseline stays intact.
        self.assertEqual(set(diagnostics),INPUT_DIAG_PATHS)
        # Exact reviewed fixture revisions, NOT more behavioral allowlist paths.
        # Every fixture still has a strict digest and original Task 1 history.
        self.assertEqual(set(fixtures),CAP_FIXTURE_PATHS)
        self.assertEqual(contract["mutable_metadata"],MUTABLE_METADATA)
        self.assertTrue(set(MUTABLE_METADATA)<=set(manifest))
        self.assertFalse(set(changes)-set(manifest),"Task 1 overrides must name original files")
        self.assertFalse(set(additions)&set(manifest),"Task 1 additions must be genuinely new")
        self.assertTrue(set(fixtures)<=set(additions))
        expected=manifest|changes|additions|fixtures|diagnostics
        for name,entry in expected.items():
            if name in MUTABLE_METADATA:
                # Mutable text does not authorize a symlink escape, directory,
                # deletion, or malformed original manifest entry.
                p=ROOT/name
                self.assertTrue(p.is_file(),name)
                while p!=ROOT:
                    self.assertFalse(p.is_symlink(),name)
                    p=p.parent
                continue
            if name not in TASK2_PATHS:
                self.assert_entry(name,entry)
        for name in TASK2_PATHS:
            self.assertTrue((ROOT/name).is_file(),name)
            self.assertFalse((ROOT/name).is_symlink(),name)
        # No blanket root/shared/vendor/app path exclusion; unexpected source
        # additions are rejected even when absent from the historical manifest.
        self.assertFalse(set(source_paths(ROOT))-set(expected)-TASK2_PATHS,
                         "unexpected source files")

    def test_project_boundary(self):
        self.assertTrue((APP/"CMakeLists.txt").is_file(),"independent app absent")
        for app in (ROOT,APP):
            bodies="\n".join(p.read_text(encoding="utf-8") for p in (app/"main").glob("*.c"))
            self.assertEqual(len(re.findall(r"\bvoid\s+app_main\s*\(",bodies)),1)
            self.assertIn("REQUIRES", (app/"main/CMakeLists.txt").read_text(encoding="utf-8"))
            for name in ("pet_tls","pet_touch_calibration"):
                self.assertIn(name,(app/"main/CMakeLists.txt").read_text(encoding="utf-8"))
            for name in ("touch_calibration.c","touch_calibration.h","touch_alignment.h","tailnet_http.h"):
                self.assertFalse((app/"main"/name).exists(),name)
        for p in (APP/"main").rglob("*"):
            self.assertFalse(p.is_symlink(),str(p))
            if p.is_file():
                text=p.read_text(encoding="utf-8")
                self.assertNotRegex(text,r'(\.\./){2,}main/')
                for include in re.findall(r'#\s*include\s*["<]([^">]+)[">]',text):
                    target=(p.parent/include).resolve()
                    self.assertFalse(target.is_relative_to((ROOT/"main").resolve()),
                                     f"root-main import: {p}: {include}")
        self.assertEqual((APP/"partitions.csv").read_bytes(),(ROOT/"partitions.csv").read_bytes())
        self.assertEqual((APP/"dependencies.lock").read_bytes(),(ROOT/"dependencies.lock").read_bytes())
        # The app keeps every root default byte for byte and may append ONLY the
        # voice-transport TCP settings (tests/link_rig), nothing else.
        root_defaults=(ROOT/"sdkconfig.defaults").read_bytes()
        app_defaults=(APP/"sdkconfig.defaults").read_bytes()
        self.assertTrue(app_defaults.startswith(root_defaults),"root sdkconfig defaults changed or removed")
        added={line.split("=")[0] for line in app_defaults[len(root_defaults):].decode("utf-8").splitlines()
               if not line.startswith("#") and line.strip()}
        self.assertEqual(added,{"CONFIG_LWIP_TCP_SND_BUF_DEFAULT","CONFIG_LWIP_TCP_WND_DEFAULT",
                                "CONFIG_LWIP_TCP_RECVMBOX_SIZE"})
        project=(APP/"CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn("project(unified_pet)",project)
        for dep in ("pet_tls","pet_touch_calibration","tailtiny","wireguard_lwip"):
            self.assertIn(dep,project)
        exporter=(APP/"scripts/export_pet.py").read_text(encoding="utf-8")
        self.assertIn('REPO / "assets/source/tachikoma.png"',exporter)
        self.assertIn('ROOT / "assets/generated"',exporter)
        self.assertTrue((APP/"sdkconfig").is_file())
        self.assertIn('PET_TOUCH_STORE_NAMESPACE="unified_pet"',project)
        network=(APP/"main/network.c").read_text(encoding="utf-8")
        audio=(APP/"main/audio_test.c").read_text(encoding="utf-8")
        self.assertIn('pet_store_open(&storage,"pet_setup")',network)
        self.assertIn('pet_store_open(&volume_compat,"pet_audio")',audio)
        self.assertNotIn("nvs_open(",network)
        self.assertNotIn("nvs_open(",audio)
        self.assertNotIn("nvs_flash_erase",bodies)
        for line in network.splitlines():
            if re.search(r"\bnvs_(set_\w+|commit|erase_\w+)\(",line):
                self.assertIn("storage.own",line)
        # Independent behavior may diverge. Source preservation above pins root
        # and all app files except the explicitly scoped Task 2 paths.

    def test_preservation_boundary(self):
        moves={"main/touch_alignment.h":"components/pet_touch_calibration/include/touch_alignment.h",
               "main/touch_calibration.h":"components/pet_touch_calibration/include/touch_calibration.h",
               "main/tailnet_http.h":"components/pet_tls/include/tailnet_http.h"}
        for old,new in moves.items():
            self.assertTrue((ROOT/new).exists(),new)
            self.assertEqual((BASE/old).read_bytes(),(ROOT/new).read_bytes())
        # The extraction may change only store selection and request ownership.
        tls=(ROOT/"components/pet_tls/pet_tls.c").read_text(encoding="utf-8")
        original=(BASE/"main/tailnet_tls.c").read_text(encoding="utf-8")
        self.assertEqual(tls[tls.index("    struct tls_context"):tls.index("    for(size_t sent=")].split("    out->verified=true")[0],
                         original[original.index("    struct tls_context"):original.index("    for(int sent=")].split("    out->verified=true")[0])
        self.assertEqual(tls[tls.index("    size_t n=0;"):],original[original.index("    size_t n=0;"):])
        touch=(ROOT/"components/pet_touch_calibration/touch_calibration.c").read_text(encoding="utf-8")
        old=(BASE/"main/touch_calibration.c").read_text(encoding="utf-8")
        self.assertEqual(touch[touch.index("static lv_indev_read_cb_t"):touch.index("void touch_calibration_init")],
                         old[old.index("static lv_indev_read_cb_t"):old.index("void touch_calibration_init")])

    def test_retained_artifacts(self):
        path=BASE.parent/"retained-artifacts.json"
        contract=json.loads((APP/"tests/isolation_contract.json").read_text(encoding="utf-8"))
        self.assertEqual(sha(path),contract["original_artifact_manifest_sha256"])
        self.assert_retained_artifacts(typed_manifest(path,artifacts=True))
        # Preserve both the original retained build and the explicit restore copy.
        self.assert_retained_artifacts({str(BASE.parent/"known-working.bin"):
            "ac208429d7d7ec10ca1dfa6552d989131b34b4c3ad3902de0ffbd70e3cc64044"})

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--baseline",type=Path,required=True)
    BASE=parser.parse_args().baseline.expanduser().resolve()
    unittest.main(argv=["isolation_test.py"],verbosity=2)