"""Pack the manifest-declared file set into one handoff zip, outside the package.

Why a zip is safe to hand over: the authoritative evidence is the *per-file*
SHA256 in results/MANIFEST.sha256.json, which survives any container. The zip is
only a transport convenience, so this tool guarantees three things:

1. **Deterministic bytes.** Members are written in manifest order with a fixed
   timestamp, fixed attributes, and no directory entries - building twice with an
   unchanged tree produces the identical archive hash.
2. **No self-reference.** The archive's own hash is never stored inside it. It is
   published in a sidecar written next to the zip, together with size, member
   count, and the manifest digest the archive contains.
3. **Round-trip proof.** After writing, every member is re-read *from the archive*
   and its bytes are re-hashed against the manifest. A zip that unpacks to a
   different digest is reported as a failure, not shipped.

usage: python tools/build_handoff_zip.py --out ../<name>.zip [--timestamp YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(PKG, "results", "MANIFEST.sha256.json")
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)   # fixed so the archive is reproducible


def sha_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="destination .zip path (keep it outside the package)")
    ap.add_argument("--label", default="", help="optional label recorded in the sidecar")
    ap.add_argument("--manifest", default=MANIFEST,
                    help="manifest to pack against; defaults to results/MANIFEST.sha256.json. "
                         "Pointing it at a tampered copy is how the drift check is smoke-tested.")
    args = ap.parse_args()

    if not os.path.isfile(args.manifest):
        print(json.dumps({"error": "MANIFEST_MISSING", "expected": args.manifest,
                          "remedy": "run tools/build_manifest.py first"}))
        return 2
    man = json.load(open(args.manifest, encoding="utf-8"))
    entries = man["files"]
    out = os.path.abspath(args.out)
    if out.startswith(PKG + os.sep):
        print(json.dumps({"error": "REFUSE_ZIP_INSIDE_PACKAGE", "out": out}))
        return 2
    container = ["results/MANIFEST.sha256.json", "results/MANIFEST.sha256.csv"]
    for rel in container:
        if not os.path.isfile(os.path.join(PKG, rel)):
            print(json.dumps({"error": "CONTAINER_MEMBER_MISSING", "path": rel}))
            return 2

    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".building"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for group, e_list in (("business", entries), ("container", None)):
            for rel in (e_list if e_list else container):
                path = os.path.join(PKG, rel if isinstance(rel, str) else rel["path"])
                with open(path, "rb") as f:
                    blob = f.read()
                if e_list:                      # business files must still match the manifest
                    if len(blob) != rel["bytes"] or sha_bytes(blob) != rel["sha256"]:
                        print(json.dumps({"error": "SOURCE_DRIFTED_AFTER_MANIFEST",
                                          "path": rel["path"],
                                          "manifest_sha256": rel["sha256"],
                                          "actual_sha256": sha_bytes(blob)}))
                        os.remove(tmp)
                        return 1
                    arcname = rel["path"]
                else:
                    arcname = rel
                info = zipfile.ZipInfo(arcname, date_time=FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                info.create_system = 0
                z.writestr(info, blob)
        bad = [i.filename for i in z.infolist() if "\\" in i.filename]
        if bad:
            print(json.dumps({"error": "BACKSLASH_IN_ARCNAME", "members": bad[:5]}))
            os.remove(tmp)
            return 1
    os.replace(tmp, out)

    # ---- verification: everything below is measured FROM THE ARCHIVE, not from disk ----
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        bad_crc = z.testzip()
        members, mismatches = [], []
        for e in entries:
            with z.open(e["path"]) as f:
                blob = f.read()
            digest = sha_bytes(blob)
            members.append({"path": e["path"], "bytes": len(blob), "sha256": digest,
                            "origin": "business"})
            if digest != e["sha256"] or len(blob) != e["bytes"]:
                mismatches.append(e["path"])
        container_digests = {}
        for rel in container:
            with z.open(rel) as f:
                blob = f.read()
            container_digests[rel] = sha_bytes(blob)
            members.append({"path": rel, "bytes": len(blob), "sha256": container_digests[rel],
                            "origin": "container"})
        missing = [e["path"] for e in entries if e["path"] not in names]
        unexpected = [n for n in names
                      if n not in {e["path"] for e in entries} and n not in container]

    zip_blob = open(out, "rb").read()
    sidecar = {
        "schema": "awake_kit_handoff_zip_v2",
        "label": args.label,
        "zip_path_basename": os.path.basename(out),
        "zip_bytes": len(zip_blob),
        "zip_sha256": sha_bytes(zip_blob),
        "business_file_count": len(entries),
        "container_file_count": len(container),
        "archive_member_total": len(names),
        "member_count_equation_holds": len(names) == len(entries) + len(container),
        "manifest_declares_member_count": man["closure"]["listed_total"] == len(entries),
        "manifest_sha256_inside_zip": container_digests["results/MANIFEST.sha256.json"],
        "manifest_csv_sha256_inside_zip": container_digests["results/MANIFEST.sha256.csv"],
        "digest_source": "re-read from archive members after writing; never taken from disk",
        "round_trip": {"per_member_sha_mismatches": mismatches, "missing_members": missing,
                       "unexpected_members": unexpected, "crc_bad_member": bad_crc,
                       "all_members_match_manifest": not (mismatches or missing or unexpected
                                                          or bad_crc)},
        "members": members,
        "self_reference_note": "the archive hash is recorded here, never inside the archive",
        "deliberately_excluded": ["work/ scratch state roots and SQLite state databases",
                                  "the archive itself",
                                  "any private note containing real absolute paths or identifiers",
                                  "the out-of-package signature denylist"],
    }
    side_path = out + ".SIDECAR.json"
    with open(side_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(sidecar, indent=2, ensure_ascii=False))
    print(json.dumps({k: sidecar[k] for k in
                      ("zip_path_basename", "zip_bytes", "zip_sha256", "business_file_count",
                       "container_file_count", "archive_member_total",
                       "member_count_equation_holds", "manifest_sha256_inside_zip",
                       "manifest_csv_sha256_inside_zip", "round_trip")}
                     | {"sidecar": os.path.basename(side_path),
                        "sidecar_sha256": sha_bytes(open(side_path, "rb").read())},
                     ensure_ascii=False, indent=2))
    return 0 if sidecar["round_trip"]["all_members_match_manifest"] else 1


if __name__ == "__main__":
    sys.exit(main())
