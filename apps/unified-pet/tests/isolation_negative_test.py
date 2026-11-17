"""Exercise public isolation assertions against disposable private copies only.

Usage: python3 tests/isolation_negative_test.py --baseline ORIGINAL --out PRIVATE
No live source/artifact is modified; no SDK, device or network is used.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import stat

sys.dont_write_bytecode=True
os.umask(0o077)
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("pet_isolation",HERE/"isolation_test.py")
isolation=importlib.util.module_from_spec(spec)
spec.loader.exec_module(isolation)


def new_output_leaf(base,out):
    """Claim one absent private leaf; never traverse a caller-owned tree."""
    out=out.expanduser().absolute()
    for p in (out,*out.parents):
        if p.is_symlink():
            raise ValueError("output path must not contain symlinks")
    out=out.resolve()
    for protected in (isolation.ROOT.resolve(),base.parent.resolve()):
        if out.is_relative_to(protected) or protected.is_relative_to(out):
            raise ValueError("output must not contain or be inside source/baseline")
    # No mkdir(parents=True): existing parents remain completely untouched.
    out.mkdir(mode=0o700,exist_ok=False)
    return out


def run(base,out):
    original_root=isolation.ROOT
    out=new_output_leaf(base,out)
    root=out/"fixture"
    root.mkdir(mode=0o700,exist_ok=False)
    contract=json.loads((HERE/"isolation_contract.json").read_text(encoding="utf-8"))
    manifest=isolation.typed_manifest(base.parent/"source-manifest.json")
    expected=manifest|contract["task1_changes"]|contract["task1_additions"]|contract["input_diag_sources"]
    names=set(expected)|isolation.TASK2_PATHS
    for name in sorted(names):
        src=original_root/name
        if not src.exists() and not src.is_symlink():
            assert expected.get(name) is None,name
            continue
        dst=root/name
        dst.parent.mkdir(parents=True,exist_ok=True)
        if src.is_symlink():
            dst.symlink_to(src.readlink())
        else:
            shutil.copyfile(src,dst)
    isolation.ROOT=root
    isolation.APP=root/"apps/unified-pet"
    isolation.BASE=base
    checks=isolation.Isolation()
    # This exact parent-owned ledger is mutable task metadata, not immutable
    # product source. Prove edits pass without altering any baseline or hash.
    ledger=root/".superpowers/sdd/2026-11-11-unified-pet-foundation/progress.md"
    before_ledger=ledger.read_bytes()
    try:
        ledger.write_bytes(before_ledger+b"\nParent task status changed legitimately.\n")
        checks.test_source_preservation()
    finally:
        ledger.write_bytes(before_ledger)
    print("PASS private-copy legitimate mutable-ledger edit")
    # Every mutant begins with a passing owning assertion, not an already-red
    # fixture. Use the public methods themselves, not a second preservation model.
    checks.test_source_preservation()
    checks.test_project_boundary()
    checks.test_preservation_boundary()
    assert (root/"main/live_voice.c").read_bytes()!=(
        isolation.APP/"main/live_voice.c").read_bytes()
    print("PASS independent app behavior diverges while original root remains pinned")
    results=[{"name":"mutable-ledger-edit","assertion":"test_source_preservation",
              "accepted":True}]

    def reject(name,path,mutate,method,fragment):
        original=path.read_bytes() if path.exists() else None
        method()
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(mutate(original))
        try:
            with checks.assertRaises(AssertionError) as failure:
                method()
            checks.assertIn(fragment,str(failure.exception))
            results.append({"name":name,"assertion":method.__name__,
                            "rejected":True,"failure":str(failure.exception)[:400]})
            print("PASS private-copy rejection",name,method.__name__)
        finally:
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)
        method()

    for name,path in (
        ("root-conversation","main/live_voice.c"),
        ("reviewed-root-cmake","main/CMakeLists.txt"),
        ("immutable-app-source","apps/unified-pet/main/main.c"),
        ("pinned-cap-lifecycle-fixture","apps/unified-pet/tests/audio_lifecycle_test.c"),
        ("pinned-cap-lifecycle-runner","apps/unified-pet/tests/run_audio_lifecycle.sh"),
        ("shared-tls","components/pet_tls/pet_tls.c"),
        ("shared-calibration","components/pet_touch_calibration/touch_calibration.c"),
        ("vendor","components/tailtiny/source-manifest.json"),
    ):
        reject(name,root/path,lambda b:b+b"\n/* unauthorized change */\n",
               checks.test_source_preservation,path)
    reject("unexpected-root-file",root/"main/unauthorized.c",lambda _:b"/* new */\n",
           checks.test_source_preservation,"unexpected source files")
    reject("unexpected-app-file",isolation.APP/"main/unauthorized.c",lambda _:b"/* new */\n",
           checks.test_source_preservation,"unexpected source files")
    # A neighboring SDD source record is not covered by the exact ledger
    # exception; neither are source/config directories.
    neighbor=next(name for name in expected if
                  name.startswith(".superpowers/") and name!=str(ledger.relative_to(root))
                  and expected[name] and "sha256" in expected[name])
    reject("immutable-ledger-neighbor",root/neighbor,lambda b:b+b"\nunauthorized\n",
           checks.test_source_preservation,neighbor)
    # Even the mutable metadata exception must remain a regular non-symlink.
    ledger.unlink()
    ledger.symlink_to(isolation.APP/"main/main.c")
    try:
        with checks.assertRaises(AssertionError):
            checks.test_source_preservation()
        results.append({"name":"mutable-ledger-symlink","rejected":True,
                        "assertion":"test_source_preservation"})
        print("PASS private-copy rejection mutable-ledger-symlink")
    finally:
        ledger.unlink();ledger.write_bytes(before_ledger)
    reject("root-main-import",isolation.APP/"main/live_voice.c",
           lambda b:b+b'\n#include "../../../main/live_voice.c"\n',
           checks.test_project_boundary,"Regex matched")
    reject("absolute-root-main-import",isolation.APP/"main/live_voice.c",
           lambda b:b+f'\n#include "{root}/main/live_voice.c"\n'.encode(),
           checks.test_project_boundary,"root-main import")
    reject("NVS-legacy-write",isolation.APP/"main/network.c",
           lambda b:b.replace(b'nvs_set_str(storage.own,"ssid"',
                               b'nvs_set_str(storage.legacy,"ssid"',1),
           checks.test_project_boundary,"storage.own")
    reject("NVS-store-namespace",isolation.APP/"main/pet_store.h",
           lambda b:b.replace(b'"unified_pet"',b'"pet_setup"',1),
           checks.test_source_preservation,"apps/unified-pet/main/pet_store.h")

    # The original artifact manifest contains absolute external paths. Do not
    # rewrite it, remap those live paths, or weaken its public digest pin. Feed
    # a private copy + the same known digest to the exact assertion helper used
    # by test_retained_artifacts. The full public command checks all 2,279 real
    # retained entries and the known restore copy independently.
    firmware=out/"private-known-working.bin"
    shutil.copyfile(base.parent/"known-working.bin",firmware)
    digest="ac208429d7d7ec10ca1dfa6552d989131b34b4c3ad3902de0ffbd70e3cc64044"
    def check_firmware():
        checks.assert_retained_artifacts({str(firmware):digest})
    reject("known-firmware-tamper",firmware,lambda b:bytes([b[0]^1])+b[1:],
           check_firmware,"private-known-working.bin")

    # Historical schemas are deliberately distinct: never accept a string as a
    # source entry or an object as an artifact digest.
    for artifacts,value in ((False,{"main/main.c":"a"*64}),
                            (True,{"firmware.bin":{"sha256":"a"*64}})):
        bad=out/("bad-artifacts.json" if artifacts else "bad-source.json")
        bad.write_text(json.dumps(value),encoding="utf-8")
        with checks.assertRaises(ValueError):
            isolation.typed_manifest(bad,artifacts=artifacts)
        results.append({"name":bad.stem,"rejected":True,"assertion":"typed_manifest"})
        print("PASS private-copy rejection",bad.stem,"typed_manifest")
    (out/"results.json").write_text(json.dumps(results,indent=2)+"\n",encoding="utf-8")
    for p in out.rglob("*"):
        if not p.is_symlink():
            p.chmod(0o700 if p.is_dir() else 0o600)
    out.chmod(0o700)
    rejected=sum(bool(r.get("rejected")) for r in results)
    accepted=sum(bool(r.get("accepted")) for r in results)
    print(f"PASS isolation fixtures: {rejected} rejected + {accepted} accepted = {len(results)} cases; private copies only")


def check_output_safety(base,out):
    """Regression uses only a new private sandbox, never chmods live sentinels."""
    out=new_output_leaf(base,out)
    def tree(path):
        result={}
        for p in [path,*sorted(path.rglob("*"))]:
            mode=stat.S_IMODE(p.lstat().st_mode)
            content=(str(p.readlink()) if p.is_symlink() else
                     hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            result[str(p.relative_to(path))]=(mode,content)
        return result
    existing=out/"existing"
    existing.mkdir(mode=0o755)
    sentinel=existing/"unrelated-executable"
    sentinel.write_text("private sentinel\n",encoding="utf-8");sentinel.chmod(0o755)
    old_root=isolation.ROOT
    cases=[("existing",existing,base,old_root)]
    ancestor=out/"ancestor"
    (ancestor/"source").mkdir(parents=True)
    (ancestor/"baseline").mkdir()
    (ancestor/"sentinel").write_text("ancestor sentinel\n",encoding="utf-8")
    cases.append(("source-baseline-ancestor",ancestor,ancestor/"baseline/snapshot",ancestor/"source"))
    link=out/"link"
    link.symlink_to(existing,target_is_directory=True)
    cases.append(("symlink-leaf",link,base,old_root))
    cases.append(("symlink-parent",link/"new-leaf",base,old_root))
    for name,candidate,test_base,test_root in cases:
        before=tree(out)
        isolation.ROOT=test_root
        try:
            try:
                run(test_base,candidate)
            except (ValueError,FileExistsError):
                pass
            else:
                raise AssertionError(f"unsafe output accepted: {name}")
        finally:
            isolation.ROOT=old_root
        assert tree(out)==before,f"unsafe output changed content/mode: {name}"
        print("PASS fixture output refusal unchanged files/modes",name)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--baseline",required=True,type=Path)
    parser.add_argument("--out",required=True,type=Path)
    parser.add_argument("--check-output-safety",action="store_true")
    args=parser.parse_args()
    if args.check_output_safety:
        check_output_safety(args.baseline.expanduser().resolve(),args.out.expanduser().absolute())
    else:
        run(args.baseline.expanduser().resolve(),args.out.expanduser().absolute())